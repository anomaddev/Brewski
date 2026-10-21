"""Heating model and look-ahead used by calibration and later runs.

Cold start and coasting are parabolas. With the sheath still cold, temperature
climbs as ``a t²``: the slope starts near zero and steepens until the rise
goes straight. After the SSR opens, temperature climbs as ``v t + c t²`` with
``c`` negative, so the slope falls through zero at the crest. The straight
line between those two curves is the linear heating rate. Stage II and a
normal run play that shape forward and open the SSR when the predicted crest
reaches the aim.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Calibration always aims here. The dial setpoint belongs to the next real run.
CALIBRATION_TEMP_F = 150.0
# A cooldown is over, and the pot is safe to handle, under this temperature.
SAFE_TEMP_F = 90.0
SETTLE_SECONDS = 3.0
LINEAR_HOLD_SECONDS = 15.0
MOMENTUM_SECONDS = 2 * 60.0
HOLD_SECONDS = 10 * 60.0
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
# °F/s² over the recent slope window. Above this, the rise is still steepening.
LINEAR_SETTLED_ACCEL = 0.0002
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
    """Parabolas and the straight rate learned across calibrations."""

    cold_start_seconds: float | None = None
    # °F/s². While the sheath is soaking, ΔT = cold_start_quad * t².
    cold_start_quad: float | None = None
    linear_rate_f_per_sec: float | None = None
    coast_rise_f: float | None = None
    coast_seconds: float | None = None
    # °F/s², negative. After the SSR opens, ΔT = v t + coast_quad * t²
    # and the slope is zero at coast_seconds. v is the heating rate at shutoff.
    coast_quad: float | None = None
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
            "linear_rate_f_per_sec": self.linear_rate_f_per_sec,
            "coast_rise_f": self.coast_rise_f,
            "coast_seconds": self.coast_seconds,
            "coast_quad": self.coast_quad,
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

    return ThermalModel(
        cold_start_seconds=optional("cold_start_seconds"),
        cold_start_quad=optional("cold_start_quad"),
        linear_rate_f_per_sec=optional("linear_rate_f_per_sec"),
        coast_rise_f=optional("coast_rise_f"),
        coast_seconds=optional("coast_seconds"),
        coast_quad=optional("coast_quad"),
        cooldown_f_per_min=optional("cooldown_f_per_min"),
        calibration_count=calibration_count,
    )


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
    return ThermalModel(
        cold_start_seconds=model.cold_start_seconds,
        cold_start_quad=scaled(model.cold_start_quad, 6),
        linear_rate_f_per_sec=scaled(model.linear_rate_f_per_sec),
        coast_rise_f=rise,
        coast_seconds=model.coast_seconds,
        coast_quad=quad,
        cooldown_f_per_min=cool,
        calibration_count=model.calibration_count,
    )


_BLEND_FIELDS = (
    ("cold_start_seconds", 2),
    ("cold_start_quad", 6),
    ("linear_rate_f_per_sec", 5),
    ("coast_rise_f", 2),
    ("coast_seconds", 2),
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
    """End-point slope at each sample that closes a full window."""

    slopes: list[tuple[float, float]] = []
    for stamp, _value in samples:
        window_points = [(t, reading) for t, reading in samples if stamp - window <= t <= stamp]
        if len(window_points) < PREDICT_MIN_SAMPLES:
            continue
        if window_points[-1][0] - window_points[0][0] < window * 0.8:
            continue
        rate = endpoint_rate(window_points)
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
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < LINEAR_RISE_SECONDS - 1:
        return False
    if any(rate < PREDICT_MIN_RATE_F_PER_SEC for _stamp, rate in recent):
        return False
    span = recent[-1][0] - recent[0][0]
    if span <= 0:
        return False
    if (recent[-1][1] - recent[0][1]) / span > LINEAR_SETTLED_ACCEL:
        return False
    peak = max(rate for _stamp, rate in slopes)
    return recent[-1][1] >= peak - LINEAR_SETTLED_BAND


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


def heating_lag_coast(rate_f_per_sec: float, tau_seconds: float, heated_seconds: float) -> float:
    """Degrees still to come if the heaters open after ``heated_seconds``.

    The element is holding ``rate * τ * (1 - e^{-t/τ})`` degrees of climb.
    At full soak that is ``rate * τ``.
    """

    if rate_f_per_sec <= 0 or tau_seconds <= 0 or heated_seconds <= 0:
        return 0.0
    soaked = 1.0 - math.exp(-heated_seconds / tau_seconds)
    return rate_f_per_sec * tau_seconds * soaked


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

    On the startup parabola the slope is ``2 a t``. Past the cold start it is
    the straight-line rate.
    """

    cold = model.cold_start_seconds or 0.0
    quad = startup_quad(model)
    elapsed = max(0.0, on_seconds)
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


def remaining_coast_f(model: ThermalModel, slope_f_per_sec: float) -> float:
    """Degrees the pot still climbs if the heaters open at this slope.

    At the slope Stage I measured, the crest is the measured rise. A shallower
    slope is further along that same curve. A steeper slope has not finished
    storing heat, so the crest grows with the slope instead of staying capped.
    """

    rise = model.coast_rise_f or 0.0
    if rise <= 0 or slope_f_per_sec <= 0:
        return 0.0
    linear = model.linear_rate_f_per_sec or 0.0
    velocity, _quad, _duration = coast_curve(model)
    start = linear if linear > 0 else velocity
    if start <= 0:
        return rise
    if slope_f_per_sec > start:
        scale = min(COAST_SLOPE_SCALE_MAX, slope_f_per_sec / start)
        return rise * scale
    fraction = slope_f_per_sec / start
    return rise * fraction * fraction


def coast_rise_for(model: ThermalModel, on_seconds: float) -> float:
    """Remaining crest if the shutoff slope is the slope at ``on_seconds``."""

    if on_seconds <= 0:
        return 0.0
    return remaining_coast_f(model, rate_while_heating(model, on_seconds))


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
        if linear <= 0:
            return slope
        accel = 2.0 * startup_quad(model)
        if (model.cold_start_seconds or 0.0) <= 0 or accel <= 0:
            return linear
        return min(linear, slope + accel * dt)
    duration = model.coast_seconds or 0.0
    if duration > 0 and linear > 0:
        # A full-rate shutoff has delivered its coast when the measured crest
        # arrives. The coast parabola's own starting slope is shallower than
        # the linear rate, and decaying on that shallower slope leaves a
        # phantom coast after the pot has already stopped rising.
        return max(0.0, slope - (linear / duration) * dt)
    _velocity, quad, curve_duration = coast_curve(model)
    if quad >= 0 or curve_duration <= 0:
        return slope
    return max(0.0, slope + 2.0 * quad * dt)


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

    rise = model.coast_rise_f or 0.0
    if rise <= 0:
        return 0.0
    now = remaining_coast_f(model, slope_f_per_sec)
    linear = model.linear_rate_f_per_sec or 0.0
    if linear <= 0:
        return now
    if slope_f_per_sec >= linear * 0.98:
        return remaining_coast_f(model, slope_f_per_sec)
    accel = 2.0 * startup_quad(model)
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
    crest = at_open + coast_rise_for(model, future_on)
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
) -> list[tuple[float, float]]:
    """Temperature for the next ``seconds`` if the heaters are open from now.

    A rising slope is the entry onto the coast parabola: the line climbs
    ``remaining_coast_f`` and then follows the cooling rate. A falling slope
    keeps falling. This is the shutoff the control loop is weighing, not a
    projection that keeps the elements on.
    """

    horizon = max(0.0, seconds)
    pace = step if step > 0 else horizon or 1.0
    slope = slope_f_per_sec
    rise = remaining_coast_f(model, slope) if model is not None and slope > 0 else 0.0
    cool = max(0.0, cooldown_f_per_min or 0.0) / 60.0
    duration = (2.0 * rise / slope) if slope > 0 and rise > 0 else 0.0
    quad = (-rise / (duration * duration)) if duration > 0 else 0.0
    points: list[tuple[float, float]] = []
    stamp = 0.0
    while True:
        if slope > 0 and duration > 0:
            if stamp <= duration:
                reading = temp_f + slope * stamp + quad * stamp * stamp
            else:
                reading = temp_f + rise - cool * (stamp - duration)
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

    While the element is already on, ``budgeted`` plans the crest it would
    store if the sheath reached linear heat before the aim. Stage III uses
    the coast already stored at the current slope instead. A new pulse, with
    the element off, is the climb during the minimum on time plus the coast
    that on time would store.
    """

    if slope_f_per_sec is None:
        slope_f_per_sec = rate_while_heating(model, on_seconds) if on_seconds > 0 else 0.0
    slope = max(0.0, float(slope_f_per_sec))
    on = max(0.0, float(on_seconds))
    if on > 0:
        rise = (
            budgeted_coast_f(model, slope, temp_f, target_f)
            if budgeted
            else remaining_coast_f(model, slope)
        )
        gain = 0.0
        crest = temp_f + rise
        heat = crest < target_f
    else:
        minimum = max(0.0, float(min_on_seconds))
        slope_after = step_sheath_slope(model, slope, heating=True, dt=minimum)
        gain = _pulse_temperature_gain(model, slope, minimum)
        rise = remaining_coast_f(model, slope_after)
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
    """True while a hold pulse should keep storing heat.

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


def hold_should_heat(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    min_on_seconds: float,
    slope_f_per_sec: float | None = None,
) -> bool:
    """True when the heater should be on so the fixed crest lands on target.

    Near the target the element pulses. It turns off once the crest from the
    current sheath slope reaches the target, instead of staying on until the
    probe itself reads the setpoint and then coasting past it.
    """

    return pulse_plan(
        temp_f,
        target_f,
        on_seconds,
        model,
        min_on_seconds,
        slope_f_per_sec,
        budgeted=True,
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
        full = model.coast_rise_f or 0.0
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


def _seconds_from_fixed_crest(
    temp_f: float,
    target_f: float,
    model: ThermalModel,
    *,
    heating: bool,
    off_seconds: float,
    horizon: float,
) -> float | None:
    """Project the constant linear-heat crest forward from ``temp_f``."""

    crest = model.coast_rise_f or 0.0
    rate = model.linear_rate_f_per_sec or 0.0
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
            tail = (model.coast_seconds or 0.0) if crest > 0 else 0.0
            return min(horizon, before / rate + tail)
        # This temperature plus the fixed crest already covers the target.
        # Shut off now; the coast parabola climbs the rest.
        return _crest_time_to_gain(model, need, 0.0, horizon)
    return _crest_time_to_gain(model, need, off_seconds, horizon)


def _crest_time_to_gain(
    model: ThermalModel,
    need_f: float,
    off_seconds: float,
    horizon: float,
) -> float | None:
    """Seconds for the fixed coast parabola to climb ``need_f`` more degrees."""

    if need_f <= 0:
        return 0.0
    velocity, quad, duration = coast_curve(model)
    if quad >= 0 or velocity <= 0 or duration <= 0:
        return None
    off = min(max(0.0, off_seconds), duration)
    velocity_now = velocity + 2.0 * quad * off
    if velocity_now <= PREDICT_MIN_RATE_F_PER_SEC:
        return None
    gained = velocity * off + quad * off * off
    remaining = (model.coast_rise_f or 0.0) - gained
    if need_f > remaining + 0.05:
        return None
    disc = velocity_now * velocity_now + 4.0 * quad * need_f
    if disc < 0:
        return None
    root = (-velocity_now + math.sqrt(disc)) / (2.0 * quad)
    if root < 0:
        root = (-velocity_now - math.sqrt(disc)) / (2.0 * quad)
    if root < 0:
        return None
    return min(horizon, max(0.0, root))


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
    """Shrink or grow the climb after a hold pulse misses the target.

    ``miss_f`` is the crest minus the target. Positive means the pulse was
    hot. Returns whether the model changed.
    """

    if abs(miss_f) < 0.2:
        return False
    span = max(1.0, model.coast_rise_f or 1.0)
    adjust = max(-0.25, min(0.25, miss_f / span))
    factor = 1.0 - adjust
    changed = False
    if model.linear_rate_f_per_sec is not None:
        model.linear_rate_f_per_sec = round(max(0.0, model.linear_rate_f_per_sec * factor), 5)
        changed = True
    if model.cold_start_quad is not None:
        model.cold_start_quad = round(max(0.0, model.cold_start_quad * factor), 6)
        changed = True
    if model.coast_rise_f is not None:
        model.coast_rise_f = round(max(0.0, model.coast_rise_f * factor), 2)
        if model.coast_seconds and model.coast_seconds > 0:
            model.coast_quad = round(-model.coast_rise_f / (model.coast_seconds ** 2), 6)
        changed = True
    return changed


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
