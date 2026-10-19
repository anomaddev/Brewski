"""Run state machine.

The loop is hardware-agnostic. It reads a :class:`HardwareSource`, decides
whether the heaters should be on, and appends samples while a run is going.
The first enabled probe is the one compared with the target. Heaters turn
off before that target by the coast measured in calibration, and sooner
when the pot is rising faster than that measurement, so heat still stored
in the element can finish the rise. Disabled heaters stay off. The installed service drives the SSRs,
the MAX31865 probes, and the HTS221. After setup, this loop also drives
the I2C LCD, the panel buttons, the LED relays, and the target encoder.
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
from dataclasses import replace

from brewski import __version__
from brewski.emergency import summary
from brewski.lcd import NullDisplay, display_key, open_display, status_lines
from brewski.menu import SoftwareMenu
from brewski.network import display_identity
from brewski.panel import indicator_leds, open_panel, panel_key
from brewski.sources.base import HardwareSource
from brewski.updates import (
    ReleaseInfo,
    apply_supported,
    check_release,
    read_status,
    report as update_report,
    request_reboot,
    start_apply_service,
    write_status,
)
from brewski.store import (
    EARLY_CUTOFF_MAX_F,
    SLOT_COUNT,
    TARGET_MAX_F,
    TARGET_MIN_F,
    Settings,
    SettingsError,
    Store,
    charge_gallons,
    default_gpio,
    heater_on_seconds,
    merge_settings,
    shape_changed,
    utc_now,
)

logger = logging.getLogger(__name__)

# Width of the hysteresis band. The element turns back on only after the
# probe falls this far below the early-cutoff temperature.
DEADBAND_F = 2.0
# A cooldown runs from the first time the control probe reaches the target
# until the temperature stops moving. After the run is stopped, that lower
# stop is a 5 minute stretch where the probe stays inside a 2°F band.
COOLDOWN_BAND_F = 2.0
COOLDOWN_STABLE_SECONDS = 5 * 60
SAMPLE_INTERVAL_SECONDS = 5.0
# Ignore a stalled clock that tries to replay more than an hour in one tick.
MAX_TICK_SECONDS = 3600.0
# A calibration only trusts a coast after the element has been on long enough
# to be hot. Shorter pulses still have a cold sheath and teach the wrong number.
COAST_SOAK_SECONDS = 30.0
# The early cutoff would open the SSR before the setpoint, and the probe would
# never actually reach it. Calibration keeps heating until the probe is this
# far above the target, then lets that overshoot become the measurement.
CALIBRATION_ABOVE_F = 1.0
# After that soaked shutoff, the coast is the rise in a rolling average of the
# control probe, measured at the crest. A slow climb is still climbing.
COAST_AVERAGE_SECONDS = 10.0
# The crest is real once the average has fallen this far below its high and
# stayed there. The saved coast is the high itself, not this confirmation dip.
COAST_PEAK_DROP_F = 0.4
COAST_PEAK_CONFIRM_SECONDS = 10.0
# A pot that levels off without a clear drop. The high must gain this much
# to keep the watch open.
COAST_PLATEAU_RISE_F = 0.2
COAST_PLATEAU_SECONDS = 90.0
COAST_WATCH_MAX_SECONDS = 20 * 60
# After the crest, how fast the pot falls with the elements off.
COOLDOWN_MIN_DROP_F = 1.0
COOLDOWN_MIN_SECONDS = 45.0
COOLDOWN_AGREE_SECONDS = 30.0
COOLDOWN_RATE_AGREE_F = 0.2
COOLDOWN_RATE_AGREE_FRACTION = 0.25
COOLDOWN_WATCH_MAX_SECONDS = 4 * 60
COOLDOWN_RATE_MAX = 120.0
# A sustain pulse is sized so its guessed coast replaces about this much sag.
SUSTAIN_RIPPLE_F = 1.0
# Reached the target when the crest is at least this close to it.
# Above the target counts: that overshoot is what the calibration records.
CALIBRATION_LAND_F = 0.5
# Control-probe samples kept for the live rise or fall. Short enough that a
# change in heater count shows up, long enough that one noisy reading does not.
PREDICT_WINDOW_SECONDS = 15.0
# No slope until the window holds this many points and this much time. A single
# jumped reading is not a rate.
PREDICT_MIN_SAMPLES = 4
PREDICT_MIN_SPAN_SECONDS = 8.0
# Flatter than this is not moving. A stalled probe does not invent an ETA.
PREDICT_MIN_RATE_F_PER_SEC = 0.01
# Both estimates stop here. A cold pot must not report a multi-day climb.
PREDICT_MAX_HORIZON_SECONDS = 4 * 60 * 60
# A fast rise may shut off sooner than the learned coast, but not by more than
# this multiple, and never by more than EARLY_CUTOFF_MAX_F degrees.
PREDICT_COAST_SCALE_MAX = 3.0
# Cooldown is time to get this close to a nominal room temperature. Reaching
# the room itself would take forever under Newton's law of cooling.
PREDICT_AMBIENT_F = 70.0
PREDICT_AMBIENT_BAND_F = 5.0


def scaled_cutoff(
    learned_coast_f: float | None,
    learned_gallons: float | None,
    new_gallons: float | None,
) -> float | None:
    """Scale a remembered coast when the charge volume changes.

    The same stored heat adds more degrees to a smaller charge. Returns None
    when there is nothing to scale, including a repeat at the same volume.
    """

    if learned_coast_f is None or learned_gallons is None or new_gallons is None:
        return None
    if learned_gallons == new_gallons:
        return None
    scaled = learned_coast_f * (learned_gallons / new_gallons)
    return min(EARLY_CUTOFF_MAX_F, max(0.0, round(scaled, 2)))


def scaled_rate(
    learned_rate: float | None,
    learned_gallons: float | None,
    new_gallons: float | None,
) -> float | None:
    """Scale a cooling rate when the charge volume changes.

    The same heat loss changes the temperature of a smaller charge faster.
    Returns None when there is nothing to scale, including a repeat at the
    same volume.
    """

    if learned_rate is None or learned_gallons is None or new_gallons is None:
        return None
    if learned_gallons == new_gallons:
        return None
    scaled = learned_rate * (learned_gallons / new_gallons)
    return min(COOLDOWN_RATE_MAX, max(0.0, round(scaled, 2)))


def sustain_pulse_seconds(
    coast_f: float,
    coast_seconds: float,
    *,
    ripple_f: float = SUSTAIN_RIPPLE_F,
) -> float | None:
    """On-time whose guessed coast replaces about ``ripple_f`` degrees.

    A fully hot element coasts ``coast_f`` degrees over ``coast_seconds``.
    A shorter on-time coasts less, approximately
    ``coast_f * (1 - exp(-t / coast_seconds))``. Returns None when the full
    coast is already as small as the ripple, because a shorter pulse cannot
    shrink it.
    """

    if coast_f <= ripple_f or coast_seconds <= 0 or ripple_f <= 0:
        return None
    fraction = min(0.999, ripple_f / coast_f)
    seconds = -float(coast_seconds) * math.log(1.0 - fraction)
    return max(1.0, seconds)


def reheat_temperature(
    target_f: float,
    cool_f_per_min: float,
    pulse_seconds: float,
    *,
    ripple_f: float = SUSTAIN_RIPPLE_F,
) -> float:
    """Probe temperature where a sustain pulse should start.

    The pulse begins early by the degrees the pot will fall, at the learned
    cooling rate, during the pulse itself.
    """

    lead = (max(0.0, cool_f_per_min) / 60.0) * max(0.0, pulse_seconds)
    return float(target_f) - ripple_f - lead


def _slope_per_min(samples: list[tuple[float, float]], *, min_drop: float) -> float | None:
    start_t, start_v = samples[0]
    end_t, end_v = samples[-1]
    dt = end_t - start_t
    drop = start_v - end_v
    if dt <= 0 or drop < min_drop:
        return None
    return drop / dt * 60.0


def _cooling_progress(
    samples: list[tuple[float, float]],
    elapsed: float,
    average: float,
) -> tuple[float | None, bool]:
    """Append one falling average and report the rate and whether it is settled.

    The rate is °F per minute. Settled means the last 30 seconds agrees with
    the whole fall, or the watch has run for 4 minutes.
    """

    samples.append((elapsed, average))
    if len(samples) < 2:
        return None, False
    start_t = samples[0][0]
    end_t = samples[-1][0]
    duration = end_t - start_t
    full = _slope_per_min(samples, min_drop=COOLDOWN_MIN_DROP_F)
    if duration >= COOLDOWN_WATCH_MAX_SECONDS:
        return full, True
    if duration < COOLDOWN_MIN_SECONDS or full is None:
        return full, False
    window_start = end_t - COOLDOWN_AGREE_SECONDS
    recent = [(stamp, reading) for stamp, reading in samples if stamp >= window_start]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < COOLDOWN_AGREE_SECONDS - 1:
        return full, False
    recent_rate = _slope_per_min(recent, min_drop=0.05)
    if recent_rate is None:
        return full, False
    agrees = abs(recent_rate - full) <= COOLDOWN_RATE_AGREE_F or (
        full > 0 and abs(recent_rate - full) / full <= COOLDOWN_RATE_AGREE_FRACTION
    )
    if agrees:
        return recent_rate, True
    return full, False


def slope_f_per_sec(samples: list[tuple[float, float]]) -> float | None:
    """End-point °F/s over a window of ``(elapsed_seconds, temp_f)``.

    Positive is a rise. Returns None until the window is long enough to trust.
    """

    if len(samples) < PREDICT_MIN_SAMPLES:
        return None
    start_t, start_v = samples[0]
    end_t, end_v = samples[-1]
    dt = end_t - start_t
    if dt < PREDICT_MIN_SPAN_SECONDS:
        return None
    return (end_v - start_v) / dt


def predicted_coast_f(
    learned_coast_f: float | None,
    learned_coast_seconds: float | None,
    rate_f_per_sec: float | None,
) -> float:
    """Degrees the probe should still climb after the SSR opens.

    The learned coast is the floor. It is the residual heat measured at
    calibration, not a number from Settings. A faster live rise scales that
    coast up, because the same sheath dumps more degrees into a pot that is
    already climbing quickly. A slower rise does not shrink it. No learned
    coast, or no trustworthy positive slope, leaves the floor alone.
    """

    if learned_coast_f is None or learned_coast_f <= 0:
        return 0.0
    coast = min(EARLY_CUTOFF_MAX_F, float(learned_coast_f))
    if (
        rate_f_per_sec is None
        or rate_f_per_sec < PREDICT_MIN_RATE_F_PER_SEC
        or learned_coast_seconds is None
        or learned_coast_seconds <= 0
    ):
        return coast
    implied = coast / float(learned_coast_seconds)
    if implied <= 0:
        return coast
    scale = rate_f_per_sec / implied
    if scale < 1.0:
        scale = 1.0
    elif scale > PREDICT_COAST_SCALE_MAX:
        scale = PREDICT_COAST_SCALE_MAX
    return min(EARLY_CUTOFF_MAX_F, coast * scale)


def seconds_to_target(
    temp_f: float,
    target_f: float,
    coast_f: float,
    rate_f_per_sec: float | None,
    learned_coast_f: float | None,
    learned_coast_seconds: float | None,
) -> float | None:
    """Seconds until the probe reaches the target, including residual heat.

    The powered gap is what the element still has to climb before the SSR
    opens. The tail is the learned coast duration, scaled when the live coast
    is larger than the measurement. Once the probe is already inside that
    coast, only the remaining rise at the current rate is left. Returns None
    when the probe is not rising.
    """

    if temp_f >= target_f:
        return 0.0
    if rate_f_per_sec is None or rate_f_per_sec < PREDICT_MIN_RATE_F_PER_SEC:
        return None
    gap = target_f - temp_f
    powered = gap - max(0.0, coast_f)
    if powered > 0:
        if learned_coast_f and learned_coast_seconds and learned_coast_f > 0:
            tail = float(learned_coast_seconds) * (max(0.0, coast_f) / float(learned_coast_f))
        elif coast_f > 0:
            tail = coast_f / rate_f_per_sec
        else:
            tail = 0.0
        seconds = powered / rate_f_per_sec + tail
    else:
        seconds = gap / rate_f_per_sec
    return min(PREDICT_MAX_HORIZON_SECONDS, max(0.0, seconds))


def seconds_to_cooldown(
    temp_f: float,
    rate_f_per_sec: float | None,
    *,
    learned_rate_f_per_min: float | None = None,
) -> float | None:
    """Seconds for a falling pot to get within a few degrees of room temperature.

    The live slope gives the cooling constant ``k = -rate / (temp - ambient)``.
    Time to ``ambient + PREDICT_AMBIENT_BAND_F`` is ``ln((temp - ambient) / band) / k``.
    Before the window is full, a learned °F/min is a linear stand-in over that
    same drop. A rise or a flat line returns None.
    """

    goal = PREDICT_AMBIENT_F + PREDICT_AMBIENT_BAND_F
    if temp_f <= goal:
        return 0.0
    if rate_f_per_sec is not None and rate_f_per_sec <= -PREDICT_MIN_RATE_F_PER_SEC:
        above = temp_f - PREDICT_AMBIENT_F
        if above <= PREDICT_AMBIENT_BAND_F:
            return 0.0
        k = -rate_f_per_sec / above
        if k <= 0:
            return None
        seconds = math.log(above / PREDICT_AMBIENT_BAND_F) / k
        return min(PREDICT_MAX_HORIZON_SECONDS, max(0.0, seconds))
    # A live rise or a flat window is not a cooldown. The learned rate is only
    # a stand-in before the window is long enough to have a slope at all.
    if rate_f_per_sec is not None:
        return None
    if learned_rate_f_per_min is not None and learned_rate_f_per_min > 0:
        seconds = (temp_f - goal) / (learned_rate_f_per_min / 60.0)
        return min(PREDICT_MAX_HORIZON_SECONDS, max(0.0, seconds))
    return None


def _forecast_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    return round(min(PREDICT_MAX_HORIZON_SECONDS, max(0.0, value)), 1)


def heater_should_be_on(
    temperature_f: float,
    target_f: float,
    currently_on: bool,
    *,
    early_cutoff_f: float,
    deadband_f: float = DEADBAND_F,
) -> bool:
    """Bang-bang decision for one resistive element on a zero-crossing SSR.

    The SSR can only open or close; it cannot throttle. Opening it does not
    stop the heating. The element sheath stays hot and keeps transferring
    heat into the water or mash for a noticeable time, often 10–60 seconds,
    depending on element mass, power, and volume. The probe lags that bulk
    temperature as well: it has its own thermal mass, and the pot is not
    perfectly mixed. Turning the element off only when the probe finally
    reads the setpoint therefore overshoots. That continued rise after the
    SSR is already open is normal.

    Power is removed early, ``early_cutoff_f`` degrees before the setpoint,
    so the heat still stored in the element carries the liquid the rest of
    the way. Power is applied again only when the probe falls through the
    bottom of a hysteresis band ``deadband_f`` degrees wide. Inside that
    band the previous state is kept, so a drifting reading cannot chatter
    the SSR.
    """

    cutoff_f = target_f - early_cutoff_f
    if temperature_f >= cutoff_f:
        return False
    if temperature_f <= cutoff_f - deadband_f:
        return True
    return currently_on


class ControllerError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Controller:
    def __init__(self, store: Store, source: HardwareSource) -> None:
        self.store = store
        self.source = source
        self._lock = threading.Lock()
        self._stop_loop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shut_down = False
        self._display = NullDisplay()
        self._display_key: tuple[int, int] | None = None
        self._panel: object | None = None
        self._panel_key: tuple[int, ...] | None = None
        self._menu: SoftwareMenu | None = None
        self._menu_generation = 0
        self._menu_dirty = False
        self._menu_check_thread: threading.Thread | None = None
        self._hold_display = False
        # The dial paints this immediately. It is written to the database
        # once the knob has been still, so the screen is not waiting on disk.
        self._dial_target: int | None = None
        self._dial_dirty = False
        self._dial_moved_at = 0.0
        self._emergency: list[str] = []
        self._cabinet_temp: float | None = None
        self._cabinet_humidity: float | None = None
        self._climate_fault = False
        self._mode = "idle"
        self._run_id: int | None = None
        self._elapsed = 0.0
        self._since_sample = 0.0
        self._cooldown_run_id: int | None = None
        self._cooldown_elapsed = 0.0
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor: int | None = None
        # (cooldown elapsed seconds, control temperature) while heaters are off
        # after the run. The lower stop is read from the last 5 minutes of this.
        self._cooldown_window: list[tuple[float, float]] = []
        self._run_kind = "run"
        self._charge_gallons: float | None = None
        self._coast_watch: dict | None = None
        self._coast_f: float | None = None
        self._coast_seconds: float | None = None
        self._calibration_settled = False
        # Shown on the cooldown screen after a calibration saves its result.
        self._display_coast: float | None = None
        self._display_rate: float | None = None
        # Elapsed time when the current sustain pulse should end.
        self._pulse_until: float | None = None
        # Set once this run has already climbed above the sustain point.
        # The approach has to stay off after the coast cutoff; a pulse before
        # that point heats the rest of the way and the stored heat overshoots.
        self._hold_ready = False
        self._heaters = [False] * SLOT_COUNT
        # (predict clock seconds, control temperature). The clock follows tick
        # time, including pause, so a test can advance it without sleeping.
        self._predict_clock = 0.0
        self._predict_samples: list[tuple[float, float]] = []
        self._heater_on_for = [0.0] * SLOT_COUNT
        # Powered time not yet written to the element-hour meters.
        self._heater_life_pending = [0.0] * SLOT_COUNT
        # Infinity means the off dwell is already satisfied, so the first
        # turn-on of a run is not delayed. A control-law turn-off sets this
        # back to zero. Pause and Stop leave it satisfied on purpose.
        self._heater_off_for = [math.inf] * SLOT_COUNT
        # A previous process may have stopped mid-run. Close that record so
        # the next start is a new run, not a continuation of a dead one.
        self.store.close_open_runs()
        open_cooldown = self.store.claim_open_cooldown()
        if open_cooldown is not None:
            self._adopt_cooldown(open_cooldown)
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        self._sample_climate()
        self._emergency = self._emergency_codes(self.store.get_settings())
        self._set_heaters([False] * SLOT_COUNT if self._emergency else self._heaters)

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_loop.clear()
        self._sync_panel()
        self._thread = threading.Thread(target=self._loop, name="brewski-controller", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        self._stop_loop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            if self._mode in ("running", "paused"):
                self._finish_run()
            if self._mode == "cooling":
                self._persist_cooldown()
            self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
            self._flush_heater_life()
        self._close_display()
        self._close_panel()
        closer = getattr(self.source, "close", None)
        if closer is not None:
            closer()

    def tick(self, dt: float) -> None:
        """Advance the simulation and the run clock by ``dt`` seconds."""

        self._apply_panel()
        self._flush_dial_target()
        remaining = min(max(0.0, float(dt)), MAX_TICK_SECONDS)
        with self._lock:
            while remaining > 0:
                slice_dt = min(1.0, remaining)
                self._tick(slice_dt)
                remaining -= slice_dt

    def status(self) -> dict:
        with self._lock:
            return self._status()

    def runs(self) -> dict:
        with self._lock:
            listed = self.store.list_runs(self._live_overlay(), self._cooldown_overlay())
            listed["stats"]["heater_life_seconds"] = self._life_seconds()
            return listed

    def run(self, run_id: int) -> dict:
        with self._lock:
            record = self.store.get_run(run_id)
            if record is None:
                raise ControllerError("Run not found", 404)
            overlay = self._live_overlay()
            if overlay is not None and overlay["id"] == run_id:
                record["elapsed_seconds"] = round(self._elapsed, 2)
                record["status"] = self._mode
                # The database elapsed lags the in-memory clock between samples.
                record["heater_on_seconds"] = heater_on_seconds(
                    record["samples"], record["elapsed_seconds"]
                )
            cooldown = self._cooldown_overlay()
            if cooldown is not None and cooldown["id"] == run_id:
                record["cooldown_seconds"] = round(self._cooldown_elapsed, 2)
                record["cooldown_status"] = "open"
            return record

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            self._write_pending_target()
            current = self.store.get_settings()
            try:
                updated = merge_settings(current, patch)
            except SettingsError as exc:
                raise ControllerError(str(exc), 400) from exc
            if self._mode in ("running", "paused") and shape_changed(current, updated):
                raise ControllerError(
                    "Heater, probe, and GPIO setup is locked while a run is open",
                    409,
                )
            self.store.save_settings(updated)
            self._dial_target = None
            self._dial_dirty = False
            self._apply_idle_outputs(updated)
            saved = self.store.get_settings().to_dict()
        self._refresh_display()
        return saved

    def replace_heater(self, slot: int) -> dict:
        """Zero one element's hour meter after it has been replaced."""

        with self._lock:
            if slot < 0 or slot >= SLOT_COUNT:
                raise ControllerError("Unknown heater", 404)
            if self._mode in ("running", "paused"):
                raise ControllerError("Replace the element after the run is stopped", 409)
            self._heater_life_pending[slot] = 0.0
            self._flush_heater_life()
            self.store.reset_heater_life(slot)
            return self._status()

    def reset(self) -> dict:
        """Clear stored runs and return the controller to its first-boot state.

        Element hours are kept. Replace an element to start that meter over.
        """

        with self._lock:
            self._flush_heater_life()
            self._mode = "idle"
            self._run_id = None
            self._elapsed = 0.0
            self._since_sample = 0.0
            self._clear_coast()
            self._clear_cooldown()
            self._clear_predict()
            self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
            reset_source = getattr(self.source, "reset", None)
            if reset_source is not None:
                reset_source()
            self._set_heaters(self._heaters)
            self.store.reset()
            self._dial_target = None
            self._dial_dirty = False
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            saved = self.store.get_settings().to_dict()
        self._refresh_display()
        return saved

    def update_status(self) -> dict:
        return update_report(self.store.path.parent)

    def start_update(self) -> dict:
        """Hand the install to the root oneshot. The database is already saved."""

        with self._lock:
            if self._mode in ("running", "paused"):
                raise ControllerError("Stop the run before updating.", 409)
        current = self.update_status()
        if not current["apply_supported"]:
            raise ControllerError("This machine cannot install a Brewski update.", 400)
        if not current["available"]:
            if not current["reachable"]:
                raise ControllerError("GitHub could not be reached.", 502)
            raise ControllerError("No update is available.", 409)
        directory = self.store.path.parent
        write_status(
            phase="downloading",
            current=current["current"],
            latest=current["latest"] or "",
            error=None,
            ref=current.get("ref"),
            directory=directory,
        )
        self._hold_display = True
        self._close_display()
        try:
            start_apply_service()
        except (OSError, subprocess.CalledProcessError) as exc:
            self._hold_display = False
            self._refresh_display()
            raise ControllerError("Could not start the updater.", 500) from exc
        return self.update_status()

    def reboot(self) -> dict:
        """Reboot the Pi. Refused while a run is open or an update is installing."""

        with self._lock:
            if self._mode in ("running", "paused"):
                raise ControllerError("Stop the run before rebooting.", 409)
        phase = read_status(self.store.path.parent).get("phase")
        if phase in {"downloading", "building", "installing"}:
            raise ControllerError("Wait for the update to finish.", 409)
        try:
            request_reboot()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ControllerError("Could not reboot this Pi.", 500) from exc
        return {"rebooting": True}

    def start(self, patch: dict | None = None) -> dict:
        with self._lock:
            if self._mode == "cooling":
                self._abandon_cooldown()
            elif self._mode != "idle":
                raise ControllerError("A run is already open", 409)
            patch = patch or {}
            if set(patch) - {"target_temp_f", "kind", "charge_gallons"}:
                raise ControllerError(
                    "Start only accepts target_temp_f, kind, and charge_gallons",
                    400,
                )
            kind = patch.get("kind", "run")
            if kind not in ("run", "calibration"):
                raise ControllerError("kind must be run or calibration", 400)
            if kind != "calibration" and "charge_gallons" in patch:
                raise ControllerError("charge_gallons is only for a calibration", 400)
            if kind != "calibration" and self.store.get_settings().learned_coast_f is None:
                raise ControllerError("Calibrate the still before starting a run", 409)
            gallons = None
            if patch.get("charge_gallons") is not None:
                try:
                    gallons = charge_gallons(patch["charge_gallons"])
                except SettingsError as exc:
                    raise ControllerError(str(exc), 400) from exc
            self._write_pending_target()
            if "target_temp_f" in patch:
                try:
                    updated = merge_settings(
                        self.store.get_settings(),
                        {"target_temp_f": patch["target_temp_f"]},
                    )
                except SettingsError as exc:
                    raise ControllerError(str(exc), 400) from exc
                self.store.save_settings(updated)
                self._dial_target = None
                self._dial_dirty = False
            settings = self.store.get_settings()
            if kind == "calibration" and gallons is None:
                gallons = settings.charge_gallons
            if kind == "calibration":
                updates: dict = {}
                scaled = scaled_cutoff(settings.learned_coast_f, settings.charge_gallons, gallons)
                if scaled is not None:
                    updates["learned_coast_f"] = scaled
                scaled_cool = scaled_rate(
                    settings.learned_cooldown_f_per_min, settings.charge_gallons, gallons
                )
                if scaled_cool is not None:
                    updates["learned_cooldown_f_per_min"] = scaled_cool
                if updates:
                    settings = replace(settings, **updates)
                    self.store.save_settings(settings)
            self._run_kind = kind
            self._charge_gallons = gallons
            self._coast_watch = None
            self._coast_f = None
            self._coast_seconds = None
            self._calibration_settled = False
            self._display_coast = None
            self._display_rate = None
            self._pulse_until = None
            self._hold_ready = False
            self._clear_predict()
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            self._sample_climate()
            self._emergency = self._emergency_codes(settings)
            if self._emergency:
                self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
                raise ControllerError("Emergency stop is active", 409)
            self._command_heaters(settings)
            run = self.store.create_run(settings, kind=kind, charge_gallons=gallons)
            self._run_id = int(run["id"])
            self._mode = "running"
            self._elapsed = 0.0
            self._since_sample = 0.0
            self._record_sample(settings)
            logger.info("Run %s started at %s°F", self._run_id, settings.target_temp_f)
            return self._status()

    def pause(self) -> dict:
        with self._lock:
            if self._mode != "running":
                raise ControllerError("There is no running run to pause", 409)
            settings = self.store.get_settings()
            self._pulse_until = None
            self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            self._mode = "paused"
            self._record_sample(settings)
            self.store.update_run(self._run_id, status="paused", elapsed_seconds=self._elapsed)
            self._flush_heater_life()
            logger.info("Run %s paused at %ss", self._run_id, round(self._elapsed, 1))
            return self._status()

    def resume(self) -> dict:
        with self._lock:
            if self._mode != "paused":
                raise ControllerError("There is no paused run to resume", 409)
            settings = self.store.get_settings()
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            self._sample_climate()
            self._emergency = self._emergency_codes(settings)
            if self._emergency:
                self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
                raise ControllerError("Emergency stop is active", 409)
            self._command_heaters(settings)
            self._mode = "running"
            self._record_sample(settings)
            self.store.update_run(self._run_id, status="running", elapsed_seconds=self._elapsed)
            logger.info("Run %s resumed", self._run_id)
            return self._status()

    def stop(self) -> dict:
        with self._lock:
            if self._mode not in ("running", "paused"):
                raise ControllerError("There is no open run to stop", 409)
            self._finish_run()
            return self._status()

    def _refresh_display(self) -> None:
        if self._hold_display:
            phase = read_status(self.store.path.parent).get("phase")
            if phase != "failed":
                return
            self._hold_display = False
        with self._lock:
            self._drop_menu_if_blocked()
            settings = self.store.get_settings()
            key = display_key(settings)
            if self._menu is not None and key is not None:
                address, hostname = "", ""
                if self._menu.screen == "networking":
                    try:
                        address, hostname = display_identity()
                    except Exception:
                        logger.exception("Could not read the network address")
                        address, hostname = "No address", "—"
                lines = self._menu.lines(__version__, address=address, hostname=hostname)
                led_state = self._menu.leds()
            else:
                lines = self._panel_lines(settings) if key is not None else None
                led_state = indicator_leds(self._mode, emergency=bool(self._emergency))
        self._sync_panel()
        if self._panel is not None:
            self._panel.set_leds(*led_state)  # type: ignore[attr-defined]
        if key != self._display_key:
            self._display.clear()
            self._display.close()
            if self._hold_display:
                self._display = NullDisplay()
                self._display_key = None
                return
            self._display = open_display(settings) if key is not None else NullDisplay()
            self._display_key = key
        if self._hold_display or lines is None:
            return
        self._display.write(lines)

    def _close_display(self) -> None:
        self._display.clear()
        self._display.close()
        self._display = NullDisplay()
        self._display_key = None

    def _calibration_lcd(self) -> dict:
        """Which calibration step the panel should name, and the number for it."""

        if self._mode == "cooling" and self._display_coast is not None:
            return {"peak": self._display_coast, "rate": self._display_rate}
        if self._run_kind != "calibration" or self._mode not in ("running", "paused"):
            return {}
        watch = self._coast_watch
        if watch is None:
            return {"step": "heating"}
        opened = float(watch["temp_at_open"])
        peak_average = watch.get("peak_average")
        if watch.get("phase") == "falling":
            peak = self._coast_f
            if peak is None and peak_average is not None:
                peak = peak_average - opened
            return {"step": "falling", "peak": peak, "rate": watch.get("rate")}
        rise = None
        if peak_average is not None:
            rise = peak_average - opened
        elif watch["samples"]:
            rise = float(watch["samples"][-1][1]) - opened
        return {"step": "coasting", "rise": rise}

    def _panel_lines(self, settings: Settings) -> list[str]:
        probes: list[tuple[str, float | None]] = []
        for index, channel in enumerate(settings.sensors):
            if not channel.enabled or index >= len(self._live_temps):
                continue
            value = self._live_temps[index]
            probes.append((channel.name, None if value is None else float(value)))
        heaters = [
            (
                channel.enabled,
                bool(self._heaters[index]) if index < len(self._heaters) else False,
            )
            for index, channel in enumerate(settings.heaters)
        ]
        elapsed = self._cooldown_elapsed if self._mode == "cooling" else self._elapsed
        code = self._emergency[0] if self._emergency else None
        lcd = self._calibration_lcd()
        forecast = self._forecast(settings)
        if forecast["seconds_to_target"] is not None:
            eta_seconds: float | None = forecast["seconds_to_target"]
            eta_kind: str | None = "target"
        elif forecast["seconds_to_cooldown"] is not None:
            eta_seconds = forecast["seconds_to_cooldown"]
            eta_kind = "cool"
        else:
            eta_seconds = None
            eta_kind = None
        return status_lines(
            mode=self._mode,
            elapsed_seconds=elapsed,
            target_f=self._target_value(settings),
            probes=probes,
            heaters=heaters,
            calibrating=self._run_kind == "calibration" and self._mode in ("running", "paused"),
            needs_calibration=settings.learned_coast_f is None and self._mode == "idle",
            emergency_code=code,
            cabinet_temp_f=self._cabinet_temp,
            cabinet_limit_f=settings.cabinet_limit_f,
            cabinet_fault=code == "E02",
            calibration_step=lcd.get("step"),
            calibration_rise_f=lcd.get("rise"),
            calibration_peak_f=lcd.get("peak"),
            calibration_rate_f=lcd.get("rate"),
            eta_seconds=eta_seconds,
            eta_kind=eta_kind,
        )

    def _loop(self) -> None:
        last = time.monotonic()
        since_control = 1.0
        while not self._stop_loop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            since_control += dt
            try:
                moved = self._apply_panel()
            except Exception:
                logger.exception("Panel update failed")
                moved = False
            if self._dial_dirty and now - self._dial_moved_at >= 0.15:
                try:
                    self._flush_dial_target()
                except Exception:
                    logger.exception("Could not save the target")
            if since_control >= 1.0:
                try:
                    self.tick(since_control)
                except Exception:
                    logger.exception("Controller tick failed")
                since_control = 0.0
                try:
                    self._refresh_display()
                except Exception:
                    logger.exception("LCD refresh failed")
            # While the knob is moving, paint the next target immediately.
            # Sleeping here is what left the number trailing the dial.
            if not moved:
                self._stop_loop.wait(0.02)

    def _tick(self, dt: float) -> None:
        settings = self.store.get_settings()
        stepper = getattr(self.source, "step", None)
        if stepper is not None:
            stepper(dt)
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        self._sample_climate()
        self._emergency = self._emergency_codes(settings)
        if self._mode in ("running", "paused", "cooling"):
            if self._current_control(settings) is None:
                self._predict_samples.clear()
            else:
                self._note_predict(dt)
        if self._emergency:
            if self._mode in ("running", "paused"):
                self._finish_run()
            self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
            return
        if self._mode != "running":
            self._apply_idle_outputs(settings)
            if self._mode == "cooling":
                self._tick_cooldown(dt, settings)
            elif self._mode == "paused":
                self._advance_cooldown_clock(dt, settings)
            return
        self._elapsed += dt
        self._note_heater_dwell(dt)
        self._command_heaters(settings)
        if self._mode != "running":
            return
        self._advance_coast(settings)
        if self._mode != "running":
            return
        self._advance_cooldown_clock(dt, settings)
        self._since_sample += dt
        while self._since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._since_sample -= SAMPLE_INTERVAL_SECONDS
            self._record_sample(settings)
            self.store.update_run(self._run_id, elapsed_seconds=self._elapsed)

    def _finish_run(self) -> None:
        settings = self.store.get_settings()
        if self._run_kind == "calibration" and self._coast_f is not None:
            self._display_coast = self._coast_f
            watch = self._coast_watch or {}
            self._display_rate = watch.get("saved_rate")
        self._coast_watch = None
        self._pulse_until = None
        self._hold_ready = False
        self._flush_heater_life()
        self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        if self._run_id is not None:
            self._record_sample(settings)
            self.store.update_run(
                self._run_id,
                status="stopped",
                elapsed_seconds=self._elapsed,
                ended_at=utc_now(),
            )
            logger.info("Run %s stopped at %ss", self._run_id, round(self._elapsed, 1))
            self._begin_cooldown(self._run_id)
        self._run_id = None
        self._elapsed = 0.0
        self._since_sample = 0.0
        if self._mode != "cooling":
            self._mode = "idle"

    def _begin_cooldown(self, run_id: int) -> None:
        settings = self.store.get_settings()
        if self._cooldown_run_id is None:
            self._advance_cooldown_clock(0, settings)
        if self._cooldown_run_id is None:
            return
        self._mode = "cooling"
        self._cooldown_window = []
        current = self._cooldown_temp()
        if current is not None:
            self._push_cooldown_temp(current)
        self._record_cooldown_sample(settings)
        self._persist_cooldown()
        logger.info("Run %s cooling; cooldown clock at %ss", run_id, round(self._cooldown_elapsed, 1))

    def _advance_cooldown_clock(self, dt: float, settings: Settings) -> None:
        """Count time after the control probe first reaches the target.

        The lower stop is not decided here. Holding at the target, or pausing,
        still belongs to the cooldown, but the temperature can only "stop"
        once the run is over and the heaters are off.
        """

        if self._mode == "cooling" or self._run_id is None:
            return
        if self._cooldown_run_id is None:
            found = self._current_control(settings)
            if found is None:
                return
            index, temp = found
            if temp < settings.target_temp_f:
                return
            self._cooldown_run_id = self._run_id
            self._cooldown_sensor = index
            self._cooldown_elapsed = 0.0
            self._cooldown_since_sample = 0.0
            self.store.update_run(
                self._run_id,
                cooldown_status="open",
                cooldown_seconds=0,
                cooldown_baseline_f=settings.target_temp_f,
                cooldown_sensor_index=index,
            )
            logger.info(
                "Run %s reached %s°F; cooldown timing started",
                self._run_id,
                settings.target_temp_f,
            )
        self._cooldown_elapsed += dt
        self._cooldown_since_sample += dt
        while self._cooldown_since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._cooldown_since_sample -= SAMPLE_INTERVAL_SECONDS
            self._persist_cooldown()

    def _adopt_cooldown(self, run: dict) -> None:
        sensor = run.get("cooldown_sensor_index")
        if sensor is None:
            self.store.update_run(int(run["id"]), cooldown_status="abandoned")
            return
        self._mode = "cooling"
        self._cooldown_run_id = int(run["id"])
        self._cooldown_elapsed = float(run.get("cooldown_seconds") or 0)
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor = int(sensor)
        self._cooldown_window = []

    def _tick_cooldown(self, dt: float, settings: Settings) -> None:
        if self._cooldown_run_id is None or self._cooldown_sensor is None:
            self._clear_cooldown()
            return
        self._cooldown_elapsed += dt
        self._cooldown_since_sample += dt
        current = self._cooldown_temp()
        if current is not None:
            self._push_cooldown_temp(current)
            if self._cooldown_settled():
                self._complete_cooldown(settings)
                return
        while self._cooldown_since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._cooldown_since_sample -= SAMPLE_INTERVAL_SECONDS
            self._record_cooldown_sample(settings)
        self._persist_cooldown()

    def _push_cooldown_temp(self, temp: float) -> None:
        self._cooldown_window.append((self._cooldown_elapsed, temp))
        cutoff = self._cooldown_elapsed - COOLDOWN_STABLE_SECONDS
        while len(self._cooldown_window) >= 2 and self._cooldown_window[1][0] <= cutoff:
            self._cooldown_window.pop(0)

    def _cooldown_settled(self) -> bool:
        window = self._cooldown_window
        if len(window) < 2:
            return False
        if window[-1][0] - window[0][0] < COOLDOWN_STABLE_SECONDS:
            return False
        temps = [temp for _, temp in window]
        return max(temps) - min(temps) <= COOLDOWN_BAND_F

    def _complete_cooldown(self, settings: Settings) -> None:
        run_id = self._cooldown_run_id
        elapsed = self._cooldown_elapsed
        self._record_cooldown_sample(settings)
        self.store.update_run(
            run_id,
            cooldown_status="complete",
            cooldown_seconds=round(elapsed, 2),
            cooldown_ended_at=utc_now(),
        )
        logger.info("Run %s cooled in %ss", run_id, round(elapsed, 1))
        self._clear_cooldown()

    def _abandon_cooldown(self) -> None:
        if self._cooldown_run_id is None:
            self._clear_cooldown()
            return
        run_id = self._cooldown_run_id
        self.store.update_run(
            run_id,
            cooldown_status="abandoned",
            cooldown_seconds=round(self._cooldown_elapsed, 2),
        )
        logger.info("Run %s cooldown abandoned at %ss", run_id, round(self._cooldown_elapsed, 1))
        self._clear_cooldown()

    def _persist_cooldown(self) -> None:
        if self._cooldown_run_id is None:
            return
        self.store.update_run(
            self._cooldown_run_id,
            cooldown_seconds=round(self._cooldown_elapsed, 2),
        )

    def _record_cooldown_sample(self, settings: Settings) -> None:
        if self._cooldown_run_id is None:
            return
        temps = self._sample_temps()
        self.store.add_sample(
            run_id=self._cooldown_run_id,
            elapsed_seconds=round(self._cooldown_elapsed, 2),
            temps_f=temps,
            heaters_on=[False] * SLOT_COUNT,
            target_temp_f=settings.target_temp_f,
            phase="cooldown",
        )
        self._log_forecast(settings)

    def _clear_cooldown(self) -> None:
        self._cooldown_run_id = None
        self._cooldown_elapsed = 0.0
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor = None
        self._cooldown_window = []
        self._display_coast = None
        self._display_rate = None
        if self._mode == "cooling":
            self._mode = "idle"

    def _cooldown_temp(self) -> float | None:
        index = self._cooldown_sensor
        if index is None or index >= len(self._live_temps):
            return None
        return float(self._live_temps[index])

    def _cooldown_overlay(self) -> dict | None:
        if self._mode != "cooling" or self._cooldown_run_id is None:
            return None
        return {
            "id": self._cooldown_run_id,
            "cooldown_seconds": self._cooldown_elapsed,
            "cooldown_status": "open",
        }

    def _apply_idle_outputs(self, settings: Settings) -> None:
        if self._mode == "running":
            return
        self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)

    def _note_heater_dwell(self, dt: float) -> None:
        for index in range(SLOT_COUNT):
            if self._heaters[index]:
                self._heater_on_for[index] += dt
                self._heater_life_pending[index] += dt
            else:
                self._heater_off_for[index] += dt
        # A crash should not drop more than a few seconds of element life.
        if max(self._heater_life_pending) >= 5.0:
            self._flush_heater_life()

    def _flush_heater_life(self) -> None:
        pending = self._heater_life_pending
        if not any(pending):
            return
        self.store.add_heater_life(pending)
        self._heater_life_pending = [0.0] * SLOT_COUNT

    def _life_seconds(self) -> list[float]:
        stored = self.store.heater_life_seconds()
        return [
            round(stored[index] + self._heater_life_pending[index], 2)
            for index in range(SLOT_COUNT)
        ]

    def _command_heaters(self, settings: Settings) -> None:
        """Apply the temperature decision, then the minimum on and off times.

        The dwells are longer than an AC cycle, so a zero-crossing SSR is
        not asked to change state on every reading. Pause and Stop skip the
        dwell and open the SSR immediately.
        """

        before = list(self._heaters)
        on_for = list(self._heater_on_for)
        desired = self._decide(settings, self._live_temps)
        min_on = settings.heater_min_on_seconds
        min_off = settings.heater_min_off_seconds
        states = []
        for index in range(SLOT_COUNT):
            enabled = settings.heaters[index].enabled
            if not enabled:
                states.append(False)
                continue
            # Finish an on-dwell even if the probe has already reached the
            # early cutoff. The matching off-dwell blocks the turn back on.
            if self._heaters[index] and not desired[index] and self._heater_on_for[index] < min_on:
                states.append(True)
                continue
            if (
                not self._heaters[index]
                and desired[index]
                and self._heater_off_for[index] < min_off
            ):
                states.append(False)
                continue
            states.append(bool(desired[index]))
        self._set_heaters(states)
        self._note_coast_open(settings, before, on_for)

    def _set_heaters(self, states: list[bool], *, release_off_dwell: bool = False) -> None:
        requested = [bool(on) for on in states[:SLOT_COUNT]]
        while len(requested) < SLOT_COUNT:
            requested.append(False)
        previous = list(self._heaters)
        while len(previous) < SLOT_COUNT:
            previous.append(False)
        self._heaters = requested
        for index, on in enumerate(self._heaters):
            if on:
                self._heater_off_for[index] = math.inf
            else:
                self._heater_on_for[index] = 0.0
                # Only the transition into off starts the dwell. Holding an
                # already-open SSR does not reset the timer. Pause and Stop
                # release it so the next Start or Resume is not delayed.
                if previous[index] or release_off_dwell:
                    self._heater_off_for[index] = math.inf if release_off_dwell else 0.0
        self.source.set_heaters(self._heaters)

    def _note_coast_open(self, settings: Settings, before: list[bool], on_for: list[float]) -> None:
        """Start the coast watch when a soaked calibration opens the SSR."""

        if self._run_kind != "calibration" or self._mode != "running":
            return
        was_on = any(
            before[index] and settings.heaters[index].enabled for index in range(SLOT_COUNT)
        )
        now_on = any(
            self._heaters[index] and settings.heaters[index].enabled for index in range(SLOT_COUNT)
        )
        if was_on and not now_on:
            soaked = max(
                (on_for[index] for index in range(SLOT_COUNT) if before[index]),
                default=0.0,
            )
            found = self._current_control(settings)
            if soaked < COAST_SOAK_SECONDS or found is None:
                return
            _, temp = found
            self._coast_watch = {
                "temp_at_open": temp,
                "opened_elapsed": self._elapsed,
                "samples": [],
                "phase": "coasting",
                "peak_average": None,
                "peak_elapsed": None,
                "plateau_anchor": None,
                "plateau_since": None,
                "below_since": None,
                "fall_samples": [],
                "saved_rate": None,
            }

    def _advance_coast(self, settings: Settings) -> None:
        watch = self._coast_watch
        if watch is None:
            return
        found = self._current_control(settings)
        if found is None:
            if (
                watch["phase"] == "coasting"
                and self._elapsed - watch["opened_elapsed"] >= COAST_WATCH_MAX_SECONDS
            ):
                self._finish_calibration(settings, rate=None)
            return
        _, temp = found
        samples: list[tuple[float, float]] = watch["samples"]
        samples.append((self._elapsed, temp))
        if self._elapsed - samples[0][0] < COAST_AVERAGE_SECONDS:
            return
        window_start = self._elapsed - COAST_AVERAGE_SECONDS
        window = [reading for elapsed, reading in samples if elapsed >= window_start]
        if not window:
            return
        average = sum(window) / len(window)
        watch["average"] = average
        if watch["phase"] == "coasting":
            self._track_peak(watch, average)
            if self._peak_ready(watch):
                self._lock_coast(settings, watch)
                watch["phase"] = "falling"
                watch["fall_samples"] = [(self._elapsed, average)]
            return
        rate, done = _cooling_progress(watch["fall_samples"], self._elapsed, average)
        if rate is not None:
            watch["rate"] = rate
        if done:
            self._finish_calibration(settings, rate=rate)

    def _track_peak(self, watch: dict, average: float) -> None:
        peak = watch["peak_average"]
        if peak is None or average > peak + 0.01:
            watch["peak_average"] = average
            watch["peak_elapsed"] = self._elapsed
            watch["below_since"] = None
            peak = average
        if watch["plateau_anchor"] is None:
            watch["plateau_anchor"] = average
            watch["plateau_since"] = self._elapsed
        elif average >= watch["plateau_anchor"] + COAST_PLATEAU_RISE_F:
            watch["plateau_anchor"] = average
            watch["plateau_since"] = self._elapsed
        if average <= peak - COAST_PEAK_DROP_F:
            if watch["below_since"] is None:
                watch["below_since"] = self._elapsed
        else:
            watch["below_since"] = None

    def _peak_ready(self, watch: dict) -> bool:
        if watch["peak_average"] is None:
            return False
        below = watch["below_since"]
        if below is not None and self._elapsed - below >= COAST_PEAK_CONFIRM_SECONDS:
            return True
        since = watch["plateau_since"]
        if since is not None and self._elapsed - since >= COAST_PLATEAU_SECONDS:
            return True
        return self._elapsed - watch["opened_elapsed"] >= COAST_WATCH_MAX_SECONDS

    def _lock_coast(self, settings: Settings, watch: dict) -> None:
        if self._run_id is None or watch.get("locked"):
            return
        peak = float(watch["peak_average"])
        coast = min(EARLY_CUTOFF_MAX_F, max(0.0, round(peak - watch["temp_at_open"], 2)))
        seconds = round(max(0.0, float(watch["peak_elapsed"]) - watch["opened_elapsed"]), 2)
        settled = peak >= settings.target_temp_f - CALIBRATION_LAND_F
        watch["locked"] = True
        self._coast_f = coast
        self._coast_seconds = seconds
        self._calibration_settled = settled
        learned = replace(
            settings,
            learned_coast_f=coast,
            learned_coast_seconds=seconds,
            learned_cooldown_f_per_min=None,
            charge_gallons=self._charge_gallons,
        )
        self.store.save_settings(learned)
        self.store.update_run(
            self._run_id,
            coast_f=coast,
            coast_seconds=seconds,
            calibration_settled=1 if settled else 0,
        )
        logger.info("Run %s coast %s°F over %ss", self._run_id, coast, seconds)

    def _finish_calibration(self, settings: Settings, rate: float | None) -> None:
        watch = self._coast_watch
        if watch is not None:
            watch["saved_rate"] = None if rate is None else round(rate, 2)
        if rate is not None and self._run_id is not None:
            stored = min(COOLDOWN_RATE_MAX, max(0.0, round(rate, 2)))
            current = self.store.get_settings()
            self.store.save_settings(replace(current, learned_cooldown_f_per_min=stored))
            self.store.update_run(self._run_id, cooldown_f_per_min=stored)
            logger.info("Run %s cooling %s°F/min", self._run_id, stored)
        self._finish_run()

    def _clear_coast(self) -> None:
        self._run_kind = "run"
        self._charge_gallons = None
        self._coast_watch = None
        self._coast_f = None
        self._coast_seconds = None
        self._calibration_settled = False
        self._pulse_until = None
        self._hold_ready = False

    def _clear_predict(self) -> None:
        self._predict_clock = 0.0
        self._predict_samples.clear()

    def _note_predict(self, dt: float) -> None:
        """Append the control probe and drop readings older than the window."""

        if dt <= 0:
            return
        self._predict_clock += dt
        found = self._current_control(self.store.get_settings())
        if found is None:
            self._predict_samples.clear()
            return
        self._predict_samples.append((self._predict_clock, found[1]))
        horizon = self._predict_clock - PREDICT_WINDOW_SECONDS
        while len(self._predict_samples) >= 2 and self._predict_samples[1][0] <= horizon:
            self._predict_samples.pop(0)

    def _shutoff_coast(self, settings: Settings) -> float:
        """Degrees early to open the SSR on a normal run.

        Calibration does not use this. A short window leaves the learned coast
        unchanged so one jumped reading cannot move the shutoff.
        """

        return predicted_coast_f(
            settings.learned_coast_f,
            settings.learned_coast_seconds,
            slope_f_per_sec(self._predict_samples),
        )

    def _forecast(self, settings: Settings) -> dict[str, float | None]:
        """Time to the target while rising, and time to cool while falling."""

        empty: dict[str, float | None] = {"seconds_to_target": None, "seconds_to_cooldown": None}
        if self._mode not in ("running", "paused", "cooling"):
            return empty
        found = self._current_control(settings)
        if found is None:
            return empty
        _, temp = found
        rate = slope_f_per_sec(self._predict_samples)
        coast = self._shutoff_coast(settings)
        heating = any(
            self._heaters[index] and settings.heaters[index].enabled for index in range(SLOT_COUNT)
        )
        rising = (
            rate is not None
            and rate >= PREDICT_MIN_RATE_F_PER_SEC
            and temp < float(settings.target_temp_f)
        )
        if heating or rising:
            return {
                "seconds_to_target": _forecast_seconds(
                    seconds_to_target(
                        temp,
                        float(settings.target_temp_f),
                        coast,
                        rate,
                        settings.learned_coast_f,
                        settings.learned_coast_seconds,
                    )
                ),
                "seconds_to_cooldown": None,
            }
        return {
            "seconds_to_target": None,
            "seconds_to_cooldown": _forecast_seconds(
                seconds_to_cooldown(
                    temp,
                    rate,
                    learned_rate_f_per_min=settings.learned_cooldown_f_per_min,
                )
            ),
        }

    def _log_forecast(self, settings: Settings) -> None:
        forecast = self._forecast(settings)
        target = forecast["seconds_to_target"]
        cool = forecast["seconds_to_cooldown"]
        if target is None and cool is None:
            return
        logger.info(
            "Temperature forecast: %s to target, %s to cool",
            "n/a" if target is None else f"{target:.0f}s",
            "n/a" if cool is None else f"{cool:.0f}s",
        )

    def _hold_pulse(self, settings: Settings) -> tuple[float, float] | None:
        """Pulse length and reheat temperature, when a cooling rate can hold the target."""

        coast = settings.learned_coast_f
        seconds = settings.learned_coast_seconds
        rate = settings.learned_cooldown_f_per_min
        if coast is None or seconds is None or rate is None or rate <= 0:
            return None
        duration = sustain_pulse_seconds(coast, seconds)
        if duration is None:
            return None
        reheat = reheat_temperature(settings.target_temp_f, rate, duration)
        coast = settings.learned_coast_f or 0.0
        if reheat <= settings.target_temp_f - coast:
            return None
        return duration, reheat

    def _sustain_states(self, settings: Settings, primary: float, pulse: tuple[float, float]) -> list[bool]:
        """Approach on the learned coast, then pulse only after the pot has arrived.

        The climb turns the elements off at the early cutoff and leaves them
        off while stored heat finishes the rise. A pulse starts only after
        the probe has already been above the reheat point and then sagged
        back to it. Pulsing as soon as the cutoff is crossed heats the rest
        of the way to the target, and the learned coast then overshoots it.
        """

        duration, reheat = pulse
        target = float(settings.target_temp_f)
        enabled = [settings.heaters[index].enabled for index in range(SLOT_COUNT)]
        coast_f = self._shutoff_coast(settings)
        coast_line = target - coast_f
        if primary <= coast_line:
            # Below the coast, including a deep sag, this is a fresh approach.
            self._pulse_until = None
            self._hold_ready = False
            return [
                heater_should_be_on(
                    primary,
                    settings.target_temp_f,
                    bool(self._heaters[index]),
                    early_cutoff_f=coast_f,
                )
                and enabled[index]
                for index in range(SLOT_COUNT)
            ]
        if primary >= target:
            self._pulse_until = None
            self._hold_ready = True
            return [False] * SLOT_COUNT
        if self._pulse_until is not None and self._elapsed < self._pulse_until:
            return list(enabled)
        self._pulse_until = None
        if primary > reheat:
            self._hold_ready = True
            return [False] * SLOT_COUNT
        if not self._hold_ready:
            return [False] * SLOT_COUNT
        blocked = any(
            enabled[index]
            and not self._heaters[index]
            and self._heater_off_for[index] < settings.heater_min_off_seconds
            for index in range(SLOT_COUNT)
        )
        if not blocked:
            self._pulse_until = self._elapsed + duration
        return list(enabled)

    def _decide(self, settings: Settings, temps: list[float]) -> list[bool]:
        """Physical heater mask. Index 0 is heater 1. Disabled slots stay off.

        The on/off choice itself, including why power is removed before the
        setpoint, lives in :func:`heater_should_be_on`. The degrees early are
        the learned coast, widened when the live rise is faster. Once a
        cooling rate is known, the climb still shuts off at that coast. A
        short pulse replaces the deadband only after that coast has already
        carried the probe up to the target and it sags back.
        """

        off = [False] * SLOT_COUNT
        control_indexes = settings.enabled_sensor_indexes()
        if not control_indexes or control_indexes[0] >= len(temps):
            return off
        # The coast watch is the measurement. Leave the SSR open through the
        # crest and the cooling-rate watch so a second heat pulse does not
        # interrupt it.
        if self._run_kind == "calibration" and self._coast_watch is not None:
            return off
        raw = temps[control_indexes[0]]
        if raw is None:
            return off
        primary = float(raw)
        current = list(self._heaters[:SLOT_COUNT])
        while len(current) < SLOT_COUNT:
            current.append(False)
        # A normal run stops at the learned coast, or sooner when the rise is
        # fast. Calibration heats until the probe is a little above the target,
        # so the still can actually reach it. Prediction stays out of that watch.
        if self._run_kind == "calibration":
            cutoff = -CALIBRATION_ABOVE_F
        elif (pulse := self._hold_pulse(settings)) is not None:
            return self._sustain_states(settings, primary, pulse)
        else:
            cutoff = self._shutoff_coast(settings)
        return [
            heater_should_be_on(
                primary,
                settings.target_temp_f,
                bool(current[index]),
                early_cutoff_f=cutoff,
            )
            and settings.heaters[index].enabled
            for index in range(SLOT_COUNT)
        ]

    def _record_sample(self, settings: Settings) -> None:
        if self._run_id is None:
            return
        # Samples stay aligned to the physical slots. A disabled heater is off.
        temps = self._sample_temps()
        heaters = [False] * SLOT_COUNT
        for index, on in enumerate(self._heaters[:SLOT_COUNT]):
            heaters[index] = bool(on) and settings.heaters[index].enabled
        self.store.add_sample(
            run_id=self._run_id,
            elapsed_seconds=round(self._elapsed, 2),
            temps_f=temps,
            heaters_on=heaters,
            target_temp_f=settings.target_temp_f,
        )
        self._log_forecast(settings)

    def _live_overlay(self) -> dict | None:
        if self._run_id is None or self._mode == "idle":
            return None
        return {
            "id": self._run_id,
            "elapsed_seconds": self._elapsed,
            "status": self._mode,
        }

    def _status(self) -> dict:
        settings = self.store.get_settings()
        sensor_indexes = settings.enabled_sensor_indexes()
        heater_indexes = settings.enabled_heater_indexes()
        temps = []
        for index in sensor_indexes:
            if index >= len(self._live_temps):
                continue
            value = self._live_temps[index]
            temps.append(None if value is None else round(float(value), 1))
        heaters = [
            bool(self._heaters[index])
            for index in heater_indexes
            if index < len(self._heaters)
        ]
        life = self._life_seconds()
        return {
            "mode": self._mode,
            "source": getattr(self.source, "name", "unknown"),
            "elapsed_seconds": round(
                self._cooldown_elapsed if self._mode == "cooling" else self._elapsed,
                1,
            ),
            "target_temp_f": self._target_value(settings),
            **self._forecast(settings),
            "learned_coast_f": settings.learned_coast_f,
            "learned_coast_seconds": settings.learned_coast_seconds,
            "learned_cooldown_f_per_min": settings.learned_cooldown_f_per_min,
            "calibration_step": self._calibration_lcd().get("step"),
            "charge_gallons": self._charge_gallons
            if self._mode in ("running", "paused") and self._run_kind == "calibration"
            else settings.charge_gallons,
            "run_kind": self._run_kind if self._mode in ("running", "paused") else None,
            "coast_f": self._coast_f if self._mode in ("running", "paused") else None,
            "coast_seconds": self._coast_seconds if self._mode in ("running", "paused") else None,
            "calibration_settled": self._calibration_settled
            if self._mode in ("running", "paused")
            else False,
            "temps_f": temps,
            "sensor_names": [settings.sensors[index].name for index in sensor_indexes],
            "heaters_on": heaters,
            "heater_names": [settings.heaters[index].name for index in heater_indexes],
            "heater_indexes": heater_indexes,
            "heater_life_seconds": life,
            "heater_count": len(heater_indexes),
            "sensor_count": len(sensor_indexes),
            "run_id": self._run_id,
            "cooldown_run_id": self._cooldown_run_id if self._mode == "cooling" else None,
            "settings_locked": self._mode in ("running", "paused"),
            "cabinet_temp_f": None if self._cabinet_temp is None else round(self._cabinet_temp, 1),
            "cabinet_humidity_pct": None
            if self._cabinet_humidity is None
            else round(self._cabinet_humidity, 1),
            "cabinet_limit_f": settings.cabinet_limit_f,
            "cabinet_enabled": self._hts_enabled(settings),
            "emergency_codes": list(self._emergency),
            "emergency": [{"code": code, "summary": summary(code)} for code in self._emergency],
        }


    def _current_control(self, settings: Settings) -> tuple[int, float] | None:
        indexes = settings.enabled_sensor_indexes()
        if not indexes or indexes[0] >= len(self._live_temps):
            return None
        index = indexes[0]
        value = self._live_temps[index]
        if value is None:
            return None
        return index, float(value)

    def nudge_target(self, steps: int) -> bool:
        """Move the target by whole degrees and paint it before saving.

        The database write happens once the knob stops. Steps past the dial
        ends are ignored. Returns whether the shown target changed.
        """

        steps = int(steps)
        if steps == 0:
            return False
        with self._lock:
            current = self._target_value(self.store.get_settings())
            target = min(int(TARGET_MAX_F), max(int(TARGET_MIN_F), int(current) + steps))
            if target == int(current):
                return False
            self._dial_target = target
            self._dial_dirty = True
            self._dial_moved_at = time.monotonic()
        self._refresh_display()
        return True

    def _target_value(self, settings: Settings) -> int:
        if self._dial_target is not None:
            return self._dial_target
        return int(settings.target_temp_f)

    def _write_pending_target(self) -> None:
        """Caller holds ``self._lock``. Store a dial change that is only on screen."""

        if not self._dial_dirty or self._dial_target is None:
            return
        current = self.store.get_settings()
        if int(current.target_temp_f) != self._dial_target:
            updated = merge_settings(current, {"target_temp_f": self._dial_target})
            self.store.save_settings(updated)
        self._dial_dirty = False
        self._dial_target = None

    def _flush_dial_target(self) -> None:
        with self._lock:
            self._write_pending_target()

    def _sample_temps(self) -> list[float | None]:
        return [
            None if temp is None else round(float(temp), 2) for temp in self._live_temps[:SLOT_COUNT]
        ]

    def _sample_climate(self) -> None:
        reader = getattr(self.source, "read_climate", None)
        if reader is None:
            # A test double with no cabinet sensor is not a failed HTS221.
            self._cabinet_temp = None
            self._cabinet_humidity = None
            self._climate_fault = False
            return
        try:
            temp, humidity = reader()
        except Exception:
            logger.exception("Cabinet sensor read failed")
            self._cabinet_temp = None
            self._cabinet_humidity = None
            self._climate_fault = True
            return
        self._cabinet_temp = None if temp is None else float(temp)
        self._cabinet_humidity = None if humidity is None else float(humidity)
        self._climate_fault = bool(getattr(self.source, "climate_fault", False)) or temp is None

    def _hts_enabled(self, settings: Settings) -> bool:
        gpio = settings.gpio or default_gpio()
        return any(device.id == "hts221" and device.enabled for device in gpio.i2c_devices)

    def _emergency_codes(self, settings: Settings) -> list[str]:
        codes: list[str] = []
        if self._hts_enabled(settings) and hasattr(self.source, "read_climate"):
            if self._climate_fault or self._cabinet_temp is None:
                codes.append("E02")
            elif self._cabinet_temp > settings.cabinet_limit_f:
                codes.append("E01")
        spi_fault = bool(getattr(self.source, "spi_fault", False))
        faults = list(getattr(self.source, "probe_faults", []) or [])
        if spi_fault:
            codes.append("E04")
        indexes = settings.enabled_sensor_indexes()
        control = indexes[0] if indexes else 0
        temp = self._live_temps[control] if control < len(self._live_temps) else None
        probe_fault = bool(faults[control]) if control < len(faults) else False
        if spi_fault or probe_fault or temp is None:
            codes.append("E03")
        return codes

    def _apply_panel(self) -> bool:
        dismissed = self._dismiss_blocked_menu()
        painted = self._paint_finished_check()
        panel = self._panel
        if panel is None:
            return dismissed or painted
        starts, stops, holds, steps = panel.take()  # type: ignore[attr-defined]
        if starts and stops:
            # Both pins moved. In a run, Stop is the one that matters.
            # Otherwise it was Start, and the other pin followed the ground.
            with self._lock:
                running = self._mode in ("running", "paused")
            if running:
                starts = 0
            else:
                stops = 0
        if self._menu is not None:
            return self._apply_menu(starts, stops, steps) or dismissed or painted
        if holds and self._open_menu():
            # This batch opened the menu. Do not also move, select, or stop.
            self._refresh_display()
            return True
        moved = False
        if steps:
            try:
                moved = self.nudge_target(steps)
            except ControllerError:
                logger.debug("Target dial ignored", exc_info=True)
        if stops:
            try:
                self.stop()
            except ControllerError:
                return moved or dismissed or painted
            return moved or dismissed or painted
        if not starts:
            return moved or dismissed or painted
        try:
            snapshot = self.status()
            if snapshot["emergency_codes"]:
                return moved or dismissed or painted
            if snapshot["mode"] == "paused":
                self.resume()
            elif snapshot["mode"] in ("idle", "cooling"):
                if snapshot["learned_coast_f"] is None:
                    self.start({"kind": "calibration"})
                else:
                    self.start()
        except ControllerError:
            return moved or dismissed or painted
        return moved or dismissed or painted

    def _menu_blocked(self) -> bool:
        """Caller holds ``self._lock``. True when the menu must not stay open."""

        return self._mode not in ("idle", "cooling")

    def _drop_menu_if_blocked(self) -> None:
        """Caller holds ``self._lock``."""

        if self._menu is None or not self._menu_blocked():
            return
        self._menu = None
        self._menu_generation += 1

    def _dismiss_blocked_menu(self) -> bool:
        with self._lock:
            if self._menu is None or not self._menu_blocked():
                return False
            self._drop_menu_if_blocked()
        self._refresh_display()
        return True

    def _paint_finished_check(self) -> bool:
        """Draw a release check that finished on the background thread."""

        with self._lock:
            if not self._menu_dirty:
                return False
            self._menu_dirty = False
        self._refresh_display()
        return True

    def _open_menu(self) -> bool:
        with self._lock:
            if self._menu_blocked():
                return False
            self._menu = SoftwareMenu()
            self._menu_generation += 1
        return True

    def _apply_menu(self, starts: int, stops: int, steps: int) -> bool:
        action: str | None = None
        generation = self._menu_generation
        close = False
        changed = False
        with self._lock:
            menu = self._menu
            if menu is None:
                return False
            if stops:
                if menu.screen == "checking":
                    self._menu_generation += 1
                if not menu.back():
                    self._menu = None
                    close = True
                changed = True
            if not close and steps:
                changed = menu.move(steps) or changed
            if not close and starts and not stops:
                action = menu.select()
                if action == "check":
                    self._menu_generation += 1
                    generation = self._menu_generation
                changed = True
        if changed:
            self._refresh_display()
        if action == "check":
            self._start_menu_check(generation)
        elif action == "apply":
            self._install_from_menu()
        elif action == "reboot":
            self._reboot_from_menu()
        return changed

    def _start_menu_check(self, generation: int) -> None:
        thread = threading.Thread(
            target=self._finish_menu_check,
            args=(generation,),
            name="brewski-menu-update",
            daemon=True,
        )
        self._menu_check_thread = thread
        thread.start()

    def _finish_menu_check(self, generation: int) -> None:
        try:
            release = check_release()
            can_apply = apply_supported()
        except Exception:
            logger.exception("Update check failed")
            release = ReleaseInfo(current=__version__, latest=None, available=False, reachable=False)
            can_apply = False
        with self._lock:
            menu = self._menu
            if menu is None or generation != self._menu_generation or menu.screen != "checking":
                return
            menu.show_release(release, can_apply)
            self._menu_dirty = True

    def _install_from_menu(self) -> None:
        try:
            self.start_update()
        except ControllerError:
            logger.warning("Could not start the update from the menu")
            self._refresh_display()
            return
        with self._lock:
            self._menu = None
            self._menu_generation += 1

    def _reboot_from_menu(self) -> None:
        try:
            request_reboot()
        except (OSError, subprocess.CalledProcessError):
            logger.warning("Reboot was refused")
            with self._lock:
                menu = self._menu
                if menu is not None:
                    menu.fail_reboot()
            self._refresh_display()

    def _sync_panel(self) -> None:
        settings = self.store.get_settings()
        key = panel_key(settings)
        if key == self._panel_key:
            return
        self._close_panel()
        self._panel_key = key
        if key is None:
            return
        self._panel = open_panel(settings)

    def _close_panel(self) -> None:
        panel = self._panel
        self._panel = None
        self._panel_key = None
        if panel is not None:
            panel.close()  # type: ignore[attr-defined]
