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
# Stage II tries to land this far under the calibration temperature.
APPROACH_MARGIN_F = 10.0
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
    """Parabolas and the straight rate learned from one calibration."""

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

    def ready(self) -> bool:
        """True when a look-ahead has both a heating rate and a coast."""

        return (
            self.linear_rate_f_per_sec is not None
            and self.linear_rate_f_per_sec > 0
            and self.coast_rise_f is not None
            and self.coast_rise_f >= 0
        )

    def to_dict(self) -> dict:
        return {
            "cold_start_seconds": self.cold_start_seconds,
            "cold_start_quad": self.cold_start_quad,
            "linear_rate_f_per_sec": self.linear_rate_f_per_sec,
            "coast_rise_f": self.coast_rise_f,
            "coast_seconds": self.coast_seconds,
            "coast_quad": self.coast_quad,
            "cooldown_f_per_min": self.cooldown_f_per_min,
        }


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

    return ThermalModel(
        cold_start_seconds=optional("cold_start_seconds"),
        cold_start_quad=optional("cold_start_quad"),
        linear_rate_f_per_sec=optional("linear_rate_f_per_sec"),
        coast_rise_f=optional("coast_rise_f"),
        coast_seconds=optional("coast_seconds"),
        coast_quad=optional("coast_quad"),
        cooldown_f_per_min=optional("cooldown_f_per_min"),
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
    )


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


def coast_rise_for(model: ThermalModel, on_seconds: float) -> float:
    """Degrees the coast parabola adds after this on-interval.

    A full characterization coasts ``coast_rise_f``. A shorter pulse leaves
    with a shallower slope, so the same downward curve crests lower.
    """

    full = model.coast_rise_f or 0.0
    if full <= 0 or on_seconds <= 0:
        return 0.0
    fraction = _coast_fraction(model, on_seconds)
    return full * fraction * fraction


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


def hold_should_heat(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    min_on_seconds: float,
) -> bool:
    """True when another moment of heat still lands on or under ``target_f``.

    Far under the target the elements stay on until the crest catches it.
    Close in, they wait until the shortest legal pulse would land on it.
    """

    if temp_f >= target_f and on_seconds <= 0:
        return False
    if on_seconds > 0:
        return predicted_crest(temp_f, on_seconds, 0.0, model) < target_f
    minimum = max(0.0, min_on_seconds)
    minimum_crest = predicted_crest(temp_f, 0.0, minimum, model)
    if minimum_crest <= target_f:
        return temp_f < target_f
    rise = minimum_crest - temp_f
    return temp_f <= target_f - rise


def seconds_to_landing(
    temp_f: float,
    target_f: float,
    on_seconds: float,
    model: ThermalModel,
    *,
    heating: bool,
    horizon: float,
) -> float | None:
    """Seconds until the look-ahead crest reaches ``target_f``."""

    if temp_f >= target_f:
        return 0.0
    if not model.ready():
        return None
    if not heating:
        return _seconds_on_coast(temp_f, target_f, on_seconds, 0.0, model, horizon)
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
