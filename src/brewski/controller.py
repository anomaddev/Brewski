"""Run state machine.

The loop is hardware-agnostic. It reads a :class:`HardwareSource`, decides
whether the heaters should be on, and appends samples while a run is going.
The first enabled probe is the one compared with the target. Heaters turn
off before that target so heat still stored in the element can finish the
rise. Disabled heaters stay off. Heater and probe pins are stored for a
later Raspberry Pi source. After setup, this loop does drive the I2C LCD.
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
from dataclasses import replace

from brewski.lcd import NullDisplay, display_key, open_display, status_lines
from brewski.sources.base import HardwareSource
from brewski.updates import read_status
from brewski.updates import report as update_report
from brewski.updates import start_apply_service, write_status
from brewski.store import (
    EARLY_CUTOFF_MAX_F,
    SLOT_COUNT,
    Settings,
    SettingsError,
    Store,
    charge_gallons,
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
# control probe. The run ends once that average sits inside the drift band.
COAST_AVERAGE_SECONDS = 5.0
COAST_AVERAGE_HOLD_SECONDS = 4.0
COAST_AVERAGE_DRIFT_F = 0.25
# Reached the target when the settled average is at least this close to it.
# Above the target counts: that overshoot is what the calibration records.
CALIBRATION_LAND_F = 0.5


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
        self._hold_display = False
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
        self._heaters = [False] * SLOT_COUNT
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
        self._set_heaters(self._heaters)

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_loop.clear()
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

    def tick(self, dt: float) -> None:
        """Advance the simulation and the run clock by ``dt`` seconds."""

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
            self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
            reset_source = getattr(self.source, "reset", None)
            if reset_source is not None:
                reset_source()
            self._set_heaters(self._heaters)
            self.store.reset()
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            saved = self.store.get_settings().to_dict()
        self._refresh_display()
        return saved

    def update_status(self) -> dict:
        return update_report(self.store.path.parent)

    def start_update(self) -> dict:
        """Hand the install to the root oneshot. The database is already saved."""

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
            if "target_temp_f" in patch:
                try:
                    updated = merge_settings(
                        self.store.get_settings(),
                        {"target_temp_f": patch["target_temp_f"]},
                    )
                except SettingsError as exc:
                    raise ControllerError(str(exc), 400) from exc
                self.store.save_settings(updated)
            settings = self.store.get_settings()
            if kind == "calibration" and gallons is None:
                gallons = settings.charge_gallons
            if kind == "calibration":
                scaled = scaled_cutoff(settings.learned_coast_f, settings.charge_gallons, gallons)
                if scaled is not None:
                    settings = replace(settings, early_cutoff_f=scaled)
                    self.store.save_settings(settings)
            self._run_kind = kind
            self._charge_gallons = gallons
            self._coast_watch = None
            self._coast_f = None
            self._coast_seconds = None
            self._calibration_settled = False
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
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
            self._coast_watch = None
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
            settings = self.store.get_settings()
            key = display_key(settings)
            lines = self._panel_lines(settings) if key is not None else None
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

    def _panel_lines(self, settings: Settings) -> list[str]:
        control = self._current_control(settings)
        if control is None:
            probe_name = ""
            temp_f = None
        else:
            index, temp_f = control
            probe_name = settings.sensors[index].name
        heaters = [
            (index, bool(self._heaters[index]))
            for index, channel in enumerate(settings.heaters)
            if channel.enabled and index < len(self._heaters)
        ]
        elapsed = self._cooldown_elapsed if self._mode == "cooling" else self._elapsed
        return status_lines(
            mode=self._mode,
            elapsed_seconds=elapsed,
            probe_name=probe_name,
            temp_f=temp_f,
            target_f=settings.target_temp_f,
            heaters=heaters,
        )

    def _loop(self) -> None:
        last = time.monotonic()
        while not self._stop_loop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            try:
                self.tick(dt)
            except Exception:
                logger.exception("Controller tick failed")
            try:
                self._refresh_display()
            except Exception:
                logger.exception("LCD refresh failed")
            self._stop_loop.wait(1.0)

    def _tick(self, dt: float) -> None:
        settings = self.store.get_settings()
        stepper = getattr(self.source, "step", None)
        if stepper is not None:
            stepper(dt)
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
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
        self._coast_watch = None
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
        temps = [round(float(temp), 2) for temp in self._live_temps[:SLOT_COUNT]]
        self.store.add_sample(
            run_id=self._cooldown_run_id,
            elapsed_seconds=round(self._cooldown_elapsed, 2),
            temps_f=temps,
            heaters_on=[False] * SLOT_COUNT,
            target_temp_f=settings.target_temp_f,
            phase="cooldown",
        )

    def _clear_cooldown(self) -> None:
        self._cooldown_run_id = None
        self._cooldown_elapsed = 0.0
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor = None
        self._cooldown_window = []
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
                "averages": [],
            }

    def _advance_coast(self, settings: Settings) -> None:
        watch = self._coast_watch
        if watch is None:
            return
        found = self._current_control(settings)
        if found is None:
            return
        _, temp = found
        samples: list[tuple[float, float]] = watch["samples"]
        samples.append((self._elapsed, temp))
        if self._elapsed - samples[0][0] < COAST_AVERAGE_SECONDS:
            return
        window_start = self._elapsed - COAST_AVERAGE_SECONDS
        window = [reading for elapsed, reading in samples if elapsed >= window_start]
        average = sum(window) / len(window)
        averages: list[tuple[float, float]] = watch["averages"]
        averages.append((self._elapsed, average))
        if self._elapsed - averages[0][0] < COAST_AVERAGE_HOLD_SECONDS:
            return
        hold_start = self._elapsed - COAST_AVERAGE_HOLD_SECONDS
        recent = [reading for elapsed, reading in averages if elapsed >= hold_start]
        if max(recent) - min(recent) > COAST_AVERAGE_DRIFT_F:
            return
        watch["average"] = sum(recent) / len(recent)
        self._finish_coast(settings)

    def _finish_coast(self, settings: Settings) -> None:
        watch = self._coast_watch
        self._coast_watch = None
        if watch is None or self._run_id is None or "average" not in watch:
            return
        average = float(watch["average"])
        coast = average - watch["temp_at_open"]
        coast = min(EARLY_CUTOFF_MAX_F, max(0.0, round(coast, 2)))
        seconds = round(max(0.0, self._elapsed - watch["opened_elapsed"]), 2)
        settled = average >= settings.target_temp_f - CALIBRATION_LAND_F
        self._coast_f = coast
        self._coast_seconds = seconds
        self._calibration_settled = settled
        learned = replace(
            settings,
            early_cutoff_f=coast,
            learned_coast_f=coast,
            learned_coast_seconds=seconds,
            charge_gallons=self._charge_gallons,
        )
        self.store.save_settings(learned)
        self.store.update_run(
            self._run_id,
            coast_f=coast,
            coast_seconds=seconds,
            calibration_settled=1 if settled else 0,
        )
        logger.info(
            "Run %s coast %s°F over %ss; cutoff now %s°F",
            self._run_id,
            coast,
            seconds,
            coast,
        )
        self._finish_run()

    def _clear_coast(self) -> None:
        self._run_kind = "run"
        self._charge_gallons = None
        self._coast_watch = None
        self._coast_f = None
        self._coast_seconds = None
        self._calibration_settled = False

    def _decide(self, settings: Settings, temps: list[float]) -> list[bool]:
        """Physical heater mask. Index 0 is heater 1. Disabled slots stay off.

        The on/off choice itself, including why power is removed before the
        setpoint, lives in :func:`heater_should_be_on`.
        """

        off = [False] * SLOT_COUNT
        control_indexes = settings.enabled_sensor_indexes()
        if not control_indexes or control_indexes[0] >= len(temps):
            return off
        # The coast watch is the measurement. Leave the SSR open until the
        # average settles so a second heat pulse does not interrupt it.
        if self._run_kind == "calibration" and self._coast_watch is not None:
            return off
        primary = float(temps[control_indexes[0]])
        current = list(self._heaters[:SLOT_COUNT])
        while len(current) < SLOT_COUNT:
            current.append(False)
        # A normal run stops early. Calibration heats until the probe is a
        # little above the target, so the still can actually reach it.
        cutoff = settings.early_cutoff_f
        if self._run_kind == "calibration":
            cutoff = -CALIBRATION_ABOVE_F
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
        temps = [round(float(temp), 2) for temp in self._live_temps[:SLOT_COUNT]]
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
        temps = [
            round(float(self._live_temps[index]), 1)
            for index in sensor_indexes
            if index < len(self._live_temps)
        ]
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
            "target_temp_f": settings.target_temp_f,
            "early_cutoff_f": settings.early_cutoff_f,
            "learned_coast_f": settings.learned_coast_f,
            "learned_coast_seconds": settings.learned_coast_seconds,
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
        }


    def _current_control(self, settings: Settings) -> tuple[int, float] | None:
        indexes = settings.enabled_sensor_indexes()
        if not indexes or indexes[0] >= len(self._live_temps):
            return None
        index = indexes[0]
        return index, float(self._live_temps[index])
