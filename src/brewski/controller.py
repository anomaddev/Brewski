"""Run state machine.

The loop is hardware-agnostic. It reads a :class:`HardwareSource`, decides
whether the heaters should be on, and appends samples while a run is going.
A timeline reading is saved on the same cadence whether or not a run is open,
so the dashboard can scroll the controller clock after a reload.
The first enabled probe is the one compared with the target. Once a
calibration has saved a heating model, heaters turn off when the predicted
crest reaches the target. Until then they turn off by the measured coast,
and sooner when the pot is rising faster than that measurement, so heat
still stored in the element can finish the rise. Disabled heaters stay off.
The installed service drives the SSRs,
the MAX31865 probes, and the HTS221. After setup, this loop also drives
the I2C LCD, the panel buttons, the LED relays, and the target encoder.
"""

from __future__ import annotations

import io
import logging
import math
import subprocess
import threading
import time
import weakref
import zipfile
from dataclasses import replace

from brewski import __version__, analysis
from brewski.calibration import MARK_START, CalibrationSession
from brewski.consolelog import ConsoleCapture, console_log_text
from brewski.emergency import summary
from brewski.lcd import (
    Display,
    NullDisplay,
    display_key,
    open_display,
    startup_lines,
    status_lines,
    suite_result_lines,
    suite_run_lines,
)
from brewski.menu import SoftwareMenu
from brewski.network import display_identity
from brewski.panel import indicator_leds, open_panel, panel_key
from brewski.runlog import (
    analysis_message,
    clean_detail,
    decision_fields,
    control_log_csv,
    format_control,
    model_message,
    model_numbers,
    suite_result_message,
    phase_message,
    start_message,
)
from brewski.sources.base import HardwareSource
from brewski.suite import TEST_INITIAL_HEATING, InitialHeatingSession
from brewski.updates import (
    ReleaseInfo,
    apply_supported,
    check_release,
    installed_revision,
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
from brewski.thermal import (
    CALIBRATION_TEMP_F,
    LEARN_TEMP_F,
    GROK_RATE_BAND_F_PER_MIN,
    GROK_RATE_BAND_F_PER_SEC,
    GROK_RISE_BAND_F,
    SAFE_TEMP_F,
    ThermalModel,
    accept_grok,
    apply_grok_value,
    blend_model,
    model_from_dict,
    coast_entry_slope,
    pulse_plan,
    scale_model,
    project_temperature,
    remaining_coast_f,
    seconds_to_landing,
    step_sheath_slope,
)
from brewski.timeline import TIMELINE_COMPACT_SECONDS

logger = logging.getLogger(__name__)

# Width of the hysteresis band. The element turns back on only after the
# probe falls this far below the early-cutoff temperature.
DEADBAND_F = 2.0
# After a run, cooldown stays open while the control probe is above 90°F.
SAMPLE_INTERVAL_SECONDS = 5.0
# The control log keeps each control-loop reading. The loop decides about
# once a second, and a 5 second gap hides the slope and the shutoff.
LOG_INTERVAL_SECONDS = 1.0
# Ignore a stalled clock that tries to replay more than an hour in one tick.
MAX_TICK_SECONDS = 3600.0
# A calibration only trusts a coast after the element has been on long enough
# to be hot. Shorter pulses still have a cold sheath and teach the wrong number.
COAST_SOAK_SECONDS = 30.0
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
# A late slope this close to the early one is still a straight line. The
# flattening fit waits until the rise has clearly slowed.
COAST_TAU_SLOW_FRACTION = 0.85
# Below this fraction the sheath is still leaving cold, and the shallow slope
# is not yet the straight-line rate. At SOAK_FULL the full coast applies.
SOAK_RAMP_MIN = 0.2
SOAK_FULL = 0.9
# A falling pot is reported against the safe-to-handle line, not room temperature.


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


def coast_tau_seconds(samples: list[tuple[float, float]]) -> float | None:
    """Time constant of a rise that is slowing down.

    The early and late windows each have to be long enough to trust a slope.
    Both slopes have to be rising, and the late one has to be clearly slower.
    ``tau = dt / ln(early / late)`` for a rate that decays as ``exp(-t / tau)``.
    """

    if len(samples) < PREDICT_MIN_SAMPLES * 2:
        return None
    start = samples[0][0]
    end = samples[-1][0]
    if end - start < PREDICT_MIN_SPAN_SECONDS * 2:
        return None
    early_end = start + PREDICT_MIN_SPAN_SECONDS
    late_start = end - PREDICT_MIN_SPAN_SECONDS
    if late_start < early_end:
        return None
    early = [(stamp, reading) for stamp, reading in samples if stamp <= early_end]
    late = [(stamp, reading) for stamp, reading in samples if stamp >= late_start]
    early_rate = slope_f_per_sec(early)
    late_rate = slope_f_per_sec(late)
    if (
        early_rate is None
        or late_rate is None
        or early_rate < PREDICT_MIN_RATE_F_PER_SEC
        or late_rate < PREDICT_MIN_RATE_F_PER_SEC
        or late_rate > early_rate * COAST_TAU_SLOW_FRACTION
    ):
        return None
    early_mid = (early[0][0] + early[-1][0]) / 2.0
    late_mid = (late[0][0] + late[-1][0]) / 2.0
    dt = late_mid - early_mid
    if dt <= 0 or early_rate <= late_rate:
        return None
    tau = dt / math.log(early_rate / late_rate)
    if tau < 1.0 or tau > COAST_WATCH_MAX_SECONDS:
        return None
    return tau


def sheath_soak(off_seconds: float, on_seconds: float, tau_seconds: float) -> float:
    """Fraction of a hot sheath, from 0 cold to 1 soaked.

    The same time constant fills the sheath after the SSR closes and empties
    it after the SSR opens. A long off interval starts from cold.
    """

    if tau_seconds <= 0:
        return 1.0
    off = max(0.0, off_seconds)
    if math.isinf(off) or off >= tau_seconds * 8.0:
        stored = 0.0
    else:
        stored = math.exp(-off / tau_seconds)
    on = max(0.0, on_seconds)
    return 1.0 - (1.0 - stored) * math.exp(-on / tau_seconds)


def projected_linear_rate(rate_f_per_sec: float | None, soak: float) -> float | None:
    """Straight-line °F/s the ramp is climbing toward.

    A cold sheath has not shown that rate yet. Returns None until the soak is
    far enough along, and the measured slope itself once the sheath is hot.
    """

    if rate_f_per_sec is None or rate_f_per_sec < PREDICT_MIN_RATE_F_PER_SEC:
        return None
    if soak >= SOAK_FULL:
        return rate_f_per_sec
    if soak < SOAK_RAMP_MIN:
        return None
    return rate_f_per_sec / soak


def seconds_along_coast(
    temp_f: float,
    target_f: float,
    rate_f_per_sec: float | None,
    tau_seconds: float | None,
) -> float | None:
    """Seconds to the target while the rise rate decays as ``exp(-t / tau)``.

    The degrees still to come are ``rate * tau``. Returns None when this coast
    crests short of the target, or when the rise is not real.
    """

    if temp_f >= target_f:
        return 0.0
    if (
        rate_f_per_sec is None
        or rate_f_per_sec < PREDICT_MIN_RATE_F_PER_SEC
        or tau_seconds is None
        or tau_seconds <= 0
    ):
        return None
    remaining = rate_f_per_sec * float(tau_seconds)
    gap = target_f - temp_f
    if remaining <= gap:
        return None
    seconds = -float(tau_seconds) * math.log(1.0 - gap / remaining)
    return min(PREDICT_MAX_HORIZON_SECONDS, max(0.0, seconds))


def tune_coast(
    learned_f: float | None,
    learned_seconds: float | None,
    used_f: float,
    rate_f_per_sec: float | None,
    miss_f: float,
    observed_seconds: float,
    tau_seconds: float | None = None,
) -> tuple[float, float | None]:
    """Learned coast and duration after a calibration landing.

    ``miss_f`` is the crest minus the calibration temperature. Positive means
    the shutoff was late. The coast that would have landed on that temperature
    is the coast the prediction used, plus the miss. A fitted time constant is
    kept when the same rise rate still predicts that corrected coast. Otherwise
    the duration is the one that makes :func:`predicted_coast_f` return it.
    With no fit, the time from shutoff to the crest stays the duration.
    """

    desired = min(EARLY_CUTOFF_MAX_F, max(0.0, float(used_f) + float(miss_f)))
    fitted = tau_seconds is not None and tau_seconds > 0
    rate_ok = (
        rate_f_per_sec is not None
        and rate_f_per_sec >= PREDICT_MIN_RATE_F_PER_SEC
        and used_f > 0
    )
    if not rate_ok:
        if fitted:
            seconds: float | None = float(tau_seconds)
        elif observed_seconds > 0:
            seconds = observed_seconds
        else:
            seconds = learned_seconds
        return round(desired, 2), None if seconds is None else round(float(seconds), 2)

    floor = 0.0 if learned_f is None else float(learned_f)
    if used_f <= floor + 0.05 or desired < floor:
        floor = desired
    elif desired > floor * PREDICT_COAST_SCALE_MAX:
        floor = desired / PREDICT_COAST_SCALE_MAX
    floor = min(EARLY_CUTOFF_MAX_F, max(0.0, floor))
    rate = float(rate_f_per_sec)
    if fitted and abs(predicted_coast_f(floor, float(tau_seconds), rate) - desired) <= 0.05:
        return round(floor, 2), round(float(tau_seconds), 2)
    if desired <= 0:
        fallback = float(tau_seconds) if fitted else learned_seconds
        return 0.0, None if fallback is None else round(float(fallback), 2)
    seconds = desired / rate
    if floor <= 0 or desired > floor * PREDICT_COAST_SCALE_MAX:
        floor = desired / PREDICT_COAST_SCALE_MAX
    if floor > desired:
        floor = desired
    if fitted and abs(predicted_coast_f(floor, float(tau_seconds), rate) - desired) <= 0.05:
        seconds = float(tau_seconds)
    return round(min(EARLY_CUTOFF_MAX_F, floor), 2), round(seconds, 2)


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
    """Seconds for the probe to fall under 90°F.

    A live falling slope is used as a straight line. A rise is not a cooldown.
    A flat window, or a window that is still too short, uses the learned
    °F/min from calibration.
    """

    if temp_f <= SAFE_TEMP_F:
        return 0.0
    if rate_f_per_sec is not None and rate_f_per_sec <= -PREDICT_MIN_RATE_F_PER_SEC:
        seconds = (temp_f - SAFE_TEMP_F) / -rate_f_per_sec
        return min(PREDICT_MAX_HORIZON_SECONDS, max(0.0, seconds))
    if rate_f_per_sec is not None and rate_f_per_sec >= PREDICT_MIN_RATE_F_PER_SEC:
        return None
    if learned_rate_f_per_min is not None and learned_rate_f_per_min > 0:
        seconds = (temp_f - SAFE_TEMP_F) / (learned_rate_f_per_min / 60.0)
        return min(PREDICT_MAX_HORIZON_SECONDS, max(0.0, seconds))
    return None


def _grok_band(field_name: str) -> float:
    if field_name == "coast_rise_f":
        return GROK_RISE_BAND_F
    if field_name == "cooldown_f_per_min":
        return GROK_RATE_BAND_F_PER_MIN
    return GROK_RATE_BAND_F_PER_SEC


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


def _console_context(owner: weakref.ReferenceType) -> tuple[int | None, float | None]:
    """The open run and its clock. Cooldown keeps the same run after Stop."""

    controller = owner()
    if controller is None:
        return None, None
    if controller._run_id is not None:
        return controller._run_id, controller._elapsed
    if controller._cooldown_run_id is not None:
        return controller._cooldown_run_id, controller._cooldown_elapsed
    return None, None


class Controller:
    def __init__(
        self,
        store: Store,
        source: HardwareSource,
        display: Display | None = None,
    ) -> None:
        self.store = store
        self.source = source
        self._lock = threading.Lock()
        self._stop_loop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shut_down = False
        self._display = NullDisplay()
        self._display_key: tuple[int, int] | None = None
        self._show_panel_step = True
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
        self._since_log = 0.0
        self._since_timeline = 0.0
        self._since_timeline_compact = 0.0
        self._cooldown_run_id: int | None = None
        self._cooldown_elapsed = 0.0
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor: int | None = None
        # (cooldown elapsed seconds, control temperature) while heaters are off
        # after the run. The lower stop is read from the last 5 minutes of this.
        self._cooldown_window: list[tuple[float, float]] = []
        self._run_kind = "run"
        self._charge_gallons: float | None = None
        self._session: CalibrationSession | None = None
        self._suite: InitialHeatingSession | None = None
        self._test_review: dict | None = None
        self._run_model: ThermalModel | None = None
        # The model already saved when this calibration started. Later commits
        # fold the new measurements into this copy, not into their own output.
        self._calibration_prior: ThermalModel | None = None
        self._analysis_inbox: list[tuple[dict, object]] = []
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
        # Control-probe samples for the rate shown on the dashboard. Kept apart
        # from the shutoff window so an idle screen can show it too.
        self._rate_clock = 0.0
        self._rate_samples: list[tuple[float, float]] = []
        # Samples since the SSR opened, cleared when an element turns back on.
        self._coast_curve: list[tuple[float, float]] = []
        # Temperature and slope at the moment the elements opened. The chart
        # spends this coast instead of starting a new one from the live slope.
        self._coast_open_temp: float | None = None
        self._coast_open_slope: float = 0.0
        # Coast and slope from the last shutoff decision, captured while the
        # sheath on-time is still the one that decision used.
        self._last_shutoff_coast = 0.0
        self._last_shutoff_rate: float | None = None
        self._heater_on_for = [0.0] * SLOT_COUNT
        # Where the sheath sits on the fixed coast crest. Heating and cooling
        # move it; opening the SSR does not put it back at zero.
        self._sheath_slope = 0.0
        # Off-time at the moment each SSR last closed. Infinity is a cold start.
        self._off_when_closed = [math.inf] * SLOT_COUNT
        # On-time at the moment each SSR last opened, so the sheath can cool
        # from hot instead of looking cold the instant the element turns off.
        self._on_when_opened = [0.0] * SLOT_COUNT
        # Powered time not yet written to the element-hour meters.
        self._heater_life_pending = [0.0] * SLOT_COUNT
        # Infinity means the off dwell is already satisfied, so the first
        # turn-on of a run is not delayed. A control-law turn-off sets this
        # back to zero. Pause and Stop leave it satisfied on purpose.
        self._heater_off_for = [math.inf] * SLOT_COUNT
        # The last control-law choice, before the minimum on and off times.
        self._decision: dict = {}
        self._desired = [False] * SLOT_COUNT
        # Heater mask already written to this run's log, so a later tick only
        # records a line when the SSR actually changes.
        self._logged_heaters: list[bool] | None = None
        self._console: ConsoleCapture | None = None
        self._install_console()
        if display is not None:
            self._display = display
            self._display_key = display_key(self.store.get_settings())
        # A previous process may have stopped mid-run. Close that record so
        # the next start is a new run, not a continuation of a dead one.
        self.store.close_open_runs()
        open_cooldown = self.store.claim_open_cooldown()
        if open_cooldown is not None:
            self._adopt_cooldown(open_cooldown)
        self._show_startup("Reading sensors")
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        self._sample_climate()
        self._emergency = self._emergency_codes(self.store.get_settings())
        self._set_heaters([False] * SLOT_COUNT if self._emergency else self._heaters)
        if self._mode == "cooling":
            temp = self._cooldown_temp()
            if temp is not None and temp < SAFE_TEMP_F:
                self._complete_cooldown(self.store.get_settings())
        self._record_timeline_point()

    def _install_console(self) -> None:
        """Keep brewski log lines on the open run.

        A controller that has already been freed drops its handler here, so
        tests do not pile handlers onto the process logger.
        """

        log = logging.getLogger("brewski")
        # The service prints INFO. A test run often leaves the root logger at
        # WARNING, which would drop those lines before this handler saw them.
        # Leave a louder setting alone so DEBUG still reaches the console.
        if log.getEffectiveLevel() > logging.INFO:
            log.setLevel(logging.INFO)
        for existing in list(log.handlers):
            if isinstance(existing, ConsoleCapture) and not existing.alive():
                log.removeHandler(existing)
                existing.close()
        owner = weakref.ref(self)
        self._console = ConsoleCapture(self.store, lambda: _console_context(owner), owner)
        log.addHandler(self._console)

    def _detach_console(self) -> None:
        handler = getattr(self, "_console", None)
        if handler is None:
            return
        self._console = None
        log = logging.getLogger("brewski")
        log.removeHandler(handler)
        handler.close()

    def __del__(self) -> None:
        try:
            self._detach_console()
        except Exception:
            return

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
        self._detach_console()

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

    def timeline(self, start_ms: int, end_ms: int, max_points: int) -> dict:
        if int(end_ms) < int(start_ms):
            raise ControllerError("Timeline end is before the start", 400)
        capped = min(5000, max(50, int(max_points)))
        with self._lock:
            return self.store.timeline(int(start_ms), int(end_ms), capped)

    def run(self, run_id: int, *, include_logs: bool = True) -> dict:
        with self._lock:
            record = self.store.get_run(run_id, include_logs=include_logs)
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

    def run_logs(self, run_id: int, after_id: int = 0) -> dict:
        """Control-log lines newer than ``after_id``. The run page appends these."""

        with self._lock:
            logs = self.store.run_logs(run_id, after_id)
        if logs is None:
            raise ControllerError("Run not found", 404)
        return {"logs": logs}

    def control_log_export(self, run_id: int) -> tuple[str, str]:
        """CSV text and a file name for the whole control log."""

        with self._lock:
            kind = self.store.run_kind(run_id)
            if kind is None:
                raise ControllerError("Run not found", 404)
            logs = self.store.run_logs(run_id)
        label = "calibration" if kind == "calibration" else "run"
        return control_log_csv(logs or []), f"brewski-{label}-{run_id}-log.csv"

    def run_export(self, run_id: int) -> tuple[bytes, str]:
        """Zip of the control-log CSV and the console output captured for this run."""

        with self._lock:
            kind = self.store.run_kind(run_id)
            if kind is None:
                raise ControllerError("Run not found", 404)
            logs = self.store.run_logs(run_id) or []
            lines = self.store.console_lines(run_id)
        label = "calibration" if kind == "calibration" else "run"
        stem = f"brewski-{label}-{run_id}"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{stem}-log.csv", control_log_csv(logs))
            archive.writestr(f"{stem}-console.log", console_log_text(lines or []))
        return buffer.getvalue(), f"{stem}.zip"

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

    def clear_calibration(self) -> dict:
        """Forget the learned heating model so the next calibration starts over.

        Run history stays. A normal run stays locked until a new calibration
        saves a coast.
        """

        with self._lock:
            if self._mode in ("running", "paused"):
                raise ControllerError("Stop the run before clearing the calibration", 409)
            current = self.store.get_settings()
            self.store.save_settings(
                replace(
                    current,
                    learned_coast_f=None,
                    learned_coast_seconds=None,
                    learned_cooldown_f_per_min=None,
                    thermal_model=None,
                )
            )
            self._display_coast = None
            self._display_rate = None
            self._calibration_prior = None
            self._run_model = None
            self._coast_f = None
            self._coast_seconds = None
            status = self._status()
        self._refresh_display()
        return status

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
            self._since_log = 0.0
            self._clear_coast()
            self._suite = None
            self._test_review = None
            self._clear_cooldown()
            self._clear_predict()
            self._clear_rate()
            self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
            reset_source = getattr(self.source, "reset", None)
            if reset_source is not None:
                reset_source()
            self._set_heaters(self._heaters)
            self.store.reset()
            self._dial_target = None
            self._dial_dirty = False
            self._since_timeline = 0.0
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            self._record_timeline_point()
            saved = self.store.get_settings().to_dict()
        self._refresh_display()
        return saved

    def update_status(self, *, fresh: bool = False) -> dict:
        return update_report(self.store.path.parent, fresh=fresh)

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
            if self._test_review is not None:
                raise ControllerError("End the test result before starting a run", 409)
            if self._mode == "cooling":
                self._abandon_cooldown()
            elif self._mode != "idle":
                raise ControllerError("A run is already open", 409)
            patch = patch or {}
            if set(patch) - {"target_temp_f", "kind", "charge_gallons", "test"}:
                raise ControllerError(
                    "Start only accepts target_temp_f, kind, charge_gallons, and test",
                    400,
                )
            kind = patch.get("kind", "run")
            if kind not in ("run", "calibration", "test"):
                raise ControllerError("kind must be run, calibration, or test", 400)
            if kind == "test":
                if patch.get("test") != TEST_INITIAL_HEATING:
                    raise ControllerError("Unknown test", 400)
                if "charge_gallons" in patch or "target_temp_f" in patch:
                    raise ControllerError("A test does not take a target or a still size", 400)
            elif "test" in patch:
                raise ControllerError("test is only for a test run", 400)
            if kind != "calibration" and "charge_gallons" in patch:
                raise ControllerError("charge_gallons is only for a calibration", 400)
            if kind == "run" and self.store.get_settings().learned_coast_f is None:
                raise ControllerError("Calibrate the still before starting a run", 409)
            gallons = None
            if patch.get("charge_gallons") is not None:
                try:
                    gallons = charge_gallons(patch["charge_gallons"])
                except SettingsError as exc:
                    raise ControllerError(str(exc), 400) from exc
            self._write_pending_target()
            # A calibration aims at CALIBRATION_TEMP_F. A target in this
            # request would replace the setpoint the next real run uses.
            if kind != "calibration" and "target_temp_f" in patch:
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
                settings = self._scale_charge(settings, gallons)
            self._calibration_prior = (
                model_from_dict(settings.thermal_model) if kind == "calibration" else None
            )
            self._run_kind = kind
            self._charge_gallons = gallons
            self._session = (
                CalibrationSession(self._calibration_prior) if kind == "calibration" else None
            )
            self._suite = InitialHeatingSession() if kind == "test" else None
            self._run_model = (
                None if kind in ("calibration", "test") else model_from_dict(settings.thermal_model)
            )
            self._sheath_slope = 0.0
            self._coast_f = None
            self._coast_seconds = None
            self._calibration_settled = False
            self._display_coast = None
            self._display_rate = None
            self._pulse_until = None
            self._hold_ready = False
            self._clear_predict()
            self._coast_curve.clear()
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            self._sample_climate()
            self._emergency = self._emergency_codes(settings)
            if self._emergency:
                self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
                raise ControllerError("Emergency stop is active", 409)
            # Running before the first heater command so a calibration aims at
            # the learning temperature on that command, not at the saved run
            # setpoint. The first three seconds stay off.
            self._mode = "running"
            self._elapsed = 0.0
            self._since_sample = 0.0
            self._since_log = 0.0
            self._command_heaters(settings)
            run_settings = settings
            if kind == "calibration":
                run_settings = replace(settings, target_temp_f=int(CALIBRATION_TEMP_F))
            run = self.store.create_run(run_settings, kind=kind, charge_gallons=gallons)
            self._run_id = int(run["id"])
            if kind in ("calibration", "test"):
                self._add_mark(MARK_START)
            detail = self._control_detail(settings)
            prior = self._calibration_prior if kind == "calibration" else self._run_model
            if prior is not None:
                numbers = model_numbers(prior.to_dict())
                if numbers:
                    detail["model"] = numbers
            self._log(
                "start",
                start_message(
                    kind,
                    self._aim_temperature(settings),
                    None if prior is None else prior.to_dict(),
                    settings.learned_coast_f,
                ),
                detail,
            )
            self._record_sample(settings)
            self._record_control_log(settings)
            self._logged_heaters = self._heater_mask(settings)
            return self._status()

    def pause(self) -> dict:
        with self._lock:
            if self._mode != "running":
                raise ControllerError("There is no running run to pause", 409)
            if self._run_kind == "test":
                raise ControllerError("A test cannot be paused", 409)
            settings = self.store.get_settings()
            self._pulse_until = None
            self._decision = {"path": "pause", "heat": False}
            self._desired = [False] * SLOT_COUNT
            self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
            self._logged_heaters = self._heater_mask(settings)
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
            self._mode = "paused"
            self._record_sample(settings)
            self._record_control_log(settings)
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
            self._log_output_changes(settings, None)
            self._record_sample(settings)
            self._record_control_log(settings)
            self.store.update_run(self._run_id, status="running", elapsed_seconds=self._elapsed)
            logger.info("Run %s resumed", self._run_id)
            return self._status()

    def stop(self) -> dict:
        with self._lock:
            if self._mode in ("running", "paused"):
                self._finish_run()
                return self._status()
            if not self._clear_test_review():
                raise ControllerError("There is no open run to stop", 409)
            status = self._status()
        self._refresh_display()
        return status

    def end_test(self) -> dict:
        """Clear a finished test result. A test that is still heating is left running."""

        with self._lock:
            if self._test_review is None:
                if self._mode in ("running", "paused") and self._suite is not None:
                    raise ControllerError("The test is still running", 409)
                raise ControllerError("There is no test result to end", 409)
            self._clear_test_review()
            status = self._status()
        self._refresh_display()
        return status

    def _clear_test_review(self) -> bool:
        """Caller holds ``self._lock``. True when a result was on screen."""

        if self._test_review is None:
            return False
        self._test_review = None
        return True

    def _show_startup(self, step: str) -> None:
        self._display.write(startup_lines(step))

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
                revision = installed_revision() if self._menu.screen == "about" else None
                lines = self._menu.lines(
                    __version__,
                    address=address,
                    hostname=hostname,
                    update_source=settings.update_source,
                    revision=revision,
                )
                led_state = self._menu.leds()
            else:
                lines = self._panel_lines(settings) if key is not None else None
                led_state = indicator_leds(
                    self._mode,
                    emergency=bool(self._emergency),
                    review=self._test_review is not None,
                )
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
        session = self._session
        if session is None or self._run_kind != "calibration" or self._mode not in ("running", "paused"):
            return {}
        return {
            "step": session.phase,
            "rise": session.live_rise,
            "peak": session.model.coast_rise_f,
            "rate": session.live_rate if session.live_rate is not None else session.model.cooldown_f_per_min,
        }

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
        if not code and self._test_review is not None and self._mode not in ("running", "paused"):
            return suite_result_lines(self._test_review)
        if (
            not code
            and self._suite is not None
            and self._run_kind == "test"
            and self._mode in ("running", "paused")
        ):
            return suite_run_lines(self._suite.phase, self._suite.phase_elapsed(), probes, heaters)
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
            target_f=self._aim_temperature(settings),
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
        self._drain_analysis()
        settings = self.store.get_settings()
        stepper = getattr(self.source, "step", None)
        if stepper is not None:
            stepper(dt)
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        self._sample_climate()
        self._note_rate(dt)
        self._emergency = self._emergency_codes(settings)
        try:
            self._note_timeline(dt, settings)
        except Exception:
            logger.exception("Timeline sample was not saved")
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
                self._note_control_log(dt, settings)
            return
        self._elapsed += dt
        self._note_heater_dwell(dt)
        phase_before = self._active_phase()
        self._advance_calibration(settings)
        self._advance_suite(settings)
        if self._mode != "running":
            return
        self._step_sheath(dt, settings)
        self._command_heaters(settings)
        self._log_output_changes(settings, phase_before)
        if self._mode != "running":
            return
        self._advance_cooldown_clock(dt, settings)
        self._note_control_log(dt, settings)
        self._since_sample += dt
        while self._since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._since_sample -= SAMPLE_INTERVAL_SECONDS
            self._record_sample(settings)
            self.store.update_run(self._run_id, elapsed_seconds=self._elapsed)

    def _finish_run(self) -> None:
        settings = self.store.get_settings()
        suite = self._suite
        if suite is not None:
            if not suite.finished:
                found = self._current_control(settings)
                if found is not None:
                    temp = float(found[1])
                elif suite.samples:
                    temp = suite.samples[-1][1]
                else:
                    temp = 0.0
                suite.abort(self._elapsed, temp)
                for name in suite.pending_marks:
                    self._add_mark(name)
            self._publish_test(suite)
            self._suite = None
            self._run_kind = "run"
        session = self._session
        if session is not None:
            if session.model.coast_rise_f is not None or session.model.linear_rate_f_per_sec is not None:
                self._commit_model(settings)
            saved = self.store.get_settings()
            self._display_coast = saved.learned_coast_f
            self._display_rate = saved.learned_cooldown_f_per_min
            self._calibration_settled = session.reached_target
            # The revision is for the next run. Detach this session first so
            # a live heater decision cannot pick the revised numbers up.
            self._session = None
            self._calibration_prior = None
            self._queue_full_analysis(session)
        elif self._run_kind == "calibration" and self._coast_f is not None:
            self._display_coast = self._coast_f
        self._session = None
        self._run_model = None
        self._pulse_until = None
        self._hold_ready = False
        self._flush_heater_life()
        self._decision = {"path": "stop", "heat": False}
        self._desired = [False] * SLOT_COUNT
        self._set_heaters([False] * SLOT_COUNT, release_off_dwell=True)
        self._logged_heaters = [False] * SLOT_COUNT
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        if self._run_id is not None:
            self._record_sample(settings)
            self._record_control_log(settings)
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
            self._open_cooldown_if_hot(settings)
        if self._cooldown_run_id is None:
            return
        self._mode = "cooling"
        self._record_cooldown_sample(settings)
        self._record_control_log(settings)
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
            aim = self._aim_temperature(settings)
            if temp < aim:
                return
            self._cooldown_run_id = self._run_id
            self._cooldown_sensor = index
            self._cooldown_elapsed = 0.0
            self._cooldown_since_sample = 0.0
            self.store.update_run(
                self._run_id,
                cooldown_status="open",
                cooldown_seconds=0,
                cooldown_baseline_f=aim,
                cooldown_sensor_index=index,
            )
            logger.info(
                "Run %s reached %s°F; cooldown timing started",
                self._run_id,
                aim,
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
        if current is not None and current < SAFE_TEMP_F:
            self._complete_cooldown(settings)
            return
        self._note_control_log(dt, settings)
        while self._cooldown_since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._cooldown_since_sample -= SAMPLE_INTERVAL_SECONDS
            self._record_cooldown_sample(settings)
        self._persist_cooldown()

    def _open_cooldown_if_hot(self, settings: Settings) -> None:
        """Start a cooldown when a run stops above 90°F without having reached its aim."""

        if self._run_id is None or self._cooldown_run_id is not None:
            return
        found = self._current_control(settings)
        if found is None:
            return
        index, temp = found
        if temp <= SAFE_TEMP_F:
            return
        self._cooldown_run_id = self._run_id
        self._cooldown_sensor = index
        self._cooldown_elapsed = 0.0
        self._cooldown_since_sample = 0.0
        self.store.update_run(
            self._run_id,
            cooldown_status="open",
            cooldown_seconds=0,
            cooldown_baseline_f=round(temp, 2),
            cooldown_sensor_index=index,
        )
        logger.info("Run %s cooling from %s°F", self._run_id, round(temp, 1))

    def _complete_cooldown(self, settings: Settings) -> None:
        run_id = self._cooldown_run_id
        elapsed = self._cooldown_elapsed
        self._record_cooldown_sample(settings)
        self._record_control_log(settings)
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
            cooldown_status="interrupted",
            cooldown_seconds=round(self._cooldown_elapsed, 2),
        )
        logger.info("Run %s cooldown interrupted at %ss", run_id, round(self._cooldown_elapsed, 1))
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
            target_temp_f=(
                CALIBRATION_TEMP_F
                if self._run_kind == "calibration"
                else float(settings.target_temp_f)
            ),
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

        desired = self._decide(settings, self._live_temps)
        self._desired = list(desired)
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
        self._note_coast_curve(settings)

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
                if not previous[index]:
                    self._off_when_closed[index] = self._heater_off_for[index]
                self._heater_off_for[index] = math.inf
            else:
                if previous[index]:
                    self._on_when_opened[index] = self._heater_on_for[index]
                self._heater_on_for[index] = 0.0
                # Only the transition into off starts the dwell. Holding an
                # already-open SSR does not reset the timer. Pause and Stop
                # release it so the next Start or Resume is not delayed.
                if previous[index] or release_off_dwell:
                    self._heater_off_for[index] = math.inf if release_off_dwell else 0.0
        self.source.set_heaters(self._heaters)

    def _scale_charge(self, settings: Settings, gallons: float | None) -> Settings:
        """Scale a remembered model when this calibration uses a different volume."""

        updates: dict = {}
        scaled = scaled_cutoff(settings.learned_coast_f, settings.charge_gallons, gallons)
        if scaled is not None:
            updates["learned_coast_f"] = scaled
        scaled_cool = scaled_rate(
            settings.learned_cooldown_f_per_min, settings.charge_gallons, gallons
        )
        if scaled_cool is not None:
            updates["learned_cooldown_f_per_min"] = scaled_cool
        grown = scale_model(model_from_dict(settings.thermal_model), settings.charge_gallons, gallons)
        if grown.to_dict() != model_from_dict(settings.thermal_model).to_dict():
            updates["thermal_model"] = grown.to_dict()
            if grown.coast_rise_f is not None:
                updates["learned_coast_f"] = min(EARLY_CUTOFF_MAX_F, max(0.0, grown.coast_rise_f))
            if grown.coast_seconds is not None:
                updates["learned_coast_seconds"] = grown.coast_seconds
            if grown.cooldown_f_per_min is not None:
                updates["learned_cooldown_f_per_min"] = grown.cooldown_f_per_min
        if not updates:
            return settings
        settings = replace(settings, **updates)
        self.store.save_settings(settings)
        return settings

    def _advance_calibration(self, settings: Settings) -> None:
        """Move the calibration one reading, then let the heater command follow."""

        session = self._session
        if session is None or self._run_kind != "calibration" or self._mode != "running":
            return
        found = self._current_control(settings)
        if found is None:
            return
        session.observe(
            self._elapsed,
            found[1],
            self._pulse_on_seconds(settings),
            settings.heater_min_on_seconds,
        )
        for name in session.pending_marks:
            self._add_mark(name)
        for item in session.pending_analysis:
            self._queue_analysis(item)
        if session.reached_target:
            self._calibration_settled = True
        if session.commit:
            self._commit_model(settings)
        if session.finished:
            self._finish_run()

    def _advance_suite(self, settings: Settings) -> None:
        """Move an Initial Heating test one reading."""

        suite = self._suite
        if suite is None or self._run_kind != "test" or self._mode != "running":
            return
        found = self._current_control(settings)
        if found is None:
            return
        suite.observe(self._elapsed, found[1])
        for name in suite.pending_marks:
            self._add_mark(name)
        if suite.finished:
            self._finish_run()

    def _publish_test(self, suite: InitialHeatingSession) -> None:
        payload = suite.payload(review=True, run_id=self._run_id)
        self._test_review = payload
        self._log("test", suite_result_message(payload), payload)

    def _active_phase(self) -> str | None:
        if self._session is not None:
            return self._session.phase
        if self._suite is not None:
            return self._suite.phase
        return None

    def _test_payload(self) -> dict | None:
        if self._suite is not None and self._mode in ("running", "paused"):
            return self._suite.payload(review=False, run_id=self._run_id)
        if self._test_review is not None:
            return dict(self._test_review)
        return None

    def _step_sheath(self, dt: float, settings: Settings) -> None:
        """Move the crest entry for a normal run. Calibration steps its own."""

        model = self._run_model
        if self._session is not None or model is None or not model.ready():
            return
        self._sheath_slope = step_sheath_slope(
            model,
            self._sheath_slope,
            heating=self._elements_on(settings),
            dt=dt,
        )

    def _pulse_on_seconds(self, settings: Settings) -> float:
        """How long the enabled elements that are on have been on."""

        ons = [
            self._heater_on_for[index]
            for index in range(SLOT_COUNT)
            if settings.heaters[index].enabled and self._heaters[index]
        ]
        if not ons:
            return 0.0
        return max(ons)

    def _opened_on_seconds(self, settings: Settings) -> float:
        """How long the elements had been on when they last opened."""

        opened = [
            self._on_when_opened[index]
            for index in range(SLOT_COUNT)
            if settings.heaters[index].enabled
        ]
        if not opened:
            return 0.0
        return max(opened)

    def _coast_off_seconds(self, settings: Settings) -> float:
        """How long the enabled elements have been off since they opened."""

        offs = []
        for index in range(SLOT_COUNT):
            if not settings.heaters[index].enabled or self._heaters[index]:
                continue
            off = self._heater_off_for[index]
            if math.isfinite(off):
                offs.append(off)
        if not offs:
            return 0.0
        return max(offs)

    def _add_mark(self, name: str) -> None:
        if self._run_id is None:
            return
        self.store.add_mark(self._run_id, name, self._elapsed)

    def _commit_model(self, settings: Settings) -> None:
        """Write the model learned so far. Missing stages do not erase a previous one."""

        session = self._session
        if session is None:
            return
        prior = self._calibration_prior if self._calibration_prior is not None else ThermalModel()
        model = blend_model(prior, session.model)
        current = self.store.get_settings()
        coast = current.learned_coast_f
        seconds = current.learned_coast_seconds
        rate = current.learned_cooldown_f_per_min
        if model.coast_rise_f is not None:
            coast = round(max(0.0, model.coast_rise_f), 2)
            self._coast_f = coast
        if model.coast_seconds is not None:
            seconds = round(max(0.0, model.coast_seconds), 2)
            self._coast_seconds = seconds
        if model.cooldown_f_per_min is not None:
            rate = round(min(COOLDOWN_RATE_MAX, max(0.0, model.cooldown_f_per_min)), 2)
        learned = replace(
            current,
            learned_coast_f=coast,
            learned_coast_seconds=seconds,
            learned_cooldown_f_per_min=rate,
            charge_gallons=self._charge_gallons if self._charge_gallons is not None else current.charge_gallons,
            thermal_model=model.to_dict(),
        )
        self.store.save_settings(learned)
        if self._run_id is not None:
            import json

            self.store.update_run(
                self._run_id,
                coast_f=coast,
                coast_seconds=seconds,
                cooldown_f_per_min=rate,
                calibration_settled=1 if self._calibration_settled else 0,
                thermal_model=json.dumps(model.to_dict()),
            )
        self._log(
            "model",
            model_message(model.to_dict()),
            {
                "model": model.to_dict(),
                "coast_f": coast,
                "coast_seconds": seconds,
                "cooldown_f_per_min": rate,
            },
        )

    def _queue_analysis(self, item: dict) -> None:
        """Ask Grok about one stage. The loop keeps the local fit until it answers."""

        run_id = self._run_id
        if run_id is None:
            return
        kind = str(item.get("kind") or "")
        local = item.get("local")
        if not analysis.api_key():
            self._record_analysis(run_id, kind, local, None, False)
            return
        samples = item.get("samples") or []

        def finish(value: object) -> None:
            self._accept_analysis(run_id, item, value)

        if analysis.inline_analysis():
            finish(analysis.analyze_rate(samples, kind))
            return

        def work() -> None:
            value = analysis.analyze_rate(samples, kind)
            with self._lock:
                self._analysis_inbox.append(({"run_id": run_id, **item}, value))

        threading.Thread(target=work, name="brewski-analysis", daemon=True).start()

    def _queue_full_analysis(self, session: CalibrationSession) -> None:
        """One pass over the finished calibration, applied to the next run's model."""

        run_id = self._run_id
        if run_id is None or not analysis.api_key():
            if run_id is not None:
                self._record_analysis(run_id, "full", None, None, False)
            return
        samples = [[round(stamp, 2), round(reading, 3)] for stamp, reading in session.samples]
        local = session.model.to_dict()

        def finish(value: object) -> None:
            self._revise_saved_model(run_id, local, value if isinstance(value, dict) else None)

        if analysis.inline_analysis():
            finish(analysis.analyze_model(samples, local))
            return

        def work() -> None:
            value = analysis.analyze_model(samples, local)
            with self._lock:
                self._analysis_inbox.append(
                    ({"run_id": run_id, "kind": "full", "local_model": local}, value)
                )

        threading.Thread(target=work, name="brewski-analysis", daemon=True).start()

    def _drain_analysis(self) -> None:
        inbox = self._analysis_inbox
        if not inbox:
            return
        self._analysis_inbox = []
        for item, value in inbox:
            kind = str(item.get("kind") or "")
            run_id = item.get("run_id")
            if kind == "full":
                self._revise_saved_model(
                    int(run_id) if run_id is not None else None,
                    item.get("local_model") if isinstance(item.get("local_model"), dict) else {},
                    value if isinstance(value, dict) else None,
                )
                continue
            if run_id is None:
                continue
            self._accept_analysis(int(run_id), item, value)

    def _accept_analysis(self, run_id: int, item: dict, value: object) -> None:
        local = item.get("local")
        local_f = float(local) if isinstance(local, (int, float)) else None
        remote = float(value) if isinstance(value, (int, float)) else None
        field = str(item.get("field") or "")
        band = _grok_band(field)
        accepted = accept_grok(local_f, remote, band=band)
        session = self._session
        heaters_on = self._elements_on(self.store.get_settings())
        if (
            accepted
            and remote is not None
            and session is not None
            and self._run_id == run_id
            and not (session.phase in {"pulse", "pulses", "hold"} and heaters_on)
        ):
            apply_grok_value(session.model, field, remote)
            self._commit_model(self.store.get_settings())
        elif accepted and remote is not None and session is not None and session.phase in {"pulse", "pulses", "hold"} and heaters_on:
            with_value = dict(item)
            with_value["run_id"] = run_id
            self._analysis_inbox.append((with_value, remote))
            return
        self._record_analysis(run_id, str(item.get("kind") or ""), local_f, remote, accepted)

    def _revise_saved_model(self, run_id: int | None, local: dict, revised: dict | None) -> None:
        """A finished calibration may update the model the next run will load."""

        if run_id is None:
            return
        if not revised:
            self._record_analysis(run_id, "full", None, None, False)
            return
        current = self.store.get_settings()
        model = model_from_dict(current.thermal_model)
        accepted_any = False
        checks = (
            ("linear_rate_f_per_sec", "linear_rate_f_per_sec", GROK_RATE_BAND_F_PER_SEC),
            ("coast_rise_f", "coast_rise_f", GROK_RISE_BAND_F),
            ("cooldown_f_per_min", "cooldown_f_per_min", GROK_RATE_BAND_F_PER_MIN),
        )
        for key, _field, band in checks:
            local_value = local.get(key)
            remote_value = revised.get(key)
            local_f = float(local_value) if isinstance(local_value, (int, float)) else None
            remote_f = float(remote_value) if isinstance(remote_value, (int, float)) else None
            if not accept_grok(local_f, remote_f, band=band) or remote_f is None:
                continue
            accepted_any = True
            if key == "linear_rate_f_per_sec":
                model.linear_rate_f_per_sec = round(max(0.0, remote_f), 5)
            elif key == "coast_rise_f":
                model.coast_rise_f = round(max(0.0, remote_f), 2)
                if model.coast_seconds and model.coast_seconds > 0:
                    model.coast_quad = round(-model.coast_rise_f / (model.coast_seconds ** 2), 6)
            else:
                model.cooldown_f_per_min = round(min(COOLDOWN_RATE_MAX, max(0.0, remote_f)), 2)
        self._record_analysis(run_id, "full", None, None, accepted_any)
        if not accepted_any:
            return
        if self._session is not None and self._run_id == run_id:
            return
        learned = replace(
            current,
            thermal_model=model.to_dict(),
            learned_coast_f=model.coast_rise_f if model.coast_rise_f is not None else current.learned_coast_f,
            learned_coast_seconds=model.coast_seconds
            if model.coast_seconds is not None
            else current.learned_coast_seconds,
            learned_cooldown_f_per_min=model.cooldown_f_per_min
            if model.cooldown_f_per_min is not None
            else current.learned_cooldown_f_per_min,
        )
        self.store.save_settings(learned)

    def _record_analysis(
        self,
        run_id: int,
        kind: str,
        local: float | None,
        remote: float | None,
        accepted: bool,
    ) -> None:
        self.store.append_analysis(
            run_id,
            {
                "kind": kind,
                "local": local,
                "grok": remote,
                "accepted": accepted,
            },
        )
        elapsed = self._elapsed if self._run_id == run_id else None
        self._log(
            "analysis",
            analysis_message(kind, local, remote, accepted),
            {"kind": kind, "local": local, "grok": remote, "accepted": accepted},
            run_id=run_id,
            elapsed=elapsed,
        )

    def _clear_coast(self) -> None:
        self._run_kind = "run"
        self._charge_gallons = None
        self._session = None
        self._run_model = None
        self._coast_f = None
        self._coast_seconds = None
        self._calibration_settled = False
        self._pulse_until = None
        self._hold_ready = False

    def _clear_predict(self) -> None:
        self._predict_clock = 0.0
        self._predict_samples.clear()
        self._coast_curve.clear()

    def _clear_rate(self) -> None:
        self._rate_clock = 0.0
        self._rate_samples.clear()

    def _note_rate(self, dt: float) -> None:
        """Remember the control probe so the dashboard can show °F/min."""

        if dt <= 0:
            return
        self._rate_clock += dt
        found = self._current_control(self.store.get_settings())
        if found is None:
            self._rate_samples.clear()
            return
        self._rate_samples.append((self._rate_clock, found[1]))
        horizon = self._rate_clock - PREDICT_WINDOW_SECONDS
        while len(self._rate_samples) >= 2 and self._rate_samples[1][0] <= horizon:
            self._rate_samples.pop(0)

    def _temperature_rate(self) -> float | None:
        return slope_f_per_sec(self._rate_samples)

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
        """Degrees early to open the SSR.

        A short window leaves the learned coast unchanged so one jumped reading
        cannot move the shutoff. While the sheath is still heating, only the
        soaked fraction of that coast is used. A hot sheath uses the full
        prediction, widened when the live rise is faster.
        """

        rate = slope_f_per_sec(self._predict_samples)
        tau = settings.learned_coast_seconds
        soak = 1.0 if tau is None or tau <= 0 else self._sheath_soak(settings)
        if soak < SOAK_FULL:
            coast = predicted_coast_f(settings.learned_coast_f, tau, None) * soak
        else:
            coast = predicted_coast_f(settings.learned_coast_f, tau, rate)
        self._last_shutoff_coast = coast
        self._last_shutoff_rate = rate
        return coast

    def _sheath_soak(self, settings: Settings) -> float:
        """Hottest enabled sheath that is currently on, else the hottest enabled one."""

        tau = settings.learned_coast_seconds
        if tau is None or tau <= 0:
            return 1.0
        enabled = [index for index in range(SLOT_COUNT) if settings.heaters[index].enabled]
        if not enabled:
            return 1.0
        powered = [index for index in enabled if self._heaters[index]] or enabled
        return max(self._one_sheath_soak(index, tau) for index in powered)

    def _one_sheath_soak(self, index: int, tau: float) -> float:
        if self._heaters[index]:
            return sheath_soak(self._off_when_closed[index], self._heater_on_for[index], tau)
        hot = sheath_soak(self._off_when_closed[index], self._on_when_opened[index], tau)
        off = self._heater_off_for[index]
        if math.isinf(off):
            return 0.0
        return hot * math.exp(-max(0.0, off) / tau)

    def _elements_on(self, settings: Settings) -> bool:
        return any(
            self._heaters[index] and settings.heaters[index].enabled for index in range(SLOT_COUNT)
        )

    def _note_coast_curve(self, settings: Settings) -> None:
        """Remember the rise after the SSR opens, and drop it when heat returns."""

        if self._elements_on(settings):
            self._coast_curve.clear()
            self._coast_open_temp = None
            self._coast_open_slope = 0.0
            return
        found = self._current_control(settings)
        if found is None:
            return
        if self._coast_open_temp is None:
            self._coast_open_temp = found[1]
            session = self._session
            if session is not None and session.shutoff_slope > 0:
                self._coast_open_slope = session.shutoff_slope
            else:
                self._coast_open_slope = self._sheath_slope
        self._coast_curve.append((self._predict_clock, found[1]))
        horizon = self._predict_clock - COAST_WATCH_MAX_SECONDS
        while len(self._coast_curve) >= 2 and self._coast_curve[1][0] <= horizon:
            self._coast_curve.pop(0)

    def _coast_time_constant(self, settings: Settings) -> float | None:
        """Fitted decay from this shutoff, or the learned duration when it has not bent yet."""

        samples: list[tuple[float, float]] = list(self._coast_curve)
        fitted = coast_tau_seconds(samples) if len(samples) >= PREDICT_MIN_SAMPLES * 2 else None
        if fitted is not None:
            return fitted
        learned = settings.learned_coast_seconds
        if learned is None or learned <= 0:
            return None
        return float(learned)

    def _lookahead_model(self) -> ThermalModel | None:
        """The heating model, once the learning coast has finished or a run loaded one."""

        if self._run_model is not None and self._run_model.ready():
            return self._run_model
        session = self._session
        if (
            session is not None
            and session.model.ready()
            and session.phase in {"pulse", "pulses", "hold"}
        ):
            return session.model
        return None

    def _aim_temperature(self, settings: Settings) -> float:
        """Temperature the heaters are aiming at.

        A calibration scores the learning coast against 140°F, then pulses
        and holds at 160°F. The dial and the saved setpoint still belong to
        the next real run.
        """

        if self._run_kind == "calibration" and self._mode in ("running", "paused"):
            session = self._session
            if session is not None and session.phase in {"pulse", "pulses", "hold"}:
                return CALIBRATION_TEMP_F
            return LEARN_TEMP_F
        return float(self._target_value(settings))

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
        aim = self._aim_temperature(settings)
        heating = self._elements_on(settings)
        rising = rate is not None and rate >= PREDICT_MIN_RATE_F_PER_SEC and temp < aim
        lookahead = self._lookahead_model()
        if lookahead is not None and (heating or rising):
            # The heating slope picks where on the coast the shutoff starts.
            # A sheath or probe reading below that curve does not lower it.
            on_seconds = (
                self._pulse_on_seconds(settings)
                if heating
                else self._opened_on_seconds(settings)
            )
            slope = self._session.sheath_slope if self._session is not None else self._sheath_slope
            if heating:
                if rate is not None and rate > slope:
                    slope = rate
                slope = coast_entry_slope(lookahead, on_seconds, slope)
            seconds = seconds_to_landing(
                temp,
                aim,
                on_seconds,
                lookahead,
                heating=heating,
                off_seconds=0.0 if heating else self._coast_off_seconds(settings),
                horizon=PREDICT_MAX_HORIZON_SECONDS,
                slope_f_per_sec=slope,
            )
            return {
                "seconds_to_target": _forecast_seconds(seconds),
                "seconds_to_cooldown": None,
            }
        if heating or rising:
            if heating:
                use_rate = rate
                if settings.learned_coast_seconds:
                    use_rate = projected_linear_rate(rate, self._sheath_soak(settings))
                seconds = seconds_to_target(
                    temp,
                    aim,
                    self._shutoff_coast(settings),
                    use_rate,
                    settings.learned_coast_f,
                    settings.learned_coast_seconds,
                )
            else:
                tau = self._coast_time_constant(settings)
                if tau is None:
                    seconds = seconds_to_target(
                        temp,
                        aim,
                        predicted_coast_f(
                            settings.learned_coast_f,
                            settings.learned_coast_seconds,
                            rate,
                        ),
                        rate,
                        settings.learned_coast_f,
                        settings.learned_coast_seconds,
                    )
                else:
                    seconds = seconds_along_coast(temp, aim, rate, tau)
            return {
                "seconds_to_target": _forecast_seconds(seconds),
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
            self._decision = {"path": "run", "heat": False}
            return off
        if self._suite is not None and self._run_kind == "test":
            heat = bool(self._suite.heat) and not self._suite.finished
            trace = dict(self._suite.trace)
            trace["heat"] = heat
            trace.setdefault("path", self._suite.phase)
            self._decision = trace
            return [heat and settings.heaters[index].enabled for index in range(SLOT_COUNT)]
        if self._session is not None and self._run_kind == "calibration":
            heat = bool(self._session.heat) and not self._session.finished
            trace = dict(self._session.trace)
            trace["heat"] = heat
            trace.setdefault("path", self._session.phase)
            self._decision = trace
            return [
                heat and settings.heaters[index].enabled for index in range(SLOT_COUNT)
            ]
        raw = temps[control_indexes[0]]
        if raw is None:
            self._decision = {"path": "run", "heat": False}
            return off
        primary = float(raw)
        model = self._run_model
        if model is not None and model.ready():
            live = slope_f_per_sec(self._predict_samples)
            slope = self._sheath_slope
            if live is not None and live > slope:
                slope = live
            target = float(self._target_value(settings))
            plan = pulse_plan(
                primary,
                target,
                self._pulse_on_seconds(settings),
                model,
                settings.heater_min_on_seconds,
                slope_f_per_sec=slope,
                budgeted=True,
            )
            self._decision = {
                "path": "model",
                "target_f": target,
                "live_slope_f_per_sec": None if live is None else round(live, 5),
                "sheath_slope_f_per_sec": round(self._sheath_slope, 5),
                **plan,
            }
            return [bool(plan["heat"]) and settings.heaters[index].enabled for index in range(SLOT_COUNT)]
        current = list(self._heaters[:SLOT_COUNT])
        while len(current) < SLOT_COUNT:
            current.append(False)
        if (pulse := self._hold_pulse(settings)) is not None:
            states = self._sustain_states(settings, primary, pulse)
            duration, reheat = pulse
            target = float(settings.target_temp_f)
            self._decision = {
                "path": "sustain",
                "heat": any(
                    states[index] and settings.heaters[index].enabled for index in range(SLOT_COUNT)
                ),
                "target_f": target,
                "shutoff_coast_f": round(self._last_shutoff_coast, 2),
                "slope_f_per_sec": None
                if self._last_shutoff_rate is None
                else round(float(self._last_shutoff_rate), 5),
                "cutoff_f": round(target - self._last_shutoff_coast, 2),
                "reheat_f": round(reheat, 2),
                "pulse_seconds": round(duration, 2),
                "hold_ready": self._hold_ready,
                "on_seconds": round(self._pulse_on_seconds(settings), 2),
            }
            return states
        cutoff = self._shutoff_coast(settings)
        target = float(settings.target_temp_f)
        mask = [
            heater_should_be_on(
                primary,
                target,
                bool(current[index]),
                early_cutoff_f=cutoff,
            )
            and settings.heaters[index].enabled
            for index in range(SLOT_COUNT)
        ]
        self._decision = {
            "path": "cutoff",
            "heat": any(mask),
            "target_f": target,
            "shutoff_coast_f": round(cutoff, 2),
            "slope_f_per_sec": None
            if self._last_shutoff_rate is None
            else round(float(self._last_shutoff_rate), 5),
            "soak": round(self._sheath_soak(settings), 3),
            "cutoff_f": round(target - cutoff, 2),
            "on_seconds": round(self._pulse_on_seconds(settings), 2),
        }
        return mask

    def _note_timeline(self, dt: float, settings: Settings) -> None:
        """Save one reading per sample interval, in every mode.

        A stalled clock can hand in a large ``dt``. That still records the
        temperature we have now, not a burst of copies of the same reading.
        """

        self._since_timeline += dt
        self._since_timeline_compact += dt
        if self._since_timeline >= SAMPLE_INTERVAL_SECONDS:
            self._since_timeline %= SAMPLE_INTERVAL_SECONDS
            self._record_timeline_point(settings)
        if self._since_timeline_compact >= TIMELINE_COMPACT_SECONDS:
            self._since_timeline_compact = 0.0
            self.store.compact_timeline()

    def _record_timeline_point(self, settings: Settings | None = None) -> None:
        if settings is None:
            settings = self.store.get_settings()
        self.store.add_timeline_point(self._sample_temps(), self._aim_temperature(settings))

    def _heater_mask(self, settings: Settings) -> list[bool]:
        """Enabled heaters that are actually on. Disabled slots stay false."""

        heaters = [False] * SLOT_COUNT
        for index, on in enumerate(self._heaters[:SLOT_COUNT]):
            heaters[index] = bool(on) and settings.heaters[index].enabled
        return heaters

    def _dwell_kind(self, settings: Settings, heaters: list[bool]) -> str | None:
        """Set when the minimum on or off time is holding the SSR against the math."""

        held_on = False
        held_off = False
        for index in range(SLOT_COUNT):
            if not settings.heaters[index].enabled:
                continue
            want = bool(self._desired[index]) if index < len(self._desired) else False
            got = bool(heaters[index]) if index < len(heaters) else False
            if got and not want:
                held_on = True
            if want and not got:
                held_off = True
        if held_on and held_off:
            return "both"
        if held_on:
            return "min_on"
        if held_off:
            return "min_off"
        return None

    def _control_detail(self, settings: Settings, heaters: list[bool] | None = None) -> dict:
        """Temperature, heater state, and the calculation the last decision used."""

        if heaters is None:
            heaters = self._heater_mask(settings)
        found = self._current_control(settings)
        decision = dict(self._decision)
        path = str(decision.get("path") or "run")
        if path in {"pause", "stop"}:
            phase = path
        elif self._mode == "cooling" or path == "cooldown":
            phase = "cooldown"
        elif self._active_phase() is not None:
            phase = self._active_phase()
        else:
            phase = "run"
        detail: dict = {
            "phase": phase,
            "path": path,
            "temp_f": None if found is None else round(found[1], 2),
            "target_f": decision.get("target_f", round(self._aim_temperature(settings), 2)),
            "heaters_on": list(heaters),
            "heat": bool(decision.get("heat")),
        }
        detail.update(decision_fields(decision))
        if "on_seconds" not in detail and path not in {"pause", "stop", "cooldown"}:
            detail["on_seconds"] = round(self._pulse_on_seconds(settings), 2)
        off = self._coast_off_seconds(settings)
        if off and path not in {"pause", "stop"}:
            detail["off_seconds"] = round(off, 2)
        live = slope_f_per_sec(self._predict_samples)
        if live is not None and "live_slope_f_per_sec" not in detail:
            detail["live_slope_f_per_sec"] = round(live, 5)
        dwell = self._dwell_kind(settings, heaters)
        if dwell:
            detail["dwell"] = dwell
        if path not in {"pause", "stop"}:
            forecast = self._forecast(settings)
            if forecast["seconds_to_target"] is not None:
                detail["seconds_to_target"] = forecast["seconds_to_target"]
            if forecast["seconds_to_cooldown"] is not None:
                detail["seconds_to_cooldown"] = forecast["seconds_to_cooldown"]
        model = self._session.model if self._session is not None else self._run_model
        numbers = model_numbers(None if model is None else model.to_dict())
        if numbers:
            detail["model"] = numbers
        return detail

    def _log(
        self,
        event: str,
        message: str,
        detail: dict | None = None,
        *,
        run_id: int | None = None,
        elapsed: float | None = None,
    ) -> None:
        """Store one diagnostic line on the open run, or on ``run_id`` after it closed."""

        target = run_id
        if target is None:
            target = self._run_id if self._run_id is not None else self._cooldown_run_id
        if target is None:
            return
        if elapsed is None and run_id is None:
            elapsed = self._cooldown_elapsed if self._mode == "cooling" else self._elapsed
        payload = clean_detail(detail)
        self.store.add_run_log(target, elapsed, event, message, payload)
        if event != "sample":
            logger.info(
                "Run %s %s",
                target,
                message,
                extra={"brewski_run_id": target, "brewski_elapsed": elapsed},
            )

    def _note_control_log(self, dt: float, settings: Settings) -> None:
        """Save the control reading about once a second.

        A stalled clock can hand in a large ``dt``. That still records the
        temperature we have now, not copies of the same reading.
        """

        self._since_log += dt
        if self._since_log < LOG_INTERVAL_SECONDS:
            return
        self._since_log %= LOG_INTERVAL_SECONDS
        self._record_control_log(settings)

    def _record_control_log(self, settings: Settings) -> None:
        """One diagnostic line for the temperature and calculation right now."""

        if self._run_id is None and self._cooldown_run_id is None:
            return
        if self._mode == "cooling":
            self._decision = {"path": "cooldown", "heat": False}
            heaters = [False] * SLOT_COUNT
            detail = self._control_detail(settings, heaters)
            if settings.learned_cooldown_f_per_min is not None:
                detail["learned_rate_f_per_min"] = settings.learned_cooldown_f_per_min
        else:
            detail = self._control_detail(settings)
        self._log("sample", format_control(detail), detail)

    def _log_output_changes(self, settings: Settings, phase_before: str | None) -> None:
        """Record a stage change or an SSR change as soon as it happens."""

        if self._run_id is None:
            return
        phase = self._active_phase()
        if phase_before is not None and phase is not None and phase != phase_before:
            detail = self._control_detail(settings)
            self._log("phase", phase_message(phase, detail), detail)
        applied = self._heater_mask(settings)
        if self._logged_heaters is not None and applied != self._logged_heaters:
            detail = self._control_detail(settings)
            self._log("heat", format_control(detail), detail)
        self._logged_heaters = applied

    def _record_sample(self, settings: Settings) -> None:
        if self._run_id is None:
            return
        # Samples stay aligned to the physical slots. A disabled heater is off.
        temps = self._sample_temps()
        heaters = [False] * SLOT_COUNT
        for index, on in enumerate(self._heaters[:SLOT_COUNT]):
            heaters[index] = bool(on) and settings.heaters[index].enabled
        phase = self._active_phase() or "run"
        self.store.add_sample(
            run_id=self._run_id,
            elapsed_seconds=round(self._elapsed, 2),
            temps_f=temps,
            heaters_on=heaters,
            target_temp_f=self._aim_temperature(settings),
            phase=phase,
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
        rate = self._temperature_rate()
        return {
            "mode": self._mode,
            "source": getattr(self.source, "name", "unknown"),
            "elapsed_seconds": round(
                self._cooldown_elapsed if self._mode == "cooling" else self._elapsed,
                1,
            ),
            "target_temp_f": int(self._aim_temperature(settings)),
            **self._forecast(settings),
            "learned_coast_f": settings.learned_coast_f,
            "learned_coast_seconds": settings.learned_coast_seconds,
            "learned_cooldown_f_per_min": settings.learned_cooldown_f_per_min,
            "calibration_count": model_from_dict(settings.thermal_model).calibration_count,
            "calibration_step": self._calibration_lcd().get("step"),
            "predicted_crest_f": None
            if self._session is None or self._session.predicted_crest is None
            else round(self._session.predicted_crest, 1),
            "charge_gallons": self._charge_gallons
            if self._mode in ("running", "paused") and self._run_kind == "calibration"
            else settings.charge_gallons,
            "run_kind": self._run_kind if self._mode in ("running", "paused") else None,
            "test": self._test_payload(),
            "coast_f": self._coast_f if self._mode in ("running", "paused") else None,
            "coast_seconds": self._coast_seconds if self._mode in ("running", "paused") else None,
            "calibration_settled": self._calibration_settled
            if self._mode in ("running", "paused")
            else False,
            "temps_f": temps,
            "rate_f_per_sec": None if rate is None else round(rate, 5),
            "rate_f_per_min": None if rate is None else round(rate * 60.0, 1),
            "projection": self._projection(settings),
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
            "update_source": settings.update_source,
        }


    def _projection_model(self, settings: Settings) -> ThermalModel | None:
        """Coast the chart should use, from this run or the last one saved."""

        session = self._session
        if session is not None and (session.guess.coast_rise_f or 0.0) > 0:
            return session.guess
        if session is not None and (session.model.coast_rise_f or 0.0) > 0:
            return session.model
        if self._run_model is not None and (self._run_model.coast_rise_f or 0.0) > 0:
            return self._run_model
        saved = model_from_dict(settings.thermal_model)
        if (saved.coast_rise_f or 0.0) > 0:
            return saved
        if settings.learned_coast_f and settings.learned_coast_seconds:
            return ThermalModel(
                coast_rise_f=settings.learned_coast_f,
                coast_seconds=settings.learned_coast_seconds,
                cooldown_f_per_min=settings.learned_cooldown_f_per_min,
            )
        return saved if saved.cooldown_f_per_min else None

    def _projection_slope(self, settings: Settings) -> float:
        """Slope the shutoff guess is using right now.

        While an element is on, that is the slope a cutoff at this instant
        would carry onto the coast. While the elements are already open, a
        falling probe keeps falling. A rise still on the coast is limited
        separately to the degrees left since shutoff.
        """

        live = slope_f_per_sec(self._predict_samples)
        if self._session is not None:
            modeled = self._session._control_slope(self._elapsed)
        elif self._run_model is not None and self._run_model.ready():
            modeled = self._sheath_slope
            if live is not None and live > modeled:
                modeled = live
        else:
            modeled = 0.0 if live is None else live
        if self._elements_on(settings):
            model = self._projection_model(settings)
            if model is None:
                return modeled
            return coast_entry_slope(model, self._pulse_on_seconds(settings), modeled)
        if live is not None:
            return live
        return modeled

    def _coast_origin(self) -> tuple[float, float | None]:
        """Shutoff slope and temperature for the coast the heaters are on."""

        session = self._session
        if (
            session is not None
            and session.open_temp is not None
            and session.shutoff_slope > 0
            and (session.phase in {"coast", "pulse", "pulses", "hold"} or session.pulse_coasting)
        ):
            return session.shutoff_slope, session.open_temp
        if self._coast_open_slope > 0 and self._coast_open_temp is not None:
            return self._coast_open_slope, self._coast_open_temp
        return 0.0, None

    def _unspent_coast_f(self, temp_f: float, model: ThermalModel) -> float | None:
        """Degrees still ahead on the coast that started when the heaters opened.

        The live slope stays steep after the SSR opens, and treating it as a
        new shutoff stacks another full rise on top of temperature the pot
        has already gained. The crest stays the one chosen at shutoff.
        """

        shutoff, opened = self._coast_origin()
        if shutoff <= 0 or opened is None:
            return None
        full = remaining_coast_f(model, shutoff)
        climbed = max(0.0, temp_f - opened)
        return max(0.0, full - climbed)

    def _projection(self, settings: Settings) -> list[dict[str, float]]:
        found = self._current_control(settings)
        if found is None:
            return []
        model = self._projection_model(settings)
        cool = settings.learned_cooldown_f_per_min
        if model is not None and model.cooldown_f_per_min is not None:
            cool = model.cooldown_f_per_min
        slope = self._projection_slope(settings)
        remaining = None
        if model is not None and not self._elements_on(settings) and slope >= 0:
            remaining = self._unspent_coast_f(found[1], model)
        return [
            {"offset_s": stamp, "temp_f": round(reading, 2)}
            for stamp, reading in project_temperature(
                found[1],
                slope,
                model,
                cool,
                remaining_f=remaining,
            )
        ]

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
            if snapshot.get("test") and snapshot["test"].get("review"):
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
        test_id: str | None = None
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
                if action == "test":
                    test_id = menu.chosen_test
                changed = True
        if changed:
            self._refresh_display()
        if action == "check":
            self._start_menu_check(generation)
        elif action == "apply":
            self._install_from_menu()
        elif action == "reboot":
            self._reboot_from_menu()
        elif action == "test":
            self._start_suite(test_id or "")
        return changed

    def _start_suite(self, test_id: str) -> None:
        try:
            self.start({"kind": "test", "test": test_id})
        except ControllerError as exc:
            logger.warning("Could not start the test from the menu")
            with self._lock:
                menu = self._menu
                if menu is not None:
                    if "End the test" in exc.message:
                        menu.show_notice("Result still up", "End it first")
                    elif "already open" in exc.message:
                        menu.show_notice("Run is open", "")
                    else:
                        menu.show_notice("Could not start", "")
            self._refresh_display()
            return
        with self._lock:
            self._menu = None
        self._refresh_display()

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
            release = check_release(fresh=True)
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
        if self._show_panel_step:
            self._show_startup("Opening panel")
            self._show_panel_step = False
        self._panel = open_panel(settings)

    def _close_panel(self) -> None:
        panel = self._panel
        self._panel = None
        self._panel_key = None
        if panel is not None:
            panel.close()  # type: ignore[attr-defined]
