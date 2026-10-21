"""Initial Heating: the first live test in the Software menu."""

import math

import pytest

from dataclasses import replace

from brewski.controller import Controller, ControllerError
from brewski.panel import indicator_leds
from brewski.suite import InitialHeatingSession


class _Panel:
    def __init__(self, events):
        self.events = events

    def take(self):
        events = self.events
        self.events = (0, 0, 0, 0)
        return events

    def set_leds(self, *_args):
        return None

    def close(self):
        return None


class _Source:
    def __init__(self, temps):
        self.temps = list(temps)
        self.heaters = [False, False]

    def read_temperatures(self, count):
        return list(self.temps[:count])

    def set_heaters(self, states):
        padded = [False, False]
        for index, on in enumerate(states[:2]):
            padded[index] = bool(on)
        self.heaters = padded


def test_settle_then_a_straight_rise_reports_the_chord_slope():
    session = InitialHeatingSession()
    marks = []
    for stamp in (0, 1, 2):
        session.observe(stamp, 70)
        marks.extend(session.pending_marks)
        assert session.heat is False
        assert session.phase == "settle"
    session.observe(3, 70)
    marks.extend(session.pending_marks)
    assert session.heat is True
    assert session.phase == "heating"
    finished = None
    for step in range(1, 90):
        session.observe(3 + step, 70 + 0.2 * step)
        marks.extend(session.pending_marks)
        if session.finished:
            finished = 3 + step
            break
    assert finished is not None
    # A straight rise is still watched long enough to see it is not about to steepen.
    assert finished - 3 >= 45
    assert session.heat is False
    assert marks == ["Initial Heating", "Linear"]
    result = session.result
    assert result["found"] is True
    assert result["stopped"] is False
    assert result["slope_f_per_sec"] == pytest.approx(0.2, abs=0.01)
    assert result["slope_f_per_min"] == pytest.approx(12.0, abs=0.5)
    assert result["tau_seconds"] is None
    assert result["coast_rise_f"] is None
    assert result["start_temp_f"] == 70.0
    assert result["linear_temp_f"] == pytest.approx(70 + 0.2 * (finished - 3), abs=0.2)
    assert result["seconds"] == pytest.approx(finished - 3, abs=0.1)
    held = result["slope_f_per_sec"]
    session.observe(finished + 5, 90)
    assert session.heat is False
    assert session.result["slope_f_per_sec"] == held


def test_a_shallow_start_stays_in_heating_until_the_rise_steepens():
    session = InitialHeatingSession()
    session.observe(3, 70)
    finished = None
    for step in range(1, 190):
        age = float(step)
        if age < 40:
            temp = 70 + 0.02 * age
        else:
            risen = age - 40
            rate = 0.25
            tau = 25.0
            temp = 70 + 0.02 * 40 + rate * (risen - tau * (1 - math.exp(-risen / tau)))
        session.observe(3 + step, temp)
        if age <= 40:
            assert session.finished is False
            assert session.heat is True
        if session.finished:
            finished = 3 + step
            break
    assert finished is not None
    assert finished - 3 > 40
    assert session.result["found"] is True
    assert session.result["slope_f_per_sec"] > 0.10
    assert session.pending_marks == ["Linear"]


def test_initial_heating_fits_the_soak_and_the_coast_it_leaves():
    session = InitialHeatingSession()
    session.observe(3, 70)
    rate = 0.25
    tau = 30.0
    for step in range(1, 180):
        elapsed = float(step)
        rise = rate * (elapsed - tau * (1 - math.exp(-elapsed / tau)))
        session.observe(3 + step, 70 + rise)
        if session.finished:
            break
    result = session.result
    assert result["found"] is True
    assert result["approach_f_per_sec"] == pytest.approx(rate, rel=0.15)
    assert result["tau_seconds"] == pytest.approx(tau, rel=0.25)
    assert result["coast_rise_f"] == pytest.approx(
        rate * tau * (1 - math.exp(-result["seconds"] / tau)),
        rel=0.2,
    )


def test_the_cap_ends_without_a_linear_mark_when_the_rise_keeps_bending():
    session = InitialHeatingSession()
    session.observe(3, 70)
    assert session.pending_marks == ["Initial Heating"]
    marks = []
    finished = None
    for step in range(1, 200):
        elapsed = 3 + step
        bent = elapsed - 3
        session.observe(elapsed, 70 + 0.01 * bent * bent)
        marks.extend(session.pending_marks)
        if session.finished:
            finished = elapsed
            break
    assert finished == 183
    assert "Linear" not in marks
    assert session.result["found"] is False
    assert session.result["stopped"] is False
    assert session.result["seconds"] == pytest.approx(180, abs=0.1)
    assert session.result["linear_temp_f"] is None
    assert session.heat is False


def test_abort_keeps_whatever_was_measured():
    settled = InitialHeatingSession()
    settled.observe(1, 70)
    settled.abort(2, 70.4)
    assert settled.result["stopped"] is True
    assert settled.result["found"] is False
    assert settled.result["slope_f_per_sec"] is None
    assert settled.heat is False
    settled.abort(3, 80)
    assert settled.result["start_temp_f"] == 70.4

    heating = InitialHeatingSession()
    heating.observe(3, 70)
    heating.observe(8, 71)
    heating.abort(8, 71)
    assert heating.result["found"] is False
    assert heating.result["stopped"] is True
    assert heating.result["slope_f_per_sec"] == pytest.approx(0.2, abs=0.02)
    assert heating.result["rise_f"] == pytest.approx(1.0, abs=0.05)
    assert heating.pending_marks == []


def test_initial_heating_leaves_the_model_and_holds_the_result(store):
    source = _Source([70.0, 62.0])
    model = {"linear_rate_f_per_sec": 0.1, "coast_rise_f": 3.0}
    store.save_settings(
        replace(
            store.get_settings(),
            learned_coast_f=3,
            learned_coast_seconds=8,
            setup_complete=True,
            thermal_model=model,
        )
    )
    controller = Controller(store, source)
    before = store.get_settings().thermal_model
    started = controller.start({"kind": "test", "test": "initial_heating"})
    assert started["run_kind"] == "test"
    assert started["test"]["phase"] == "settle"
    assert started["test"]["review"] is False
    assert source.heaters == [False, False]

    with pytest.raises(ControllerError, match="paused"):
        controller.pause()

    for step in range(1, 120):
        temp = 70.0 if step <= 3 else 70.0 + 0.2 * (step - 3)
        source.temps = [temp, 62.0]
        controller.tick(1)
        if controller.status()["test"]["review"]:
            break
    status = controller.status()
    assert status["mode"] == "idle"
    assert status["test"]["review"] is True
    assert status["test"]["found"] is True
    assert status["test"]["slope_f_per_sec"] == pytest.approx(0.2, abs=0.02)
    assert source.heaters == [False, False]
    assert store.get_settings().thermal_model == before
    assert store.get_settings().learned_coast_f == 3
    run = store.get_run(status["test"]["run_id"])
    assert [mark["name"] for mark in run["marks"]] == ["Start", "Initial Heating", "Linear"]
    assert any(item["event"] == "test" and "slope" in item["message"] for item in run["logs"])
    listed = controller.runs()
    assert listed["stats"]["total_runs"] == 0
    assert listed["stats"]["calibration_runs"] == 0
    assert listed["runs"][0]["kind"] == "test"

    controller._refresh_display()
    assert controller._display.lines[0] == "Init Heat done"
    assert controller._display.lines[3] == "STOP end"
    assert indicator_leds("idle", emergency=False, review=True) == (False, True)

    with pytest.raises(ControllerError, match="End the test"):
        controller.start()
    cleared = controller.end_test()
    assert cleared["test"] is None
    controller._refresh_display()
    assert controller._display.lines[0] == "Idle:"


def test_stopping_during_settle_holds_an_early_result(store):
    source = _Source([70.0, 62.0])
    controller = Controller(store, source)
    before = store.get_settings().thermal_model
    controller.start({"kind": "test", "test": "initial_heating"})
    controller.tick(1)
    assert source.heaters == [False, False]
    stopped = controller.stop()
    assert stopped["test"]["review"] is True
    assert stopped["test"]["found"] is False
    assert stopped["test"]["stopped"] is True
    assert source.heaters == [False, False]
    assert store.get_settings().thermal_model == before
    names = [mark["name"] for mark in store.get_run(stopped["test"]["run_id"])["marks"]]
    assert names == ["Start"]
    controller._panel = _Panel((0, 1, 0, 0))
    controller._apply_panel()
    assert controller.status()["test"] is None


def test_the_menu_starts_initial_heating(controller):
    controller._panel = _Panel((0, 0, 1, 0))
    controller._apply_panel()
    controller._panel = _Panel((0, 0, 0, 2))
    controller._apply_panel()
    assert controller._display.lines[3] == "> Test Suite"
    controller._panel = _Panel((1, 0, 0, 0))
    controller._apply_panel()
    assert controller._display.lines[1] == "> Initial Heating"
    controller._panel = _Panel((1, 0, 0, 0))
    controller._apply_panel()
    assert controller._display.lines[0] == "Initial Heating?"
    controller._panel = _Panel((1, 0, 0, 0))
    controller._apply_panel()
    status = controller.status()
    assert status["mode"] == "running"
    assert status["run_kind"] == "test"
    assert controller._menu is None
    assert controller._display.lines[0].startswith("Settle:")
    assert controller._display.lines[1] == "Heaters off"


def test_a_test_does_not_require_a_saved_calibration(store):
    controller = Controller(store, _Source([70.0, 62.0]))
    store.save_settings(store.get_settings())
    # The fresh store has no coast. A normal run stays locked. The test does not.
    assert store.get_settings().learned_coast_f is None
    with pytest.raises(ControllerError, match="Calibrate"):
        controller.start()
    started = controller.start({"kind": "test", "test": "initial_heating"})
    assert started["run_kind"] == "test"


def test_the_test_api_refuses_a_bad_body_and_ends_the_review(client):
    assert client.post("/api/runs/start", json={"kind": "test"}).status_code == 400
    assert client.post(
        "/api/runs/start",
        json={"kind": "test", "test": "initial_heating", "target_temp_f": 150},
    ).status_code == 400
    started = client.post("/api/runs/start", json={"kind": "test", "test": "initial_heating"})
    assert started.status_code == 200
    assert started.get_json()["test"]["phase"] == "settle"
    assert client.post("/api/tests/end").status_code == 409
    stopped = client.post("/api/runs/stop")
    assert stopped.status_code == 200
    assert stopped.get_json()["test"]["review"] is True
    assert client.post("/api/runs/start", json={}).status_code == 409
    ended = client.post("/api/tests/end")
    assert ended.status_code == 200
    assert ended.get_json()["test"] is None
