"""Live tests from the Software menu.

Initial Heating records the curve and nothing else. The heaters stay on for
three minutes, then off for three minutes. The samples and the two marks are
the result. Nothing here writes the heating model.
"""

from __future__ import annotations

from brewski.thermal import SETTLE_SECONDS

TEST_INITIAL_HEATING = "initial_heating"
TEST_LABEL = "Initial Heating"
MARK_INITIAL_HEATING = "Initial Heating"
MARK_HEATERS_OFF = "Heaters Off"
HEAT_SECONDS = 3 * 60.0
COAST_SECONDS = 3 * 60.0


class InitialHeatingSession:
    """Settle, heat for three minutes, then watch the coast for three minutes.

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
        self.heat_stopped: float | None = None
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
        if self.phase == "heating":
            if self.heat_started is not None and elapsed - self.heat_started >= HEAT_SECONDS:
                self.heat_stopped = elapsed
                self.phase = "coast"
                self.phase_started = elapsed
                self.pending_marks.append(MARK_HEATERS_OFF)
                self.heat = False
                self.trace = {"path": "coast", "heat": False}
                return
            self.heat = True
            self.trace = {"path": "heating", "heat": True}
            return
        if (
            self.heat_stopped is not None
            and elapsed - self.heat_stopped >= COAST_SECONDS
        ):
            self._complete(elapsed, float(temp), found=True, stopped=False)
            return
        self.heat = False
        self.trace = {"path": "coast", "heat": False}

    def abort(self, elapsed: float, temp: float) -> None:
        """Stop before the six minutes are up. The samples so far are kept."""

        if self.finished:
            return
        if not self.samples or self.samples[-1][0] != elapsed:
            self.samples.append((elapsed, float(temp)))
        self.pending_marks = []
        self._complete(elapsed, float(temp), found=False, stopped=True)

    def phase_elapsed(self) -> float:
        """Seconds into the phase on the panel clock."""

        if not self.samples:
            return 0.0
        return max(0.0, self.samples[-1][0] - self.phase_started)

    def phase_limit(self) -> float:
        if self.phase == "heating":
            return HEAT_SECONDS
        if self.phase == "coast":
            return COAST_SECONDS
        return SETTLE_SECONDS

    def payload(self, *, review: bool, run_id: int | None) -> dict:
        """Status fields for the panel, the dashboard, and the log."""

        if self.result is None:
            body = _empty_result(self.phase)
        else:
            body = dict(self.result)
            body["phase"] = "done"
        body["phase_seconds"] = round(self.phase_elapsed(), 1)
        body["phase_limit"] = self.phase_limit()
        body["review"] = review
        body["run_id"] = run_id
        return body

    def _complete(self, elapsed: float, temp: float, *, found: bool, stopped: bool) -> None:
        self.heat = False
        self.finished = True
        self.phase = "done"
        self.result = self._measure(elapsed, temp, found=found, stopped=stopped)
        self.trace = {"path": "done", "heat": False}

    def _measure(self, elapsed: float, temp: float, *, found: bool, stopped: bool) -> dict:
        result = _empty_result("done")
        result["found"] = found
        result["stopped"] = stopped
        result["end_temp_f"] = round(temp, 1)
        if self.heat_started is None:
            return result
        on_end = self.heat_stopped if self.heat_stopped is not None else elapsed
        result["on_seconds"] = round(max(0.0, on_end - self.heat_started), 1)
        start_temp = _reading_at(self.samples, self.heat_started)
        result["start_temp_f"] = None if start_temp is None else round(start_temp, 1)
        if self.heat_stopped is None:
            return result
        off_temp = _reading_at(self.samples, self.heat_stopped)
        result["off_temp_f"] = None if off_temp is None else round(off_temp, 1)
        result["off_seconds"] = round(max(0.0, elapsed - self.heat_stopped), 1)
        return result


def _empty_result(phase: str) -> dict:
    return {
        "id": TEST_INITIAL_HEATING,
        "phase": phase,
        "found": False,
        "stopped": False,
        "on_seconds": None,
        "off_seconds": None,
        "start_temp_f": None,
        "off_temp_f": None,
        "end_temp_f": None,
    }


def _reading_at(samples: list[tuple[float, float]], stamp: float) -> float | None:
    chosen: float | None = None
    for sample_at, reading in samples:
        if sample_at > stamp:
            break
        chosen = reading
    return chosen
