"""Heating model and look-ahead used by calibration and later runs.

Soak is an exponential lag: the slope climbs as ``R (1 - e^{-t/τ})`` until
the rise is straight. After the SSR opens, temperature climbs as
``S τ (1 - e^{-t/τ})`` and levels at an equilibrium. Calibration learns those
shapes while the probe is still under 140°F, then one predicted pulse climbs
to 160°F. The ten-minute hold stays there with minimum pulses that include
the coast those pulses store.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The learning coast is scored against this after the heaters are already off.
LEARN_TEMP_F = 140.0
# The climb pulse and the ten-minute hold aim here. The dial setpoint belongs
# to the next real run.
CALIBRATION_TEMP_F = 160.0
# First guess when no saved model exists, from the open-loop capture in run 10.
SEED_LINEAR_RATE_F_PER_SEC = 0.40
SEED_SOAK_TAU_SECONDS = 37.0
SEED_COAST_TAU_SECONDS = 35.0
SEED_COAST_RISE_F = 14.0
# A heat that never shows a settled linear rate still opens under 140°F.
LEARN_HEAT_MAX_SECONDS = 15 * 60.0
# One probe reading may move a variable only this far.
TUNE_DEADBAND_F = 0.3
TUNE_SLOPE_DEADBAND = 0.01
TUNE_FRACTION = 0.10
TUNE_RATE_CAP_F_PER_SEC = 0.01
TUNE_TAU_CAP_SECONDS = 2.0
TUNE_RISE_CAP_F = 0.4
# A cooldown is over, and the pot is safe to handle, under this temperature.
SAFE_TEMP_F = 100.0
SETTLE_SECONDS = 3.0
LINEAR_HOLD_SECONDS = 15.0
MOMENTUM_SECONDS = 2 * 60.0
HOLD_SECONDS = 10 * 60.0
# Inside this many degrees of the target, regulation trims with pulses.
# Farther below, the climb has the heaters.
HOLD_BAND_F = 1.0
# The crest has reached the setpoint once it is this close. Stopping at half
# a degree left the pot short of the target on every approach.
ARRIVAL_F = 0.1
# A new hold pulse waits while the probe is still climbing this fast.
# That rise is the previous pulse's coast, and another pulse would stack on it.
HOLD_RISE_F_PER_SEC = 0.02
HOLD_SLOPE_WINDOW_SECONDS = 6.0
HOLD_SLOPE_MIN_SPAN_SECONDS = 4.0
# Stage I must not walk the pot up to the target. Leave the cold start here.
COLD_START_MAX_SECONDS = 3 * 60.0
SLOPE_WINDOW_SECONDS = 8.0
LINEAR_STABLE_SECONDS = 5.0
# °F/s². A startup parabola keeps bending faster than this. Linear heat does not.
LINEAR_ACCEL_MAX = 0.001
# Initial Heating waits out the cold sheath. A few flat seconds can still be
# the shallow start, and the rate can keep climbing after that.
LINEAR_CONFIRM_SECONDS = 45.0
LINEAR_RISE_SECONDS = 15.0
# °F/s² of the least-squares line through the watched slopes. Above this, the
# rise is still steepening. A short pair of noisy samples can look steeper
# than this without the sheath still soaking.
LINEAR_SETTLED_ACCEL = 0.0005
# °F/s. The settled rate has to sit with the steepest rate already seen.
LINEAR_SETTLED_BAND = 0.02
PREDICT_MIN_SAMPLES = 4
PREDICT_MIN_SPAN_SECONDS = 8.0
PREDICT_MIN_RATE_F_PER_SEC = 0.01
# Extra degrees assumed while a pulse is still shorter than the slope window,
# so a short look-ahead shuts off early instead of late.
EARLY_UNCERTAINTY_F = 0.5
COAST_AVERAGE_SECONDS = 10.0
COAST_PEAK_DROP_F = 0.4
COAST_PEAK_CONFIRM_SECONDS = 10.0
COAST_PLATEAU_RISE_F = 0.2
COAST_PLATEAU_SECONDS = 90.0
COAST_WATCH_MAX_SECONDS = 20 * 60.0
COOLDOWN_RATE_MAX = 120.0
# Grok may replace a local fit only inside this band.
GROK_BAND_FRACTION = 0.35
GROK_RATE_BAND_F_PER_SEC = 0.02
GROK_RATE_BAND_F_PER_MIN = 0.4
GROK_RISE_BAND_F = 1.0


@dataclass
class ThermalModel:
    """Exponential soak, straight rate, and leveling coast learned across calibrations."""

    cold_start_seconds: float | None = None
    # °F/s². Older calibrations stored the soak as ΔT = cold_start_quad * t².
    cold_start_quad: float | None = None
    # Seconds. While the sheath is soaking, the slope climbs as R (1 - e^{-t/τ}).
    soak_tau_seconds: float | None = None
    linear_rate_f_per_sec: float | None = None
    coast_rise_f: float | None = None
    coast_seconds: float | None = None
    # Seconds. After the SSR opens, ΔT = S τ (1 - e^{-t/τ}) and levels at S τ.
    coast_tau_seconds: float | None = None
    # °F/s², negative. Older calibrations stored the coast as a downward parabola.
    coast_quad: float | None = None
    # Probe temperature where coast_rise_f was measured. A warmer shutoff runs further.
    coast_at_f: float | None = None
    cooldown_f_per_min: float | None = None
    # How many calibrations have been folded into these numbers.
    calibration_count: int | None = None

    def ready(self) -> bool:
        """True when a look-ahead has both a heating rate and a coast."""

        return (
            self.linear_rate_f_per_sec is not None
            and self.linear_rate_f_per_sec > 0
            and self.coast_rise_f is not None
            and self.coast_rise_f >= 0
        )

    def to_dict(self) -> dict:
        data = {
            "cold_start_seconds": self.cold_start_seconds,
            "cold_start_quad": self.cold_start_quad,
            "soak_tau_seconds": self.soak_tau_seconds,
            "linear_rate_f_per_sec": self.linear_rate_f_per_sec,
            "coast_rise_f": self.coast_rise_f,
            "coast_seconds": self.coast_seconds,
            "coast_tau_seconds": self.coast_tau_seconds,
            "coast_quad": self.coast_quad,
            "coast_at_f": self.coast_at_f,
            "cooldown_f_per_min": self.cooldown_f_per_min,
        }
        if self.calibration_count:
            data["calibration_count"] = int(self.calibration_count)
        return data


def model_from_dict(data: dict | None) -> ThermalModel:
    """A missing or partial document is an empty model, not an error."""

    if not isinstance(data, dict):
        return ThermalModel()

    def optional(key: str) -> float | None:
        value = data.get(key)
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return number

    count = data.get("calibration_count")
    try:
        calibration_count = int(count) if count is not None else None
    except (TypeError, ValueError):
        calibration_count = None
    if calibration_count is not None and calibration_count <= 0:
        calibration_count = None

    model = ThermalModel(
        cold_start_seconds=optional("cold_start_seconds"),
        cold_start_quad=optional("cold_start_quad"),
        soak_tau_seconds=optional("soak_tau_seconds"),
        linear_rate_f_per_sec=optional("linear_rate_f_per_sec"),
        coast_rise_f=optional("coast_rise_f"),
        coast_seconds=optional("coast_seconds"),
        coast_tau_seconds=optional("coast_tau_seconds"),
        coast_quad=optional("coast_quad"),
        coast_at_f=optional("coast_at_f"),
        cooldown_f_per_min=optional("cooldown_f_per_min"),
        calibration_count=calibration_count,
    )
    ensure_time_constants(model)
    return model


def scale_model(
    model: ThermalModel,
    learned_gallons: float | None,
    new_gallons: float | None,
) -> ThermalModel:
    """Scale degrees and rates when the charge volume changes.

    The same stored heat moves a smaller charge further. How long the sheath
    takes to heat does not change with the gallons.
    """

    if learned_gallons is None or new_gallons is None or learned_gallons == new_gallons:
        return model
    if learned_gallons <= 0 or new_gallons <= 0:
        return model
    factor = learned_gallons / new_gallons

    def scaled(value: float | None, digits: int = 5) -> float | None:
        if value is None:
            return None
        return round(value * factor, digits)

    rise = scaled(model.coast_rise_f, 2)
    cool = model.cooldown_f_per_min
    if cool is not None:
        cool = round(min(COOLDOWN_RATE_MAX, max(0.0, cool * factor)), 2)
    quad = scaled(model.coast_quad, 6)
    if rise is not None and model.coast_seconds and model.coast_seconds > 0:
        quad = round(-rise / (model.coast_seconds ** 2), 6)
    scaled_model = ThermalModel(
        cold_start_seconds=model.cold_start_seconds,
        cold_start_quad=scaled(model.cold_start_quad, 6),
        soak_tau_seconds=model.soak_tau_seconds,
        linear_rate_f_per_sec=scaled(model.linear_rate_f_per_sec),
        coast_rise_f=rise,
        coast_seconds=model.coast_seconds,
        coast_tau_seconds=model.coast_tau_seconds,
        coast_quad=quad,
        coast_at_f=model.coast_at_f,
        cooldown_f_per_min=cool,
        calibration_count=model.calibration_count,
    )
    ensure_time_constants(scaled_model)
    return scaled_model


_BLEND_FIELDS = (
    ("cold_start_seconds", 2),
    ("cold_start_quad", 6),
    ("soak_tau_seconds", 2),
    ("linear_rate_f_per_sec", 5),
    ("coast_rise_f", 2),
    ("coast_seconds", 2),
    ("coast_tau_seconds", 2),
    ("coast_at_f", 2),
    ("cooldown_f_per_min", 2),
)


def model_weight(model: ThermalModel) -> int:
    """How many calibrations the saved numbers already speak for."""

    if model.calibration_count and model.calibration_count > 0:
        return int(model.calibration_count)
    measured = [getattr(model, name) for name, _digits in _BLEND_FIELDS]
    if any(value is not None for value in measured):
        return 1
    return 0


def blend_model(prior: ThermalModel, measured: ThermalModel) -> ThermalModel:
    """Fold one calibration into the model saved by the earlier ones.

    Each calibration has the same weight. A stage this run did not measure
    keeps the earlier number. The first calibration is stored as measured.
    """

    weight = model_weight(prior)
    blended = ThermalModel()
    used = False
    for name, digits in _BLEND_FIELDS:
        new = getattr(measured, name)
        old = getattr(prior, name)
        if new is None:
            setattr(blended, name, old)
            continue
        used = True
        if old is None or weight <= 0:
            setattr(blended, name, round(float(new), digits))
        else:
            setattr(blended, name, round((float(old) * weight + float(new)) / (weight + 1), digits))
    if blended.coast_rise_f is not None and blended.coast_seconds and blended.coast_seconds > 0:
        blended.coast_quad = round(-blended.coast_rise_f / (blended.coast_seconds ** 2), 6)
    else:
        blended.coast_quad = measured.coast_quad if measured.coast_quad is not None else prior.coast_quad
    ensure_time_constants(blended)
    if not used:
        return prior
    blended.calibration_count = weight + 1
    return blended


def endpoint_rate(samples: list[tuple[float, float]]) -> float | None:
    """°F/s from the first sample to the last. Positive is a rise."""

    if len(samples) < 2:
        return None
    start_t, start_v = samples[0]
    end_t, end_v = samples[-1]
    dt = end_t - start_t
    if dt <= 0:
        return None
    return (end_v - start_v) / dt


def rolling_slopes(
    samples: list[tuple[float, float]],
    *,
    window: float = SLOPE_WINDOW_SECONDS,
) -> list[tuple[float, float]]:
    """End-point slope at each sample that closes a full window.

    ``samples`` are in time order. The slope only needs the first and last
    point of each window, so a long Initial heating does not rescan the whole
    curve on every reading.
    """

    slopes: list[tuple[float, float]] = []
    start = 0
    count = len(samples)
    for end, (stamp, _value) in enumerate(samples):
        limit = stamp - window
        while start < end and samples[start][0] < limit:
            start += 1
        right = end
        while right + 1 < count and samples[right + 1][0] <= stamp:
            right += 1
        if right - start + 1 < PREDICT_MIN_SAMPLES:
            continue
        if samples[right][0] - samples[start][0] < window * 0.7:
            continue
        rate = endpoint_rate((samples[start], samples[right]))
        if rate is None:
            continue
        slopes.append((stamp, rate))
    return slopes


def linear_onset(samples: list[tuple[float, float]]) -> bool:
    """True once the rise has been straight, and still climbing, for a few seconds."""

    slopes = rolling_slopes(samples)
    if len(slopes) < 2:
        return False
    end = slopes[-1][0]
    recent = [(stamp, rate) for stamp, rate in slopes if stamp >= end - LINEAR_STABLE_SECONDS]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < LINEAR_STABLE_SECONDS - 1:
        return False
    if any(rate < PREDICT_MIN_RATE_F_PER_SEC for _stamp, rate in recent):
        return False
    span = recent[-1][0] - recent[0][0]
    if span <= 0:
        return False
    # A parabola has a steady acceleration. Linear heat has essentially none.
    acceleration = abs(recent[-1][1] - recent[0][1]) / span
    return acceleration <= LINEAR_ACCEL_MAX


def _ols_slope(samples: list[tuple[float, float]]) -> float:
    """°F per second of the least-squares line through ``samples``."""

    count = len(samples)
    if count < 2:
        return 0.0
    mean_x = sum(stamp for stamp, _rate in samples) / count
    mean_y = sum(rate for _stamp, rate in samples) / count
    numerator = sum((stamp - mean_x) * (rate - mean_y) for stamp, rate in samples)
    denominator = sum((stamp - mean_x) ** 2 for stamp, _rate in samples)
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def slope_has_settled(samples: list[tuple[float, float]]) -> bool:
    """True once the heating rate has stopped getting steeper.

    A few flat seconds are not enough. The rate has to be watched for
    ``LINEAR_CONFIRM_SECONDS``, the last ``LINEAR_RISE_SECONDS`` must no
    longer be climbing, and that rate has to sit with the steepest rate
    seen so far. A shallow pause at the start stays in heating, because
    the line gets steeper once the sheath is soaked.
    """

    slopes = rolling_slopes(samples)
    if len(slopes) < 2:
        return False
    if slopes[-1][0] - slopes[0][0] < LINEAR_CONFIRM_SECONDS - 1:
        return False
    end = slopes[-1][0]
    recent = [(stamp, rate) for stamp, rate in slopes if stamp >= end - LINEAR_RISE_SECONDS]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < LINEAR_RISE_SECONDS * 0.8:
        return False
    if any(rate < PREDICT_MIN_RATE_F_PER_SEC for _stamp, rate in recent):
        return False
    confirm = [(stamp, rate) for stamp, rate in slopes if stamp >= end - LINEAR_CONFIRM_SECONDS]
    if len(confirm) < 4 or confirm[-1][0] - confirm[0][0] < LINEAR_CONFIRM_SECONDS - 1:
        return False
    # A straight line through the watched slopes. One noisy pair at the ends
    # of a short window is not a climb.
    if _ols_slope(confirm) > LINEAR_SETTLED_ACCEL:
        return False
    rates = sorted(rate for _stamp, rate in slopes)
    peak = rates[min(len(rates) - 1, int(len(rates) * 0.9))]
    recent_rate = sum(rate for _stamp, rate in recent) / len(recent)
    return recent_rate >= peak - LINEAR_SETTLED_BAND


def _lag_rise(elapsed: float, tau: float) -> float:
    """Degrees one °F/s of soaked heat would add by ``elapsed`` seconds."""

    if tau <= 0:
        return elapsed
    return elapsed - tau * (1.0 - math.exp(-elapsed / tau))


def fit_heating_lag(samples: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Asymptotic rate (°F/s) and time constant (s) while the element soaks.

    From a cold element the heating rate climbs as ``R (1 - e^{-t/τ})``, so

        ΔT = R (t - τ (1 - e^{-t/τ}))

    ``R`` is the straight rate once the element is hot. ``τ`` is how fast it
    gets there. Opening the heaters runs the same exponential backward: the
    degrees still to come are the rate at shutoff times ``τ``. Returns None
    when the rise is already straight, or when ``τ`` is longer than this
    window can see. A short curve can be fit by a huge rate and a huge time
    constant at once, and that pair invents a coast the pot never makes.
    """

    if len(samples) < 8:
        return None
    origin_t, origin_v = samples[0]
    points: list[tuple[float, float]] = []
    for stamp, reading in samples:
        elapsed = stamp - origin_t
        if elapsed < 0:
            continue
        points.append((elapsed, reading - origin_v))
    if len(points) < 8 or points[-1][0] < 20.0:
        return None
    duration = points[-1][0]
    early = [(elapsed, rise) for elapsed, rise in points if elapsed <= duration * 0.25]
    late = [(elapsed, rise) for elapsed, rise in points if elapsed >= duration * 0.75]
    early_rate = endpoint_rate(early)
    late_rate = endpoint_rate(late)
    if (
        early_rate is None
        or late_rate is None
        or late_rate <= 0
        or late_rate < early_rate * 1.15
    ):
        return None

    # Soak at the end has to be past about 70%. Below that, a steeper rate
    # and a longer time constant fit the same curve, and the coast runs away.
    tau_cap = duration / 1.2
    if tau_cap < 8.0:
        return None
    best: tuple[float, float] | None = None
    best_error: float | None = None
    tau = 4.0
    while tau <= tau_cap:
        numerator = 0.0
        denominator = 0.0
        for elapsed, rise in points:
            basis = _lag_rise(elapsed, tau)
            numerator += rise * basis
            denominator += basis * basis
        if denominator <= 0:
            tau *= 1.15
            continue
        rate = numerator / denominator
        if not math.isfinite(rate) or rate <= 0:
            tau *= 1.15
            continue
        error = 0.0
        for elapsed, rise in points:
            miss = rise - rate * _lag_rise(elapsed, tau)
            error += miss * miss
        if best_error is None or error < best_error:
            best_error = error
            best = (rate, tau)
        tau *= 1.15
    if best is None or best_error is None:
        return None

    line_numerator = sum(rise * elapsed for elapsed, rise in points)
    line_denominator = sum(elapsed * elapsed for elapsed, rise in points)
    if line_denominator <= 0:
        return None
    line = line_numerator / line_denominator
    line_error = sum((rise - line * elapsed) ** 2 for elapsed, rise in points)
    if line_error <= 0 or best_error > line_error * 0.85:
        return None
    rate, tau = best
    # A minimum parked on the cap means a longer time constant would still
    # fit. The two numbers are not identified.
    if tau > tau_cap / 1.15:
        return None
    modeled_end = rate * (1.0 - math.exp(-duration / tau))
    if abs(modeled_end - late_rate) / late_rate > 0.35:
        return None
    return rate, tau


def fit_soak_tau(
    samples: list[tuple[float, float]],
    rate_f_per_sec: float,
) -> float | None:
    """Soak time constant (s) for a known straight rate.

    The rate is the settled slope. This searches ``τ`` in
    ``ΔT = R (t − τ (1 − e^{−t/τ}))`` and keeps the one that tracks the
    probe. Returns None until the window is long enough to tell a soak from
    a straight line, or when every ``τ`` misses the probe by more than a few
    degrees. A collapsed live walk is not a substitute for this fit.
    """

    if rate_f_per_sec <= 0 or len(samples) < 8:
        return None
    origin_t, origin_v = samples[0]
    points: list[tuple[float, float]] = []
    for stamp, reading in samples:
        elapsed = stamp - origin_t
        if elapsed < 0:
            continue
        points.append((elapsed, reading - origin_v))
    if len(points) < 8 or points[-1][0] < 20.0:
        return None
    best_error: float | None = None
    best_tau: float | None = None
    tau = 4.0
    limit = min(180.0, points[-1][0])
    while tau <= limit:
        error = 0.0
        for elapsed, rise in points:
            miss = rise - rate_f_per_sec * _lag_rise(elapsed, tau)
            error += miss * miss
        if best_error is None or error < best_error:
            best_error = error
            best_tau = tau
        tau *= 1.08
    if best_error is None or best_tau is None:
        return None
    if math.sqrt(best_error / len(points)) > 3.0:
        return None
    return best_tau


def heating_lag_coast(rate_f_per_sec: float, tau_seconds: float, heated_seconds: float) -> float:
    """Degrees still to come if the heaters open after ``heated_seconds``.

    The element is holding ``rate * τ * (1 - e^{-t/τ})`` degrees of climb.
    At full soak that is ``rate * τ``.
    """

    if rate_f_per_sec <= 0 or tau_seconds <= 0 or heated_seconds <= 0:
        return 0.0
    soaked = 1.0 - math.exp(-heated_seconds / tau_seconds)
    return rate_f_per_sec * tau_seconds * soaked


def ensure_time_constants(model: ThermalModel) -> None:
    """Fill a missing coast time constant from the rise and the straight rate."""

    rate = model.linear_rate_f_per_sec or 0.0
    rise = model.coast_rise_f or 0.0
    if (model.coast_tau_seconds is None or model.coast_tau_seconds <= 0) and rate > 0 and rise > 0:
        model.coast_tau_seconds = round(rise / rate, 2)


def coast_tau_of(model: ThermalModel) -> float:
    """Seconds the coast takes to spend its slope. Zero when that is unknown."""

    if model.coast_tau_seconds is not None and model.coast_tau_seconds > 0:
        return model.coast_tau_seconds
    rate = model.linear_rate_f_per_sec or 0.0
    rise = model.coast_rise_f or 0.0
    if rate > 0 and rise > 0:
        return rise / rate
    return 0.0


def coast_settle_seconds(tau_seconds: float, rise_f: float) -> float:
    """Seconds for an exponential coast to come within ``ARRIVAL_F`` of its crest.

    Three time constants still leave about five percent of the rise, which is
    half a degree on a typical coast. This waits until that tail is a tenth.
    """

    tau = max(0.0, float(tau_seconds))
    rise = max(0.0, float(rise_f))
    if tau <= 0 or rise <= ARRIVAL_F:
        return 0.0
    return tau * math.log(rise / ARRIVAL_F)


def settled_linear_rate(samples: list[tuple[float, float]]) -> float | None:
    """°F/s once the heating rate has stopped getting steeper."""

    if not slope_has_settled(samples):
        return None
    slopes = rolling_slopes(samples)
    if not slopes:
        return None
    end = slopes[-1][0]
    recent = [rate for stamp, rate in slopes if stamp >= end - LINEAR_RISE_SECONDS]
    if not recent:
        return None
    return sum(recent) / len(recent)


def fit_coast_exponential(
    samples: list[tuple[float, float]],
    shutoff_slope: float,
) -> tuple[float, float] | None:
    """``(rise °F, τ seconds)`` for ``ΔT = S0 · τ · (1 - e^{-t/τ})``.

    ``S0`` is the slope at shutoff. The curve approaches ``S0 · τ`` and stays
    there. Returns None until the window can see the bend.
    """

    if shutoff_slope <= 0 or len(samples) < 4:
        return None
    origin_t, origin_v = samples[0]
    points: list[tuple[float, float]] = []
    for stamp, reading in samples[1:]:
        elapsed = stamp - origin_t
        if elapsed <= 0:
            continue
        points.append((elapsed, reading - origin_v))
    if len(points) < 4 or points[-1][0] < 8.0:
        return None
    best: tuple[float, float, float] | None = None
    tau = 4.0
    while tau <= 180.0:
        rise = shutoff_slope * tau
        error = 0.0
        for elapsed, gained in points:
            miss = gained - rise * (1.0 - math.exp(-elapsed / tau))
            error += miss * miss
        if best is None or error < best[0]:
            best = (error, rise, tau)
        tau *= 1.08
    if best is None:
        return None
    return best[1], best[2]


def seed_model(prior: ThermalModel | None = None) -> ThermalModel:
    """The projection a calibration starts from.

    A saved model is that guess. With nothing saved, the numbers are the
    open-loop capture from run 10.
    """

    if (
        prior is not None
        and (prior.linear_rate_f_per_sec or 0.0) > 0
        and (prior.coast_rise_f or 0.0) > 0
    ):
        seeded = ThermalModel(
            cold_start_seconds=prior.cold_start_seconds,
            cold_start_quad=prior.cold_start_quad,
            soak_tau_seconds=prior.soak_tau_seconds,
            linear_rate_f_per_sec=prior.linear_rate_f_per_sec,
            coast_rise_f=prior.coast_rise_f,
            coast_seconds=prior.coast_seconds,
            coast_tau_seconds=prior.coast_tau_seconds,
            coast_quad=prior.coast_quad,
            coast_at_f=prior.coast_at_f,
            cooldown_f_per_min=prior.cooldown_f_per_min,
            calibration_count=prior.calibration_count,
        )
        if seeded.soak_tau_seconds is None or seeded.soak_tau_seconds <= 0:
            seeded.soak_tau_seconds = SEED_SOAK_TAU_SECONDS
        ensure_time_constants(seeded)
        return seeded
    return ThermalModel(
        linear_rate_f_per_sec=SEED_LINEAR_RATE_F_PER_SEC,
        soak_tau_seconds=SEED_SOAK_TAU_SECONDS,
        coast_tau_seconds=SEED_COAST_TAU_SECONDS,
        coast_rise_f=SEED_COAST_RISE_F,
        coast_seconds=round(SEED_COAST_TAU_SECONDS * 3.0, 2),
    )


def _tune_step(error: float, deadband: float, value: float, cap: float, *, same_units: bool) -> float:
    """A capped step in the direction of ``error``. Zero inside the deadband."""

    if abs(error) <= deadband:
        return 0.0
    limit = cap
    if value:
        limit = min(cap, abs(value) * TUNE_FRACTION)
    if limit <= 0:
        return 0.0
    magnitude = min(abs(error), limit) if same_units else limit
    return magnitude if error > 0 else -magnitude


def tune_model(
    model: ThermalModel,
    *,
    heat_delta_f: float | None = None,
    slope_delta: float | None = None,
    rate_delta_f_per_sec: float | None = None,
    coast_delta_f: float | None = None,
    crest_delta_f: float | None = None,
) -> bool:
    """Move the guess toward the probe. One call stays inside the delta caps.

    A hotter pot raises the rate or the rise. A slope steeper than the soak
    formula shortens the soak time, because the sheath reached that slope
    sooner. A shallower slope lengthens it. ``rate_delta_f_per_sec`` is a
    slope that is already past the straight rate: the formula cannot exceed
    that rate, so the rate moves and the soak time does not. A coast that is
    ahead of the formula shortens the coast time and raises the rise.
    ``crest_delta_f`` is the equilibrium minus the check temperature: short
    lowers the rise, high raises it.
    """

    changed = False
    if model.linear_rate_f_per_sec is not None:
        # One rate step per reading. A slope already above the straight rate
        # owns it; the temperature miss is the other way in.
        rate_error = None
        rate_deadband = TUNE_DEADBAND_F
        if rate_delta_f_per_sec is not None and abs(rate_delta_f_per_sec) > TUNE_SLOPE_DEADBAND:
            rate_error = rate_delta_f_per_sec
            rate_deadband = TUNE_SLOPE_DEADBAND
        elif heat_delta_f is not None:
            rate_error = heat_delta_f
        if rate_error is not None:
            step = _tune_step(
                rate_error,
                rate_deadband,
                model.linear_rate_f_per_sec,
                TUNE_RATE_CAP_F_PER_SEC,
                same_units=False,
            )
            if step:
                model.linear_rate_f_per_sec = round(max(0.0, model.linear_rate_f_per_sec + step), 5)
                changed = True
    if slope_delta is not None and model.soak_tau_seconds is not None:
        # Positive ``slope_delta`` means the probe is already steeper than
        # ``R (1 - e^{-t/τ})``. The soak is shorter than the guess.
        step = _tune_step(
            -slope_delta,
            TUNE_SLOPE_DEADBAND,
            model.soak_tau_seconds,
            TUNE_TAU_CAP_SECONDS,
            same_units=False,
        )
        if step:
            model.soak_tau_seconds = round(max(1.0, model.soak_tau_seconds + step), 2)
            changed = True
    if coast_delta_f is not None and abs(coast_delta_f) > TUNE_DEADBAND_F:
        if model.coast_tau_seconds is not None:
            # Ahead of the formula means the bend arrived early.
            step = _tune_step(
                -coast_delta_f,
                TUNE_DEADBAND_F,
                model.coast_tau_seconds,
                TUNE_TAU_CAP_SECONDS,
                same_units=False,
            )
            if step:
                model.coast_tau_seconds = round(max(1.0, model.coast_tau_seconds + step), 2)
                changed = True
        if model.coast_rise_f is not None:
            step = _tune_step(
                coast_delta_f,
                TUNE_DEADBAND_F,
                model.coast_rise_f,
                TUNE_RISE_CAP_F,
                same_units=True,
            )
            if step:
                model.coast_rise_f = round(max(0.0, model.coast_rise_f + step), 2)
                changed = True
    if crest_delta_f is not None and model.coast_rise_f is not None:
        step = _tune_step(
            crest_delta_f,
            TUNE_DEADBAND_F,
            model.coast_rise_f,
            TUNE_RISE_CAP_F,
            same_units=True,
        )
        if step:
            model.coast_rise_f = round(max(0.0, model.coast_rise_f + step), 2)
            changed = True
    if changed and coast_delta_f is not None:
        _pull_rise_toward_rate(model)
    return changed


def _pull_rise_toward_rate(model: ThermalModel) -> None:
    """After a full soak, the rise and ``R · τ`` describe the same coast."""

    rate = model.linear_rate_f_per_sec or 0.0
    tau = model.coast_tau_seconds or 0.0
    rise = model.coast_rise_f
    if rate <= 0 or tau <= 0 or rise is None:
        return
    gap = rate * tau - rise
    step = _tune_step(gap, TUNE_DEADBAND_F, rise, TUNE_RISE_CAP_F, same_units=True)
    if step:
        model.coast_rise_f = round(max(0.0, rise + step), 2)


def heating_formula_temp(origin_f: float, on_seconds: float, model: ThermalModel) -> float:
    """Probe temperature the soak-then-linear formula expects."""

    rate = model.linear_rate_f_per_sec or 0.0
    elapsed = max(0.0, on_seconds)
    if rate <= 0 or elapsed == 0:
        return origin_f
    tau = model.soak_tau_seconds or 0.0
    if tau <= 0:
        return origin_f + rate * elapsed
    return origin_f + rate * _lag_rise(elapsed, tau)


def formula_slope(on_seconds: float, model: ThermalModel) -> float:
    """°F/s the soak formula expects after ``on_seconds`` of heat."""

    rate = model.linear_rate_f_per_sec or 0.0
    if rate <= 0:
        return 0.0
    tau = model.soak_tau_seconds or 0.0
    elapsed = max(0.0, on_seconds)
    if tau <= 0:
        return rate
    return rate * (1.0 - math.exp(-elapsed / tau))


def fit_startup_quad(samples: list[tuple[float, float]]) -> float | None:
    """Least-squares ``a`` in ``ΔT = a t²`` from the first sample.

    ``a`` is °F/s². Positive means the pot is still speeding up, which is the
    cold sheath. Returns None until there is enough of a curve to fit.
    """

    if len(samples) < 3:
        return None
    origin_t, origin_v = samples[0]
    numerator = 0.0
    denominator = 0.0
    for stamp, reading in samples[1:]:
        elapsed = stamp - origin_t
        if elapsed <= 0:
            continue
        numerator += (reading - origin_v) * elapsed * elapsed
        denominator += elapsed ** 4
    if denominator <= 0:
        return None
    quad = numerator / denominator
    if not math.isfinite(quad) or quad <= 0:
        return None
    return quad


def fit_coast_parabola(
    samples: list[tuple[float, float]],
) -> tuple[float, float] | None:
    """Least-squares ``v`` and ``c`` in ``ΔT = v t + c t²``.

    ``c`` is clamped at or below zero so the curve can crest. Returns None
    when the window is too short to separate the two terms.
    """

    if len(samples) < 4:
        return None
    origin_t, origin_v = samples[0]
    sum_t2 = sum_t3 = sum_t4 = 0.0
    sum_ty = sum_t2y = 0.0
    used = 0
    for stamp, reading in samples[1:]:
        elapsed = stamp - origin_t
        if elapsed <= 0:
            continue
        dy = reading - origin_v
        t2 = elapsed * elapsed
        t3 = t2 * elapsed
        sum_t2 += t2
        sum_t3 += t3
        sum_t4 += t2 * t2
        sum_ty += elapsed * dy
        sum_t2y += t2 * dy
        used += 1
    if used < 3:
        return None
    det = sum_t2 * sum_t4 - sum_t3 * sum_t3
    if abs(det) < 1e-9:
        return None
    velocity = (sum_t4 * sum_ty - sum_t3 * sum_t2y) / det
    quad = (sum_t2 * sum_t2y - sum_t3 * sum_ty) / det
    if not math.isfinite(velocity) or not math.isfinite(quad):
        return None
    if quad > 0:
        quad = 0.0
    return velocity, quad


def coast_curve(model: ThermalModel) -> tuple[float, float, float]:
    """``(initial °F/s, °F/s², seconds to the crest)`` for a full coast.

    The crest and the time to reach it fix the parabola: the slope starts at
    ``2 rise / duration`` and falls through zero at ``duration``.
    """

    rise = model.coast_rise_f or 0.0
    duration = model.coast_seconds or 0.0
    if rise <= 0 or duration <= 0:
        return 0.0, 0.0, 0.0
    velocity = 2.0 * rise / duration
    quad = model.coast_quad
    if quad is None or quad >= 0:
        quad = -rise / (duration * duration)
    return velocity, quad, duration


def startup_quad(model: ThermalModel) -> float:
    """°F/s² of the cold-start parabola, or the value that meets the linear rate."""

    if model.cold_start_quad is not None and model.cold_start_quad > 0:
        return model.cold_start_quad
    cold = model.cold_start_seconds or 0.0
    linear = model.linear_rate_f_per_sec or 0.0
    if cold > 0 and linear > 0:
        return linear / (2.0 * cold)
    return 0.0


def rate_while_heating(model: ThermalModel, on_seconds: float) -> float:
    """°F/s at ``on_seconds`` into the current on-interval.

    With a soak time constant the slope is ``R (1 - e^{-t/τ})``. On an older
    parabola the slope is ``2 a t`` until the rise is straight.
    """

    elapsed = max(0.0, on_seconds)
    tau = model.soak_tau_seconds or 0.0
    linear = model.linear_rate_f_per_sec or 0.0
    if tau > 0 and linear > 0:
        return max(0.0, linear * (1.0 - math.exp(-elapsed / tau)))
    cold = model.cold_start_seconds or 0.0
    quad = startup_quad(model)
    if cold > 0 and elapsed < cold and quad > 0:
        return max(0.0, 2.0 * quad * elapsed)
    if model.linear_rate_f_per_sec is not None and model.linear_rate_f_per_sec > 0:
        return model.linear_rate_f_per_sec
    if quad > 0:
        return max(0.0, 2.0 * quad * elapsed)
    return 0.0


def _startup_rise(model: ThermalModel, on_seconds: float) -> float:
    """Degrees gained since the SSR closed, along the parabola then the line."""

    elapsed = max(0.0, on_seconds)
    if elapsed == 0:
        return 0.0
    cold = model.cold_start_seconds or 0.0
    quad = startup_quad(model)
    linear = model.linear_rate_f_per_sec or 0.0
    if cold <= 0 or quad <= 0:
        return max(linear, 0.0) * elapsed
    if elapsed <= cold:
        return quad * elapsed * elapsed
    return quad * cold * cold + max(linear, 0.0) * (elapsed - cold)


def temperature_after_heating(
    temp_f: float,
    on_seconds: float,
    extra_on_seconds: float,
    model: ThermalModel,
) -> float:
    """Probe temperature after heating ``extra_on_seconds`` longer."""

    start = max(0.0, on_seconds)
    end = start + max(0.0, extra_on_seconds)
    return float(temp_f) + _startup_rise(model, end) - _startup_rise(model, start)


def _characterized_on_seconds(model: ThermalModel) -> float:
    return (model.cold_start_seconds or 0.0) + LINEAR_HOLD_SECONDS


def _coast_fraction(model: ThermalModel, on_seconds: float) -> float:
    """How much of the measured coast this on-time has stored, from 0 to 1.

    The fraction is the shutoff slope divided by the slope of a full
    characterization. Coast rise then scales with the square, because the
    downward parabola has the same curvature and a shallower starting slope.
    """

    if on_seconds <= 0:
        return 0.0
    full_on = _characterized_on_seconds(model)
    full_slope = rate_while_heating(model, full_on if full_on > 0 else on_seconds)
    if full_slope <= 0:
        return 1.0 if on_seconds > 0 else 0.0
    slope = rate_while_heating(model, on_seconds)
    if slope <= 0:
        return 0.0
    return min(1.0, slope / full_slope)


# A live rise steeper than the Stage I rate is still storing heat. Cap the
# scale so one noisy sample cannot demand an enormous crest.
COAST_SLOPE_SCALE_MAX = 3.0


def coast_entry_slope(
    model: ThermalModel,
    on_seconds: float,
    slope_f_per_sec: float,
) -> float:
    """Slope a shutoff now carries onto the coast exponential.

    The soak curve for this on-time is the heating slope. A few seconds on
    sits low on that curve, and thirty seconds on sits higher, so the coast
    levels at a different ``S0 · τ_c``. A steeper probe can raise the entry.
    A probe or a sheath reading below the soak curve cannot lower it.
    """

    observed = max(0.0, float(slope_f_per_sec))
    if on_seconds <= 0:
        return observed
    return max(observed, rate_while_heating(model, on_seconds))


def coast_temperature_scale(temp_f: float | None, measured_at_f: float | None) -> float:
    """How much further the pot runs when it is warmer than the measured coast.

    ``measured_at_f`` is the shutoff temperature the rise was learned at.
    Degrees above freezing are the same ratio on the Celsius scale. A shutoff
    at that same temperature leaves the coast unchanged.
    """

    if temp_f is None or measured_at_f is None:
        return 1.0
    span = measured_at_f - 32.0
    if span <= 1.0:
        return 1.0
    return max(0.0, float(temp_f) - 32.0) / span


def remaining_coast_f(
    model: ThermalModel,
    slope_f_per_sec: float,
    temp_f: float | None = None,
) -> float:
    """Degrees the pot still climbs if the heaters open at this slope.

    The coast is ``slope · τ``. At the straight rate that is the measured
    rise. A shallower slope has stored less, in proportion to the slope. A
    steeper slope is still storing heat, so the rise grows with the slope.
    A warmer pot than the one that coast was measured on runs further, in
    proportion to its temperature above freezing.
    """

    if slope_f_per_sec <= 0:
        return 0.0
    tau = coast_tau_of(model)
    if tau <= 0:
        return 0.0
    linear = model.linear_rate_f_per_sec or 0.0
    rise = model.coast_rise_f or 0.0
    if linear > 0 and slope_f_per_sec > linear and rise > 0:
        scale = min(COAST_SLOPE_SCALE_MAX, slope_f_per_sec / linear)
        base = rise * scale
    else:
        base = slope_f_per_sec * tau
    return base * coast_temperature_scale(temp_f, model.coast_at_f)


def coast_rise_for(
    model: ThermalModel,
    on_seconds: float,
    temp_f: float | None = None,
) -> float:
    """Remaining crest if the shutoff slope is the slope at ``on_seconds``."""

    if on_seconds <= 0:
        return 0.0
    return remaining_coast_f(model, rate_while_heating(model, on_seconds), temp_f)


def step_sheath_slope(
    model: ThermalModel,
    slope_f_per_sec: float,
    *,
    heating: bool,
    dt: float,
) -> float:
    """Move the sheath slope for ``dt`` seconds of heating or cooling.

    Heating accelerates toward the linear rate and does not start over at
    zero when the SSR was already warm. Cooling spends that slope on the
    clock of the measured crest, so it is gone when the pot has crested.
    """

    slope = max(0.0, slope_f_per_sec)
    if dt <= 0:
        return slope
    linear = model.linear_rate_f_per_sec or 0.0
    if heating:
        tau = model.soak_tau_seconds or 0.0
        if tau > 0 and linear > 0:
            return max(0.0, linear - (linear - slope) * math.exp(-dt / tau))
        if linear <= 0:
            return slope
        accel = 2.0 * startup_quad(model)
        if (model.cold_start_seconds or 0.0) <= 0 or accel <= 0:
            return linear
        return min(linear, slope + accel * dt)
    tau = coast_tau_of(model)
    if tau > 0:
        return slope * math.exp(-dt / tau)
    return slope


def budgeted_coast_f(
    model: ThermalModel,
    slope_f_per_sec: float,
    temp_f: float,
    goal_f: float,
) -> float:
    """Crest to plan for if the heater stays on.

    The slope right now may still be low. If the element would reach linear
    heat before the probe got to ``goal_f``, the landing is the full crest,
    so the shutoff has to use that crest now instead of running up to the goal.
    """

    rise = (model.coast_rise_f or 0.0) * coast_temperature_scale(temp_f, model.coast_at_f)
    if rise <= 0:
        return 0.0
    now = remaining_coast_f(model, slope_f_per_sec, temp_f)
    linear = model.linear_rate_f_per_sec or 0.0
    if linear <= 0:
        return now
    if slope_f_per_sec >= linear * 0.98:
        return remaining_coast_f(model, slope_f_per_sec, temp_f)
    tau = model.soak_tau_seconds or 0.0
    accel = 2.0 * startup_quad(model)
    if tau > 0 and accel <= 0:
        gap = max(0.0, linear - max(0.0, slope_f_per_sec))
        if gap <= 0.02 * linear:
            seconds_to_linear = 0.0
        else:
            seconds_to_linear = tau * math.log(gap / (0.02 * linear))
        climb = (max(0.0, slope_f_per_sec) + linear) * 0.5 * seconds_to_linear
        if temp_f + climb + rise >= goal_f:
            return rise
        return now
    if accel <= 0:
        return now
    seconds_to_linear = (linear - max(0.0, slope_f_per_sec)) / accel
    climb = (max(0.0, slope_f_per_sec) + linear) * 0.5 * seconds_to_linear
    if temp_f + climb + rise >= goal_f:
        return rise
    return now


def predicted_crest(
    temp_f: float,
    on_seconds: float,
    extra_on_seconds: float,
    model: ThermalModel,
) -> float:
    """Where the probe crests if the SSR opens after ``extra_on_seconds`` more.

    A pulse shorter than the slope window adds a small margin so the shutoff
    errs under the aim.
    """

    future_on = max(0.0, on_seconds) + max(0.0, extra_on_seconds)
    at_open = temperature_after_heating(temp_f, on_seconds, extra_on_seconds, model)
    crest = at_open + coast_rise_for(model, future_on, at_open)
    if future_on < PREDICT_MIN_SPAN_SECONDS:
        crest += EARLY_UNCERTAINTY_F
    return crest


# The live chart draws this much of the shutoff guess past the current time.
PROJECTION_SECONDS = 60.0
PROJECTION_STEP_SECONDS = 5.0


def project_temperature(
    temp_f: float,
    slope_f_per_sec: float,
    model: ThermalModel | None,
    cooldown_f_per_min: float | None = None,
    *,
    seconds: float = PROJECTION_SECONDS,
    step: float = PROJECTION_STEP_SECONDS,
    remaining_f: float | None = None,
) -> list[tuple[float, float]]:
    """Temperature for the next ``seconds`` if the heaters stay as they are.

    While the elements are on, a rising slope is the shutoff being weighed:
    the line climbs toward ``temp + slope · τ``. While they are already off,
    ``remaining_f`` is the unspent part of that same coast, so the line
    flattens at the crest chosen when the heaters opened. A falling slope
    keeps falling.
    """

    horizon = max(0.0, seconds)
    pace = step if step > 0 else horizon or 1.0
    slope = slope_f_per_sec
    cool = max(0.0, cooldown_f_per_min or 0.0) / 60.0
    if remaining_f is not None:
        rise = max(0.0, remaining_f)
        tau = coast_tau_of(model) if model is not None else 0.0
        if tau <= 0 and slope > 0 and rise > 0:
            tau = rise / slope
    else:
        rise = (
            remaining_coast_f(model, slope, temp_f)
            if model is not None and slope > 0
            else 0.0
        )
        tau = (rise / slope) if slope > 0 and rise > 0 else 0.0
    # Follow the exponential until the tail is within a tenth of a degree.
    # Cutting the line off at three time constants drops the last half degree.
    settled = coast_settle_seconds(tau, rise) if tau > 0 and rise > 0 else 0.0
    points: list[tuple[float, float]] = []
    stamp = 0.0
    while True:
        if remaining_f is not None and rise <= 0:
            reading = temp_f - cool * stamp
        elif rise > 0 and tau > 0:
            if stamp <= settled:
                reading = temp_f + rise * (1.0 - math.exp(-stamp / tau))
            else:
                arrived = temp_f + rise * (1.0 - math.exp(-settled / tau))
                reading = arrived - cool * (stamp - settled)
        else:
            rate = slope if slope < 0 else -cool
            reading = temp_f + rate * stamp
        points.append((round(stamp, 1), reading))
        if stamp >= horizon:
            break
        stamp = min(horizon, stamp + pace)
    return points


def _pulse_temperature_gain(model: ThermalModel, slope_f_per_sec: float, seconds: float) -> float:
    """Degrees the probe climbs while a pulse accelerates the sheath slope."""

    if seconds <= 0:
        return 0.0
    end = step_sheath_slope(model, slope_f_per_sec, heating=True, dt=seconds)
    return max(0.0, (max(0.0, slope_f_per_sec) + end) * 0.5 * seconds)


def pulse_plan(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    min_on_seconds: float,
    slope_f_per_sec: float | None = None,
    *,
    budgeted: bool,
) -> dict:
    """Crest comparison for one pulse decision, and the numbers behind it.

    While the element is already on, the coast is entered at the heating
    slope for this on-time, raised when the probe is steeper. ``budgeted``
    plans the crest that slope would store if the sheath reached linear heat
    before the aim. The calibration pulse uses the coast already stored at
    that same entry. A new pulse, with the element off, is the climb during
    the minimum on time plus the coast that on time would store.
    """

    if slope_f_per_sec is None:
        slope_f_per_sec = rate_while_heating(model, on_seconds) if on_seconds > 0 else 0.0
    slope = max(0.0, float(slope_f_per_sec))
    on = max(0.0, float(on_seconds))
    if on > 0:
        # Enter the coast where this on-time sits on the heating curve.
        slope = coast_entry_slope(model, on, slope)
        rise = (
            budgeted_coast_f(model, slope, temp_f, target_f)
            if budgeted
            else remaining_coast_f(model, slope, temp_f)
        )
        gain = 0.0
        crest = temp_f + rise
        heat = crest < target_f
    else:
        minimum = max(0.0, float(min_on_seconds))
        slope_after = step_sheath_slope(model, slope, heating=True, dt=minimum)
        gain = _pulse_temperature_gain(model, slope, minimum)
        rise = remaining_coast_f(model, slope_after, temp_f)
        crest = temp_f + gain + rise
        heat = temp_f < target_f and crest <= target_f
    return {
        "heat": heat,
        "slope_f_per_sec": round(slope, 5),
        "predicted_crest_f": round(crest, 2),
        "remaining_coast_f": round(rise, 2),
        "gain_f": round(gain, 2),
        "on_seconds": round(on, 2),
    }


def coast_sized_should_heat(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    min_on_seconds: float,
    slope_f_per_sec: float | None = None,
) -> bool:
    """True while a proving pulse should keep storing heat.

    The pulse stays on until the coast from the slope stored so far reaches
    the target. The heaters then open and that coast plays out. A wider gap
    needs a steeper slope, so the pulse runs longer. Closer to the target the
    same test ends the pulse sooner. A new pulse is refused when the minimum
    on time would already carry the crest past the target.
    """

    return pulse_plan(
        temp_f,
        target_f,
        on_seconds,
        model,
        min_on_seconds,
        slope_f_per_sec,
        budgeted=False,
    )["heat"]


def _hold_can_track_a_short_pulse(model: ThermalModel) -> bool:
    """True when the sheath model knows a 1 second pulse is not full heat."""

    return (model.soak_tau_seconds or 0.0) > 0 or (model.cold_start_seconds or 0.0) > 0


def hold_pulse_landing(
    temp_f: float,
    model: ThermalModel,
    min_on_seconds: float,
    sheath_slope: float = 0.0,
) -> tuple[float, float, float, float]:
    """Landing, on-pulse gain, coast after shutoff, and the slope at shutoff.

    With no coast on the model, the pulse is the straight rate times the
    minimum. Once a coast has been learned, the landing is the climb while
    the element is on plus the coast that slope still delivers after the SSR
    opens. Slope already stored in the sheath is part of that coast, so the
    next pulse is not planned as if the element were cold.
    """

    rate = max(0.0, float(model.linear_rate_f_per_sec or 0.0))
    minimum = max(0.0, float(min_on_seconds))
    slope0 = max(0.0, float(sheath_slope))
    if coast_tau_of(model) <= 0 or not _hold_can_track_a_short_pulse(model):
        gain = rate * minimum
        return temp_f + gain, gain, 0.0, rate
    if minimum <= 0:
        coast = remaining_coast_f(model, slope0, temp_f) if slope0 > 0 else 0.0
        return temp_f + coast, 0.0, coast, slope0
    slope1 = step_sheath_slope(model, slope0, heating=True, dt=minimum)
    average = (slope0 + max(0.0, slope1)) * 0.5
    if rate > 0:
        average = min(average, rate)
    gain = max(0.0, average * minimum)
    coast = remaining_coast_f(model, slope1, temp_f + gain)
    return temp_f + gain + coast, gain, coast, slope1


def hold_on_time(
    temp_f: float,
    target_f: float,
    model: ThermalModel,
    sheath_slope: float,
    minimum: float,
    *,
    limit: float = 10 * 60.0,
) -> float:
    """Shortest on-time at or above ``minimum`` whose crest reaches ``target_f``.

    ``minimum`` is returned when that pulse already reaches or passes the
    target, so the caller can refuse one that would go by. The search stops
    at ``limit`` when even that long a pulse stays short.
    """

    floor = max(0.0, float(minimum))
    cap = max(floor, float(limit))
    landing, _, _, _ = hold_pulse_landing(temp_f, model, floor, sheath_slope)
    if landing >= target_f or floor >= cap:
        return floor
    hi_landing, _, _, _ = hold_pulse_landing(temp_f, model, cap, sheath_slope)
    if hi_landing < target_f:
        return cap
    lo = floor
    hi = cap
    for _ in range(24):
        mid = (lo + hi) / 2.0
        crest, _, _, _ = hold_pulse_landing(temp_f, model, mid, sheath_slope)
        if crest >= target_f:
            hi = mid
        else:
            lo = mid
    return hi


def hold_maintenance_plan(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    min_on_seconds: float,
    *,
    sheath_slope: float = 0.0,
    off_seconds: float = 0.0,
    min_off_seconds: float = 0.0,
    probe_slope: float | None = None,
) -> dict:
    """Landing of the next pulse, and whether to fire it.

    A new pulse fires only when its crest lands on the target or short of it.
    One that would pass the target waits until the pot has cooled enough for
    that same minimum to land on it. A pulse that lands short stays on past
    the minimum until opening would carry the crest past the target, then
    opens. The hardware minimum still finishes once a pulse has started.
    """

    rate = max(0.0, float(model.linear_rate_f_per_sec or 0.0))
    minimum = max(0.0, float(min_on_seconds))
    on = max(0.0, float(on_seconds))
    off = float(off_seconds)
    if not math.isfinite(off):
        off = math.inf
    else:
        off = max(0.0, off)
    minimum_off = max(0.0, float(min_off_seconds))
    landing, gain, coast, shutoff = hold_pulse_landing(temp_f, model, minimum, sheath_slope)
    hold_wait = None
    pulse = minimum
    if on > 0:
        if minimum > 0 and on < minimum:
            heat = True
            extra = hold_on_time(
                temp_f, target_f, model, sheath_slope, max(0.0, minimum - on)
            )
            landing, gain, coast, shutoff = hold_pulse_landing(
                temp_f, model, extra, sheath_slope
            )
            pulse = on + extra
        else:
            now, _, _, _ = hold_pulse_landing(temp_f, model, 0.0, sheath_slope)
            nxt, _, _, _ = hold_pulse_landing(temp_f, model, 1.0, sheath_slope)
            # Open before the second that would carry the crest past the target.
            heat = now < target_f and nxt > now and nxt <= target_f
            extra = 0.0 if not heat else hold_on_time(
                temp_f, target_f, model, sheath_slope, 0.0
            )
            landing, gain, coast, shutoff = hold_pulse_landing(
                temp_f, model, extra, sheath_slope
            )
            pulse = on + extra
    elif temp_f >= target_f:
        heat = False
        hold_wait = "at_target"
    elif minimum_off > 0 and off < minimum_off:
        heat = False
        hold_wait = "min_off"
    elif probe_slope is not None and probe_slope > HOLD_RISE_F_PER_SEC:
        heat = False
        hold_wait = "rising"
    elif landing > target_f:
        heat = False
        hold_wait = "overshoot"
    elif landing <= temp_f + 1e-4:
        heat = False
        hold_wait = "short"
    else:
        heat = True
        pulse = hold_on_time(temp_f, target_f, model, sheath_slope, minimum)
        landing, gain, coast, shutoff = hold_pulse_landing(
            temp_f, model, pulse, sheath_slope
        )
    plan = {
        "heat": heat,
        "slope_f_per_sec": round(shutoff, 5),
        "linear_rate_f_per_sec": round(rate, 5),
        "predicted_crest_f": round(landing, 2),
        "gain_f": round(gain, 2),
        "remaining_coast_f": round(coast, 2),
        "pulse_seconds": round(pulse, 2),
        "on_seconds": round(on, 2),
        "sheath_slope_f_per_sec": round(max(0.0, float(sheath_slope)), 5),
    }
    if math.isfinite(off):
        plan["off_seconds"] = round(off, 2)
    if probe_slope is not None and math.isfinite(float(probe_slope)):
        plan["live_slope_f_per_sec"] = round(float(probe_slope), 5)
    if hold_wait and not heat:
        plan["hold_wait"] = hold_wait
    return plan


def hold_should_heat(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    min_on_seconds: float,
    *,
    sheath_slope: float = 0.0,
    off_seconds: float = 0.0,
    min_off_seconds: float = 0.0,
    probe_slope: float | None = None,
) -> bool:
    """True when a hold pulse should be on.

    A new pulse fires when its crest lands on the target without passing it.
    A pulse that has already started finishes the hardware minimum, then
    stays on only while another second still crests at or under the target.
    """

    return hold_maintenance_plan(
        temp_f,
        target_f,
        on_seconds,
        model,
        min_on_seconds,
        sheath_slope=sheath_slope,
        off_seconds=off_seconds,
        min_off_seconds=min_off_seconds,
        probe_slope=probe_slope,
    )["heat"]


def _in_linear_heat(model: ThermalModel, on_seconds: float) -> bool:
    """True once this on-interval has left the cold-start parabola.

    From here the coast rise does not grow. More time on the straight line
    stores the same crest.
    """

    if (model.linear_rate_f_per_sec or 0.0) <= 0:
        return False
    return on_seconds >= (model.cold_start_seconds or 0.0)


def seconds_to_landing(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    *,
    heating: bool,
    horizon: float,
    off_seconds: float = 0.0,
    slope_f_per_sec: float | None = None,
) -> float | None:
    """Seconds until the probe reaches ``target_f``.

    In linear heat the crest is the measured coast, added to the current
    temperature. The straight rate covers the degrees before that crest, and
    the coast parabola covers the crest itself. Before the rise is straight,
    the crest still depends on how long the sheath has been on.
    """

    if temp_f >= target_f:
        return 0.0
    if not model.ready():
        return None
    if slope_f_per_sec is not None:
        rise = budgeted_coast_f(model, slope_f_per_sec, temp_f, target_f)
        full = (model.coast_rise_f or 0.0) * coast_temperature_scale(temp_f, model.coast_at_f)
        if full > 0 and rise >= full * 0.98 and (model.linear_rate_f_per_sec or 0.0) > 0:
            return _seconds_from_fixed_crest(
                temp_f,
                target_f,
                model,
                heating=heating,
                off_seconds=off_seconds,
                horizon=horizon,
            )
    if _in_linear_heat(model, on_seconds):
        return _seconds_from_fixed_crest(
            temp_f,
            target_f,
            model,
            heating=heating,
            off_seconds=off_seconds,
            horizon=horizon,
        )
    if not heating:
        return _seconds_on_coast(temp_f, target_f, on_seconds, off_seconds, model, horizon)
    if predicted_crest(temp_f, on_seconds, horizon, model) < target_f:
        return None
    lo = 0.0
    hi = horizon
    for _ in range(18):
        mid = (lo + hi) / 2.0
        if predicted_crest(temp_f, on_seconds, mid, model) >= target_f:
            hi = mid
        else:
            lo = mid
    # The crest is on the coast parabola after the SSR opens, not at shutoff.
    opened_for = on_seconds + hi
    tail = _coast_duration(model, opened_for)
    return min(horizon, max(0.0, hi + tail))


def _exponential_coast_time(need_f: float, remaining_f: float, tau_seconds: float) -> float | None:
    """Seconds for ``remaining · (1 - e^{-t/τ})`` to gain ``need_f`` degrees."""

    if need_f <= 0:
        return 0.0
    if remaining_f <= 0 or tau_seconds <= 0 or need_f >= remaining_f:
        return None
    return -tau_seconds * math.log(1.0 - need_f / remaining_f)


def _seconds_from_fixed_crest(
    temp_f: float,
    target_f: float,
    model: ThermalModel,
    *,
    heating: bool,
    off_seconds: float,
    horizon: float,
) -> float | None:
    """Project the leveling coast forward from ``temp_f``."""

    crest = (model.coast_rise_f or 0.0) * coast_temperature_scale(temp_f, model.coast_at_f)
    rate = model.linear_rate_f_per_sec or 0.0
    tau = coast_tau_of(model)
    need = target_f - temp_f
    if need <= 0:
        return 0.0
    if heating:
        if rate <= 0:
            return None
        before = need - crest
        if before > 0:
            if before / rate > horizon:
                return None
            # The shutoff equilibrium is the target, so the probe approaches it.
            # Count the time to come within ARRIVAL_F of that crest.
            tail = _exponential_coast_time(max(0.0, crest - ARRIVAL_F), crest, tau) or 0.0
            return min(horizon, before / rate + tail)
        return _exponential_coast_time(need, crest, tau)
    remaining = crest
    if tau > 0 and off_seconds > 0 and crest > 0:
        gained = crest * (1.0 - math.exp(-min(off_seconds, tau * 8.0) / tau))
        remaining = max(0.0, crest - gained)
    return _exponential_coast_time(need, remaining, tau)


def _coast_duration(model: ThermalModel, on_seconds: float) -> float:
    """Seconds from shutoff until this pulse's coast parabola crests."""

    duration = model.coast_seconds or 0.0
    if duration <= 0:
        return 0.0
    return duration * _coast_fraction(model, on_seconds)


def _seconds_on_coast(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    off_seconds: float,
    model: ThermalModel,
    horizon: float,
) -> float | None:
    """Time along the downward parabola from the current point to ``target_f``."""

    need = target_f - temp_f
    if need <= 0:
        return 0.0
    full_velocity, quad, duration = coast_curve(model)
    if quad >= 0 or full_velocity <= 0:
        return None
    velocity = min(rate_while_heating(model, max(on_seconds, 1.0)), full_velocity)
    if velocity <= 0:
        return None
    crest_at = min(duration, max(0.0, -velocity / (2.0 * quad)))
    off = min(max(0.0, off_seconds), crest_at)
    velocity_now = velocity + 2.0 * quad * off
    if velocity_now <= PREDICT_MIN_RATE_F_PER_SEC:
        return None
    disc = velocity_now * velocity_now + 4.0 * quad * need
    if disc < 0:
        return None
    root = (-velocity_now + math.sqrt(disc)) / (2.0 * quad)
    if root < 0:
        root = (-velocity_now - math.sqrt(disc)) / (2.0 * quad)
    if root < 0:
        return None
    return min(horizon, max(0.0, root))


def nudge_model(model: ThermalModel, miss_f: float) -> bool:
    """Move the rise after a crest misses its check temperature.

    ``miss_f`` is the crest minus the target. Positive means the coast landed
    high, so the stored rise grows and the next pulse opens sooner.
    """

    return tune_model(model, crest_delta_f=miss_f)


def accept_grok(local: float | None, remote: float | None, *, band: float) -> bool:
    """True when Grok's number is close enough to the local fit to use."""

    if local is None or remote is None:
        return False
    if not math.isfinite(local) or not math.isfinite(remote):
        return False
    if remote < 0 and local >= 0:
        return False
    return abs(remote - local) <= max(band, GROK_BAND_FRACTION * abs(local))


def apply_grok_value(model: ThermalModel, field_name: str, remote: float) -> None:
    """Replace one fitted number with Grok's, and keep the parabola consistent."""

    if field_name == "cold_start":
        duration = model.cold_start_seconds or 0.0
        if duration > 0:
            # Average slope of ΔT = a t² over the cold start is a * duration.
            model.cold_start_quad = round(max(0.0, remote / duration), 6)
        return
    if field_name == "linear_rate_f_per_sec":
        model.linear_rate_f_per_sec = round(max(0.0, remote), 5)
        return
    if field_name == "coast_rise_f":
        model.coast_rise_f = round(max(0.0, remote), 2)
        if model.coast_seconds and model.coast_seconds > 0:
            model.coast_quad = round(-model.coast_rise_f / (model.coast_seconds ** 2), 6)
        return
    if field_name == "cooldown_f_per_min":
        model.cooldown_f_per_min = round(max(0.0, remote), 2)
