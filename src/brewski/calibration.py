"""Staged calibration.

Settle, cold start, linear heat, coast, and a quiet momentum window build
the thermal model. Stage II uses that model to land under the target, then
pulses up to it. Stage III holds for ten minutes.
"""

from __future__ import annotations

from brewski.thermal import (
    APPROACH_MARGIN_F,
    CALIBRATION_TEMP_F,
    COAST_AVERAGE_SECONDS,
    COAST_PEAK_CONFIRM_SECONDS,
    COAST_PEAK_DROP_F,
    COAST_PLATEAU_RISE_F,
    COAST_PLATEAU_SECONDS,
    COAST_WATCH_MAX_SECONDS,
    COLD_START_MAX_SECONDS,
    HOLD_SECONDS,
    LINEAR_HOLD_SECONDS,
    MOMENTUM_SECONDS,
    SETTLE_SECONDS,
    ThermalModel,
    endpoint_rate,
    budgeted_coast_f,
    fit_startup_quad,
    linear_onset,
    pulse_plan,
    nudge_model,
    remaining_coast_f,
    step_sheath_slope,
)

# One named line on the chart, in the order they are written.
MARK_START = "Start"
MARK_COLD_START = "Cold Start"
MARK_HEATING = "Heating"
MARK_COASTING = "Coasting"
MARK_MOMENTUM = "Momentum"
MARK_STAGE_II = "Stage II"
MARK_STAGE_III = "Stage III"
MARK_COOLDOWN = "Cooldown"


class CalibrationSession:
    """Phase of one calibration. The controller owns the heaters and the clock."""

    def __init__(self) -> None:
        self.phase = "settle"
        self.phase_started = 0.0
        self.model = ThermalModel()
        self.samples: list[tuple[float, float]] = []
        self.heat = False
        self.finished = False
        self.reached_target = False
        self.heat_started: float | None = None
        self.open_elapsed: float | None = None
        self.open_temp: float | None = None
        self.peak_average: float | None = None
        self.peak_elapsed: float | None = None
        self.plateau_anchor: float | None = None
        self.plateau_since: float | None = None
        self.below_since: float | None = None
        self.approach_coasting = False
        self.crest_watch_since: float | None = None
        self.crest_temp: float | None = None
        self.was_heating = False
        self.pending_marks: list[str] = []
        self.pending_analysis: list[dict] = []
        self.commit = False
        self.live_rise: float | None = None
        self.live_rate: float | None = None
        self.predicted_crest: float | None = None
        # Slope of the fixed crest the sheath is on. Heating walks it toward
        # the linear rate; cooling walks it toward the peak. It does not
        # restart when the SSR cycles.
        self.sheath_slope = 0.0
        self._slope_elapsed = 0.0
        # Numbers behind the heater choice on the latest reading.
        self.trace: dict = {"path": "settle", "heat": False}

    def observe(self, elapsed: float, temp: float, on_seconds: float, min_on: float) -> None:
        """Advance one reading. Marks and analysis requests land on this object."""

        self.samples.append((elapsed, float(temp)))
        self.pending_marks = []
        self.pending_analysis = []
        self.commit = False
        dt = max(0.0, elapsed - self._slope_elapsed)
        self._slope_elapsed = elapsed
        self.sheath_slope = step_sheath_slope(
            self.model,
            self.sheath_slope,
            heating=on_seconds > 0,
            dt=dt,
        )
        self._advance(elapsed, float(temp))
        self._set_heat(elapsed, float(temp), max(0.0, on_seconds), max(0.0, min_on))
        self._watch_hold_crest(elapsed, float(temp))
        self.was_heating = self.heat

    def _control_slope(self, elapsed: float) -> float:
        """Sheath slope, raised when the probe is rising faster than that.

        The sheath model stops at the Stage I rate. A probe that is still
        steepening past that rate is storing a larger crest.
        """

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
                self._enter("cold_start", elapsed)
                self.pending_marks.append(MARK_COLD_START)
            return
        if self.phase == "cold_start":
            window = self._since(self.heat_started)
            forced = self.heat_started is not None and elapsed - self.heat_started >= COLD_START_MAX_SECONDS
            if not linear_onset(window) and not forced:
                return
            origin = self.heat_started if self.heat_started is not None else elapsed
            self.model.cold_start_seconds = round(max(0.0, elapsed - origin), 2)
            quad = fit_startup_quad(window)
            self.model.cold_start_quad = None if quad is None else round(quad, 6)
            local = endpoint_rate(window)
            self.pending_analysis.append(self._analysis("cold_start", "cold_start", local, window))
            self._enter("linear", elapsed)
            self.pending_marks.append(MARK_HEATING)
            return
        if self.phase == "linear":
            if elapsed - self.phase_started < LINEAR_HOLD_SECONDS:
                return
            window = self._since(self.phase_started)
            rate = endpoint_rate(window)
            if rate is None or rate <= 0:
                cold = self.model.cold_start_seconds or 0.0
                quad = self.model.cold_start_quad or 0.0
                rate = 2.0 * quad * cold
            self.model.linear_rate_f_per_sec = round(max(0.0, float(rate)), 5)
            # The rise just measured is the straight line, so the sheath is at
            # the start of the fixed crest.
            self.sheath_slope = self.model.linear_rate_f_per_sec
            if (self.model.cold_start_quad is None or self.model.cold_start_quad <= 0) and self.model.cold_start_seconds:
                self.model.cold_start_quad = round(
                    self.model.linear_rate_f_per_sec / (2.0 * self.model.cold_start_seconds),
                    6,
                )
            self.pending_analysis.append(
                self._analysis("linear", "linear_rate_f_per_sec", self.model.linear_rate_f_per_sec, window)
            )
            self.open_elapsed = elapsed
            self.open_temp = temp
            self._reset_peak()
            self._enter("coast", elapsed)
            self.pending_marks.append(MARK_COASTING)
            return
        if self.phase == "coast":
            self._track_peak(elapsed, temp)
            opened = self.open_elapsed if self.open_elapsed is not None else self.phase_started
            if not self._peak_ready(elapsed) and elapsed - opened < COAST_WATCH_MAX_SECONDS:
                return
            self._lock_coast(elapsed, temp)
            self._enter("momentum", elapsed)
            self.pending_marks.append(MARK_MOMENTUM)
            return
        if self.phase == "momentum":
            if elapsed - self.phase_started < MOMENTUM_SECONDS:
                return
            window = self._since(self.phase_started)
            rate = endpoint_rate(window)
            per_min = 0.0 if rate is None else max(0.0, -rate * 60.0)
            self.model.cooldown_f_per_min = round(per_min, 2)
            self.pending_analysis.append(
                self._analysis("momentum", "cooldown_f_per_min", self.model.cooldown_f_per_min, window)
            )
            self.approach_coasting = False
            self._reset_peak()
            self.commit = True
            self._enter("approach", elapsed)
            self.pending_marks.append(MARK_STAGE_II)
            return
        if self.phase == "approach":
            if temp >= CALIBRATION_TEMP_F:
                self._begin_hold(elapsed)
                return
            if not self.approach_coasting:
                return
            self._track_peak(elapsed, temp)
            opened = self.open_elapsed if self.open_elapsed is not None else self.phase_started
            coast_for = self.model.coast_seconds or 0.0
            timed_out = elapsed - opened >= max(coast_for, 20.0)
            if not self._peak_ready(elapsed) and not timed_out:
                return
            goal = CALIBRATION_TEMP_F - APPROACH_MARGIN_F
            self.approach_coasting = False
            self._reset_peak()
            if temp >= goal - 2.0:
                self._enter("pulses", elapsed)
            return
        if self.phase == "pulses":
            if temp >= CALIBRATION_TEMP_F:
                self._begin_hold(elapsed)
            return
        if self.phase == "hold":
            if elapsed - self.phase_started >= HOLD_SECONDS:
                self.pending_marks.append(MARK_COOLDOWN)
                self.commit = True
                self.finished = True
                self.heat = False

    def _set_heat(self, elapsed: float, temp: float, on_seconds: float, min_on: float) -> None:
        self.live_rise = None
        self.live_rate = None
        self.predicted_crest = None
        self.trace = {"path": self.phase, "heat": False, "sheath_slope_f_per_sec": round(self.sheath_slope, 5)}
        if self.finished or self.phase in {"settle", "coast", "momentum"}:
            self.heat = False
            if self.phase == "coast" and self.open_temp is not None:
                self.live_rise = temp - self.open_temp
            if self.phase == "momentum":
                rate = endpoint_rate(self._since(self.phase_started))
                if rate is not None:
                    self.live_rate = max(0.0, -rate * 60.0)
            self.trace = {
                "path": self.phase,
                "heat": False,
                "sheath_slope_f_per_sec": round(self.sheath_slope, 5),
                "live_rise_f": None if self.live_rise is None else round(self.live_rise, 2),
                "live_rate_f_per_min": None if self.live_rate is None else round(self.live_rate, 2),
            }
            return
        if self.phase in {"cold_start", "linear"}:
            self.heat = True
            self.trace = {
                "path": self.phase,
                "heat": True,
                "sheath_slope_f_per_sec": round(self.sheath_slope, 5),
            }
            return
        if self.phase == "approach":
            goal = CALIBRATION_TEMP_F - APPROACH_MARGIN_F
            if self.approach_coasting:
                self.heat = False
                if self.open_temp is not None:
                    self.live_rise = temp - self.open_temp
                self.trace = {
                    "path": "approach_coast",
                    "heat": False,
                    "goal_f": goal,
                    "live_rise_f": None if self.live_rise is None else round(self.live_rise, 2),
                    "sheath_slope_f_per_sec": round(self.sheath_slope, 5),
                }
                return
            slope = self._control_slope(elapsed)
            rise = remaining_coast_f(self.model, slope)
            self.predicted_crest = temp + rise
            shutoff = temp + rise >= goal or temp >= goal
            self.trace = {
                "path": "approach_coast" if shutoff else "approach",
                "heat": not shutoff,
                "slope_f_per_sec": round(slope, 5),
                "sheath_slope_f_per_sec": round(self.sheath_slope, 5),
                "remaining_coast_f": round(rise, 2),
                "predicted_crest_f": round(self.predicted_crest, 2),
                "goal_f": goal,
                "learned_coast_f": self.model.coast_rise_f,
                "linear_rate_f_per_sec": self.model.linear_rate_f_per_sec,
            }
            if shutoff:
                self.approach_coasting = True
                self.open_elapsed = elapsed
                self.open_temp = temp
                self._reset_peak()
                self.heat = False
                return
            self.heat = True
            return
        if self.phase == "pulses":
            slope = self._control_slope(elapsed)
            plan = pulse_plan(
                temp,
                CALIBRATION_TEMP_F,
                on_seconds,
                self.model,
                min_on,
                slope_f_per_sec=slope,
                budgeted=True,
            )
            self.predicted_crest = temp + budgeted_coast_f(self.model, slope, temp, CALIBRATION_TEMP_F)
            self.heat = bool(plan["heat"])
            self.trace = {"path": "pulses", "goal_f": CALIBRATION_TEMP_F, **plan}
            return
        if self.phase == "hold":
            # Stay on until the coast stored so far will reach the target,
            # then open and let that coast play. A wider gap is a longer pulse.
            slope = self._control_slope(elapsed)
            plan = pulse_plan(
                temp,
                CALIBRATION_TEMP_F,
                on_seconds,
                self.model,
                min_on,
                slope_f_per_sec=slope,
                budgeted=False,
            )
            self.predicted_crest = plan["predicted_crest_f"]
            self.heat = bool(plan["heat"])
            self.trace = {"path": "hold", "goal_f": CALIBRATION_TEMP_F, **plan}

    def _watch_hold_crest(self, elapsed: float, temp: float) -> None:
        """After a hold pulse ends, move the model toward the crest it made."""

        if self.phase != "hold" or self.finished:
            self.crest_watch_since = None
            self.crest_temp = None
            return
        if self.was_heating and not self.heat:
            self.crest_watch_since = elapsed
            self.crest_temp = temp
            return
        if self.heat or self.crest_watch_since is None:
            return
        if self.crest_temp is None or temp > self.crest_temp:
            self.crest_temp = temp
        wait = self.model.coast_seconds or 15.0
        if elapsed - self.crest_watch_since < max(5.0, wait):
            return
        if nudge_model(self.model, self.crest_temp - CALIBRATION_TEMP_F):
            self.commit = True
        self.crest_watch_since = None
        self.crest_temp = None

    def _lock_coast(self, elapsed: float, temp: float) -> None:
        opened = self.open_temp if self.open_temp is not None else temp
        peak = self.peak_average if self.peak_average is not None else temp
        peak_at = self.peak_elapsed if self.peak_elapsed is not None else elapsed
        open_at = self.open_elapsed if self.open_elapsed is not None else self.phase_started
        self.model.coast_rise_f = round(max(0.0, peak - opened), 2)
        self.model.coast_seconds = round(max(0.0, peak_at - open_at), 2)
        if self.model.coast_seconds > 0:
            self.model.coast_quad = round(
                -self.model.coast_rise_f / (self.model.coast_seconds ** 2),
                6,
            )
        window = self._since(open_at)
        self.pending_analysis.append(
            self._analysis("coast", "coast_rise_f", self.model.coast_rise_f, window)
        )
        self.commit = True

    def _begin_hold(self, elapsed: float) -> None:
        self.reached_target = True
        self.approach_coasting = False
        self._enter("hold", elapsed)
        self.pending_marks.append(MARK_STAGE_III)
        self.commit = True

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
