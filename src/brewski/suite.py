"""Live tests from the Software menu.

Initial Heating is the first piece of Stage I: settle, heat until the rise
stops getting steeper, and fit the exponential the element soaks in on. The
same time constant is the climb left when the heaters open. It does not write
the heating model.
"""

from __future__ import annotations

from brewski.thermal import (
    COLD_START_MAX_SECONDS,
    SETTLE_SECONDS,
    endpoint_rate,
    fit_heating_lag,
    heating_lag_coast,
    slope_has_settled,
)

TEST_INITIAL_HEATING = "initial_heating"
TEST_LABEL = "Initial Heating"
MARK_INITIAL_HEATING = "Initial Heating"
MARK_LINEAR = "Linear"


class InitialHeatingSession:
    """Settle, then heat until the climb is straight.

    The controller owns the heaters and the clock. ``heat`` is the request
    for the next command. Marks land in ``pending_marks`` for that reading.
    """

    def __init__(self) -> None:
        self.phase = "settle"
        self.phase_started = 0.0
        self.samples: list[tuple[float, float]] = []
        self.heat = False
        self.finished = False
        self.heat_started: float | None = None
        self.pending_marks: list[str] = []
        self.result: dict | None = None
        self.trace: dict = {"path": "settle", "heat": False}

    def observe(self, elapsed: float, temp: float) -> None:
        """Advance one reading."""

        self.samples.append((elapsed, float(temp)))
        self.pending_marks = []
        if self.finished:
            self.heat = False
            self.trace = {"path": "done", "heat": False}
            return
        if self.phase == "settle":
            if elapsed - self.phase_started < SETTLE_SECONDS:
                self.heat = False
                self.trace = {"path": "settle", "heat": False}
                return
            self.heat_started = elapsed
            self.phase = "heating"
            self.phase_started = elapsed
            self.pending_marks.append(MARK_INITIAL_HEATING)
            self.heat = True
            self.trace = {"path": "heating", "heat": True}
            return
        self._heat_step(elapsed, float(temp))

    def abort(self, elapsed: float, temp: float) -> None:
        """Stop before the rise goes straight. The measurement so far is kept."""

        if self.finished:
            return
        if not self.samples or self.samples[-1][0] != elapsed:
            self.samples.append((elapsed, float(temp)))
        self.pending_marks = []
        self._complete(elapsed, float(temp), found=False, stopped=True)

    def payload(self, *, review: bool, run_id: int | None) -> dict:
        """Status fields for the panel, the dashboard, and the log."""

        if self.result is None:
            body = {
                "id": TEST_INITIAL_HEATING,
                "phase": self.phase,
                "found": False,
                "stopped": False,
                "slope_f_per_sec": None,
                "slope_f_per_min": None,
                "approach_f_per_sec": None,
                "approach_f_per_min": None,
                "tau_seconds": None,
                "coast_rise_f": None,
                "rise_f": None,
                "seconds": None,
                "start_temp_f": None,
                "linear_temp_f": None,
            }
        else:
            body = dict(self.result)
            body["phase"] = "done"
        body["review"] = review
        body["run_id"] = run_id
        return body

    def _heat_step(self, elapsed: float, temp: float) -> None:
        window = self._since(self.heat_started)
        found = slope_has_settled(window)
        forced = (
            self.heat_started is not None
            and elapsed - self.heat_started >= COLD_START_MAX_SECONDS
        )
        if not found and not forced:
            self.heat = True
            self.trace = {"path": "heating", "heat": True}
            return
        self._complete(elapsed, temp, found=found, stopped=False)

    def _complete(self, elapsed: float, temp: float, *, found: bool, stopped: bool) -> None:
        self.heat = False
        self.finished = True
        self.phase = "done"
        if found:
            self.pending_marks.append(MARK_LINEAR)
        self.result = self._measure(elapsed, temp, found=found, stopped=stopped)
        self.trace = {"path": "done", "heat": False}

    def _measure(self, elapsed: float, temp: float, *, found: bool, stopped: bool) -> dict:
        if self.heat_started is None:
            return {
                "id": TEST_INITIAL_HEATING,
                "found": False,
                "stopped": stopped,
                "slope_f_per_sec": None,
                "slope_f_per_min": None,
                "approach_f_per_sec": None,
                "approach_f_per_min": None,
                "tau_seconds": None,
                "coast_rise_f": None,
                "rise_f": None,
                "seconds": None,
                "start_temp_f": round(temp, 1),
                "linear_temp_f": None,
            }
        window = self._since(self.heat_started)
        rate = endpoint_rate(window)
        start_temp = window[0][1] if window else temp
        heated = max(0.0, elapsed - self.heat_started)
        lag = fit_heating_lag(window)
        approach = None if lag is None else lag[0]
        tau = None if lag is None else lag[1]
        coast = None if lag is None else heating_lag_coast(approach, tau, heated)
        return {
            "id": TEST_INITIAL_HEATING,
            "found": found,
            "stopped": stopped,
            "slope_f_per_sec": None if rate is None else round(rate, 5),
            "slope_f_per_min": None if rate is None else round(rate * 60.0, 1),
            "approach_f_per_sec": None if approach is None else round(approach, 5),
            "approach_f_per_min": None if approach is None else round(approach * 60.0, 1),
            "tau_seconds": None if tau is None else round(tau, 1),
            "coast_rise_f": None if coast is None else round(coast, 1),
            "rise_f": round(temp - start_temp, 1),
            "seconds": round(heated, 1),
            "start_temp_f": round(start_temp, 1),
            "linear_temp_f": round(temp, 1) if found else None,
        }

    def _since(self, origin: float | None) -> list[tuple[float, float]]:
        if origin is None:
            return list(self.samples)
        return [(stamp, reading) for stamp, reading in self.samples if stamp >= origin]
