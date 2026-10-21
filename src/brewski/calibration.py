"""Staged calibration.

Initial heating learns the exponential soak. Linear heating learns the
straight rate, then the heaters open while the probe is still under 140°F.
The coast learns the negative exponential up to equilibrium. That equilibrium
is scored against 140°F and the guess is tuned. One predicted pulse climbs to
160°F. A shorter pulse follows only when that coast lands short. The hold
keeps 160°F for ten minutes. Each pulse is the minimum on time, and the next
one waits until that pulse's coast has arrived.
"""

from __future__ import annotations

import math

from brewski.thermal import (
    CALIBRATION_TEMP_F,
    COAST_AVERAGE_SECONDS,
    COAST_PEAK_CONFIRM_SECONDS,
    COAST_PEAK_DROP_F,
    COAST_PLATEAU_RISE_F,
    COAST_PLATEAU_SECONDS,
    COAST_WATCH_MAX_SECONDS,
    HOLD_RISE_F_PER_SEC,
    HOLD_SECONDS,
    HOLD_SLOPE_MIN_SPAN_SECONDS,
    HOLD_SLOPE_WINDOW_SECONDS,
    LEARN_HEAT_MAX_SECONDS,
    LEARN_TEMP_F,
    SETTLE_SECONDS,
    TUNE_DEADBAND_F,
    ThermalModel,
    coast_tau_of,
    endpoint_rate,
    fit_coast_exponential,
    formula_slope,
    heating_formula_temp,
    hold_maintenance_plan,
    pulse_plan,
    seed_model,
    settled_linear_rate,
    step_sheath_slope,
    tune_model,
)

MARK_START = "Start"
MARK_INITIAL = "Initial heating"
MARK_LINEAR = "Linear heating"
MARK_COASTING = "Coasting"
MARK_ADJUSTED = "Adjusted"
MARK_PULSE = "Pulse"
MARK_HOLDING = "Holding"
MARK_COOLDOWN = "Cooldown"

# Kept so older logs and tests still import the previous names.
MARK_COLD_START = MARK_INITIAL
MARK_HEATING = MARK_LINEAR
MARK_MOMENTUM = MARK_ADJUSTED
MARK_STAGE_II = MARK_PULSE
MARK_STAGE_III = MARK_HOLDING


class CalibrationSession:
    """Phase of one calibration. The controller owns the heaters and the clock."""

    def __init__(self, prior: ThermalModel | None = None) -> None:
        self.phase = "settle"
        self.phase_started = 0.0
        self.guess = seed_model(prior)
        # Measured fields only. The seed stays on ``guess`` until a phase
        # actually records it, so a saved model is not replaced by the guess.
        self.model = ThermalModel()
        self.samples: list[tuple[float, float]] = []
        self.heat = False
        self.finished = False
        self.reached_target = False
        self.heat_started: float | None = None
        self.heat_origin: float | None = None
        self.open_elapsed: float | None = None
        self.open_temp: float | None = None
        self.shutoff_slope = 0.0
        self.peak_average: float | None = None
        self.peak_elapsed: float | None = None
        self.plateau_anchor: float | None = None
        self.plateau_since: float | None = None
        self.below_since: float | None = None
        self.pulse_coasting = False
        # Elapsed time when the hold last opened the SSR. The next pulse waits
        # until that coast is falling, or until one coast time constant has passed.
        self.hold_released_at: float | None = None
        self.rate_locked = False
        self._locked_rate: float | None = None
        self.was_heating = False
        self.pending_marks: list[str] = []
        self.pending_analysis: list[dict] = []
        self.commit = False
        self.live_rise: float | None = None
        self.live_rate: float | None = None
        self.predicted_crest: float | None = None
        self.sheath_slope = 0.0
        self._slope_elapsed = 0.0
        self.trace: dict = {"path": "settle", "heat": False}

    def observe(
        self,
        elapsed: float,
        temp: float,
        on_seconds: float,
        min_on: float,
        off_seconds: float = 0.0,
        min_off: float = 0.0,
    ) -> None:
        """Advance one reading. Marks and analysis requests land on this object."""

        self.samples.append((elapsed, float(temp)))
        self.pending_marks = []
        self.pending_analysis = []
        self.commit = False
        self._adopt_measured_rate()
        dt = max(0.0, elapsed - self._slope_elapsed)
        self._slope_elapsed = elapsed
        self.sheath_slope = step_sheath_slope(
            self.guess,
            self.sheath_slope,
            heating=on_seconds > 0,
            dt=dt,
        )
        self._advance(elapsed, float(temp))
        self._set_heat(
            elapsed,
            float(temp),
            max(0.0, on_seconds),
            max(0.0, min_on),
            off_seconds=off_seconds,
            min_off=min_off,
        )
        if self.phase == "hold" and self.was_heating and not self.heat:
            self.hold_released_at = elapsed
        if self.phase == "pulse" and self.was_heating and not self.heat:
            self.pulse_coasting = True
            self.open_elapsed = elapsed
            self.open_temp = temp
            self.shutoff_slope = self._control_slope(elapsed)
            self._reset_peak()
        self.was_heating = self.heat

    def _adopt_measured_rate(self) -> None:
        """A Grok replacement lands on the measured model. The guess follows it."""

        measured = self.model.linear_rate_f_per_sec
        if measured is None or self._locked_rate is None:
            return
        if abs(measured - self._locked_rate) <= 1e-9:
            return
        self.guess.linear_rate_f_per_sec = measured
        self._locked_rate = measured

    def _control_slope(self, elapsed: float) -> float:
        """Sheath slope, raised when the probe is rising faster than that."""

        recent = [(stamp, reading) for stamp, reading in self.samples if stamp >= elapsed - 12.0]
        rate = endpoint_rate(recent)
        live = rate if rate is not None and rate > 0 else 0.0
        return max(self.sheath_slope, live)

    def _since(self, origin: float | None) -> list[tuple[float, float]]:
        if origin is None:
            return list(self.samples)
        return [(stamp, reading) for stamp, reading in self.samples if stamp >= origin]

    def _enter(self, phase: str, elapsed: float) -> None:
        self.phase = phase
        self.phase_started = elapsed

    def _reset_peak(self) -> None:
        self.peak_average = None
        self.peak_elapsed = None
        self.plateau_anchor = None
        self.plateau_since = None
        self.below_since = None

    def _advance(self, elapsed: float, temp: float) -> None:
        if self.finished:
            self.heat = False
            return
        if self.phase == "settle":
            if elapsed - self.phase_started >= SETTLE_SECONDS:
                self.heat_started = elapsed
                self.heat_origin = temp
                self._enter("initial", elapsed)
                self.pending_marks.append(MARK_INITIAL)
            return
        if self.phase == "initial":
            self._tune_heat(elapsed, temp)
            if self._must_open(elapsed, temp):
                self._lock_rate(elapsed)
                self._begin_coast(elapsed, temp)
                return
            window = self._since(self.heat_started)
            if settled_linear_rate(window) is not None:
                self._lock_rate(elapsed)
                self._enter("linear", elapsed)
                self.pending_marks.append(MARK_LINEAR)
            return
        if self.phase == "linear":
            self._tune_heat(elapsed, temp)
            self._lock_rate(elapsed)
            self._begin_coast(elapsed, temp)
            return
        if self.phase == "coast":
            self._tune_coast(elapsed, temp)
            self._track_peak(elapsed, temp)
            opened = self.open_elapsed if self.open_elapsed is not None else self.phase_started
            if not self._peak_ready(elapsed) and elapsed - opened < COAST_WATCH_MAX_SECONDS:
                return
            self._finish_coast(elapsed, temp)
            return
        if self.phase == "pulse":
            if self.pulse_coasting:
                self._watch_pulse_coast(elapsed, temp, enter_hold=True)
                return
            if temp >= CALIBRATION_TEMP_F:
                self._begin_hold(elapsed)
            return
        if self.phase == "hold":
            if self.pulse_coasting:
                self._watch_pulse_coast(elapsed, temp, enter_hold=False)
            if elapsed - self.phase_started >= HOLD_SECONDS:
                self.pending_marks.append(MARK_COOLDOWN)
                self.commit = True
                self.finished = True
                self.heat = False

    def _must_open(self, elapsed: float, temp: float) -> bool:
        """True when another second of heat could carry the coast through 140°F."""

        if temp >= LEARN_TEMP_F:
            return True
        started = self.heat_started if self.heat_started is not None else elapsed
        if elapsed - started >= LEARN_HEAT_MAX_SECONDS:
            return True
        tau = coast_tau_of(self.guess)
        if tau > 0 and temp + self._control_slope(elapsed) * tau >= LEARN_TEMP_F:
            return True
        return False

    def _tune_heat(self, elapsed: float, temp: float) -> None:
        if self.heat_started is None or self.heat_origin is None:
            return
        on_seconds = max(0.0, elapsed - self.heat_started)
        formula = heating_formula_temp(self.heat_origin, on_seconds, self.guess)
        expected = formula_slope(on_seconds, self.guess)
        tune_model(
            self.guess,
            heat_delta_f=temp - formula,
            slope_delta=self._control_slope(elapsed) - expected,
        )

    def _tune_coast(self, elapsed: float, temp: float) -> None:
        if self.open_elapsed is None or self.open_temp is None:
            return
        age = max(0.0, elapsed - self.open_elapsed)
        tau = coast_tau_of(self.guess)
        if tau <= 0 or self.shutoff_slope <= 0:
            return
        predicted = self.shutoff_slope * tau * (1.0 - math.exp(-age / tau))
        tune_model(self.guess, coast_delta_f=(temp - self.open_temp) - predicted)

    def _lock_rate(self, elapsed: float) -> None:
        if self.rate_locked:
            return
        window = self._since(self.heat_started)
        rate = settled_linear_rate(window)
        if rate is None:
            rate = endpoint_rate(window)
        if rate is None or rate <= 0:
            rate = self.guess.linear_rate_f_per_sec
        if rate is None or rate <= 0:
            return
        locked = round(max(0.0, float(rate)), 5)
        self.model.linear_rate_f_per_sec = locked
        self.model.soak_tau_seconds = self.guess.soak_tau_seconds
        self.guess.linear_rate_f_per_sec = locked
        self._locked_rate = locked
        self.rate_locked = True
        self.pending_analysis.append(self._analysis("linear", "linear_rate_f_per_sec", locked, window))
        self.commit = True

    def _begin_coast(self, elapsed: float, temp: float) -> None:
        self.shutoff_slope = self._control_slope(elapsed)
        self.open_elapsed = elapsed
        self.open_temp = temp
        self._reset_peak()
        self._enter("coast", elapsed)
        self.pending_marks.append(MARK_COASTING)

    def _finish_coast(self, elapsed: float, temp: float) -> None:
        opened = self.open_temp if self.open_temp is not None else temp
        peak = self.peak_average if self.peak_average is not None else temp
        window = self._since(self.open_elapsed if self.open_elapsed is not None else self.phase_started)
        fitted = fit_coast_exponential(window, self.shutoff_slope)
        if fitted is not None:
            rise, tau = fitted
            self.guess.coast_rise_f = round(max(0.0, rise), 2)
            self.guess.coast_tau_seconds = round(max(1.0, tau), 2)
        else:
            self.guess.coast_rise_f = round(max(0.0, peak - opened), 2)
        tune_model(self.guess, crest_delta_f=peak - LEARN_TEMP_F)
        tail = [(stamp, reading) for stamp, reading in window if stamp >= elapsed - 30.0]
        cooling = endpoint_rate(tail)
        per_min = 0.0 if cooling is None or cooling >= 0 else max(0.0, -cooling * 60.0)
        self.guess.cooldown_f_per_min = round(per_min, 2)
        if self.guess.coast_tau_seconds:
            self.guess.coast_seconds = round(self.guess.coast_tau_seconds * 3.0, 2)
        # The rise was measured at this shutoff. A warmer pot runs further.
        self.guess.coast_at_f = round(float(opened), 2)
        self._copy_coast_to_model()
        self.pending_analysis.append(
            self._analysis("coast", "coast_rise_f", self.model.coast_rise_f, window)
        )
        self.sheath_slope = 0.0
        self.pulse_coasting = False
        self.pending_marks.append(MARK_ADJUSTED)
        self.commit = True
        self._enter("pulse", elapsed)
        self.pending_marks.append(MARK_PULSE)

    def _copy_coast_to_model(self) -> None:
        self.model.coast_rise_f = self.guess.coast_rise_f
        self.model.coast_tau_seconds = self.guess.coast_tau_seconds
        self.model.coast_seconds = self.guess.coast_seconds
        self.model.coast_at_f = self.guess.coast_at_f
        self.model.soak_tau_seconds = self.guess.soak_tau_seconds
        self.model.cooldown_f_per_min = self.guess.cooldown_f_per_min
        if self.model.linear_rate_f_per_sec is None:
            self.model.linear_rate_f_per_sec = self.guess.linear_rate_f_per_sec
        if (
            self.model.coast_rise_f is not None
            and self.model.coast_seconds
            and self.model.coast_seconds > 0
        ):
            self.model.coast_quad = round(
                -self.model.coast_rise_f / (self.model.coast_seconds ** 2),
                6,
            )

    def _watch_pulse_coast(self, elapsed: float, temp: float, *, enter_hold: bool) -> None:
        self._track_peak(elapsed, temp)
        opened = self.open_elapsed if self.open_elapsed is not None else self.phase_started
        limit = self.guess.coast_seconds or 20.0
        if not self._peak_ready(elapsed) and elapsed - opened < max(limit, 20.0):
            return
        peak = self.peak_average if self.peak_average is not None else temp
        self.pulse_coasting = False
        self._reset_peak()
        self.sheath_slope = 0.0
        tune_model(self.guess, crest_delta_f=peak - CALIBRATION_TEMP_F)
        self._copy_coast_to_model()
        self.commit = True
        landed = peak >= CALIBRATION_TEMP_F - TUNE_DEADBAND_F or temp >= CALIBRATION_TEMP_F - TUNE_DEADBAND_F
        if enter_hold and landed:
            self._begin_hold(elapsed)

    def _begin_hold(self, elapsed: float) -> None:
        self.reached_target = True
        self.pulse_coasting = False
        self._enter("hold", elapsed)
        self.pending_marks.append(MARK_HOLDING)
        self.commit = True

    def _probe_slope(self, elapsed: float) -> float | None:
        """°F/s over the last few seconds, once that window is long enough."""

        recent = [
            (stamp, reading)
            for stamp, reading in self.samples
            if stamp >= elapsed - HOLD_SLOPE_WINDOW_SECONDS
        ]
        if len(recent) < 2 or recent[-1][0] - recent[0][0] < HOLD_SLOPE_MIN_SPAN_SECONDS:
            return None
        return endpoint_rate(recent)

    def _coast_still_arriving(self, elapsed: float) -> bool:
        """True after a hold pulse until its coast has crested or aged out."""

        opened = self.hold_released_at
        if opened is None:
            return False
        age = elapsed - opened
        tau = coast_tau_of(self.guess)
        limit = tau if tau > 0 else 30.0
        slope = self._probe_slope(elapsed)
        if (slope is not None and slope < -HOLD_RISE_F_PER_SEC) or age >= limit:
            self.hold_released_at = None
            return False
        return True

    def _set_heat(
        self,
        elapsed: float,
        temp: float,
        on_seconds: float,
        min_on: float,
        *,
        off_seconds: float = 0.0,
        min_off: float = 0.0,
    ) -> None:
        self.live_rise = None
        self.live_rate = None
        self.predicted_crest = None
        slope = self._control_slope(elapsed)
        self.trace = {
            "path": self.phase,
            "heat": False,
            "sheath_slope_f_per_sec": round(self.sheath_slope, 5),
        }
        if self.finished or self.phase in {"settle", "coast"} or self.pulse_coasting:
            self.heat = False
            if self.open_temp is not None and self.phase in {"coast", "pulse", "hold"}:
                self.live_rise = temp - self.open_temp
            self.trace = {
                "path": "pulse_coast" if self.pulse_coasting and self.phase == "pulse" else self.phase,
                "heat": False,
                "sheath_slope_f_per_sec": round(self.sheath_slope, 5),
                "live_rise_f": None if self.live_rise is None else round(self.live_rise, 2),
                "goal_f": LEARN_TEMP_F if self.phase in {"settle", "initial", "linear", "coast"} else CALIBRATION_TEMP_F,
            }
            return
        if self.phase in {"initial", "linear"}:
            self.heat = temp < LEARN_TEMP_F
            self.trace = {
                "path": self.phase,
                "heat": self.heat,
                "goal_f": LEARN_TEMP_F,
                "sheath_slope_f_per_sec": round(self.sheath_slope, 5),
                "slope_f_per_sec": round(slope, 5),
            }
            return
        if self.phase == "hold":
            goal = CALIBRATION_TEMP_F
            plan = hold_maintenance_plan(
                temp,
                goal,
                on_seconds,
                self.guess,
                min_on,
                sheath_slope=self.sheath_slope,
                off_seconds=off_seconds,
                min_off_seconds=min_off,
                probe_slope=self._probe_slope(elapsed),
            )
            # A pulse that just opened is still delivering its coast. Block the
            # next one, and say so, even when the cold-sheath landing looks short.
            waiting = plan.get("hold_wait")
            if (
                on_seconds <= 0
                and waiting not in {"at_target", "min_off"}
                and self._coast_still_arriving(elapsed)
            ):
                plan = dict(plan)
                plan["heat"] = False
                plan["hold_wait"] = "coast"
            self.predicted_crest = plan["predicted_crest_f"]
            self.heat = bool(plan["heat"])
            self.trace = {"path": self.phase, "goal_f": goal, **plan}
            return
        if self.phase == "pulse":
            goal = CALIBRATION_TEMP_F
            plan = pulse_plan(
                temp,
                goal,
                on_seconds,
                self.guess,
                min_on,
                slope_f_per_sec=slope,
                budgeted=False,
            )
            self.predicted_crest = plan["predicted_crest_f"]
            self.heat = bool(plan["heat"]) and temp < goal
            self.trace = {"path": self.phase, "goal_f": goal, **plan}
            return
        self.heat = False

    def _analysis(
        self,
        kind: str,
        field: str,
        local: float | None,
        window: list[tuple[float, float]],
    ) -> dict:
        return {
            "kind": kind,
            "field": field,
            "local": None if local is None else round(float(local), 5),
            "samples": [[round(stamp, 2), round(reading, 3)] for stamp, reading in window],
        }

    def _track_peak(self, elapsed: float, temp: float) -> None:
        opened = self.open_elapsed if self.open_elapsed is not None else self.phase_started
        samples = self._since(opened)
        if not samples or elapsed - samples[0][0] < COAST_AVERAGE_SECONDS:
            return
        window = [reading for stamp, reading in samples if stamp >= elapsed - COAST_AVERAGE_SECONDS]
        if not window:
            return
        average = sum(window) / len(window)
        peak = self.peak_average
        if peak is None or average > peak + 0.01:
            self.peak_average = average
            self.peak_elapsed = elapsed
            self.below_since = None
            peak = average
        if self.plateau_anchor is None:
            self.plateau_anchor = average
            self.plateau_since = elapsed
        elif average >= self.plateau_anchor + COAST_PLATEAU_RISE_F:
            self.plateau_anchor = average
            self.plateau_since = elapsed
        if peak is not None and average <= peak - COAST_PEAK_DROP_F:
            if self.below_since is None:
                self.below_since = elapsed
        else:
            self.below_since = None

    def _peak_ready(self, elapsed: float) -> bool:
        if self.peak_average is None:
            return False
        if self.below_since is not None and elapsed - self.below_since >= COAST_PEAK_CONFIRM_SECONDS:
            return True
        if self.plateau_since is not None and elapsed - self.plateau_since >= COAST_PLATEAU_SECONDS:
            return True
        opened = self.open_elapsed if self.open_elapsed is not None else self.phase_started
        return elapsed - opened >= COAST_WATCH_MAX_SECONDS
