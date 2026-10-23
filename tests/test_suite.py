"""Initial Heating: the first live test in the Software menu."""

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


def _profile(age: float) -> float:
    """Soak at 0.1°F/s, then 0.4°F/s, then a coast that peaks and sits."""

    if age <= 60:
        return 100.0 + 0.1 * age
    if age <= 180:
        return 106.0 + 0.4 * (age - 60)
    coast = age - 180
    if coast <= 60:
        return 154.0 + 0.2 * coast
    if coast <= 120:
        return 166.0 + 0.05 * (coast - 60)
    return 169.0


def test_three_minutes_on_and_three_minutes_off_records_the_curve():
    session = InitialHeatingSession()
    marks = []
    for stamp in (0, 1, 2):
        session.observe(stamp, 100)
        marks.extend(session.pending_marks)
        assert session.heat is False
        assert session.phase == "settle"
    session.observe(3, 100)
    marks.extend(session.pending_marks)
    assert session.heat is True
    assert session.phase == "heating"
    assert session.pending_marks == ["Initial Heating"]

    finished = None
    heaters_off_at = None
    for step in range(1, 400):
        elapsed = 3 + step
        session.observe(elapsed, _profile(float(step)))
        marks.extend(session.pending_marks)
        if session.phase == "heating":
            assert session.heat is True
        if "Heaters Off" in session.pending_marks:
            heaters_off_at = elapsed
            assert session.heat is False
            assert session.phase == "coast"
        if session.phase == "coast":
            assert session.heat is False
        if session.finished:
            finished = elapsed
            break
    assert heaters_off_at == 183
    assert finished == 363
    assert session.heat is False
    assert marks == ["Initial Heating", "Heaters Off"]
    result = session.result
    assert result["found"] is True
    assert result["stopped"] is False
    assert result["on_seconds"] == pytest.approx(180.0, abs=0.1)
    assert result["off_seconds"] == pytest.approx(180.0, abs=0.1)
    assert result["start_temp_f"] == pytest.approx(100.0, abs=0.1)
    assert result["off_temp_f"] == pytest.approx(154.0, abs=0.2)
    assert result["end_temp_f"] == pytest.approx(169.0, abs=0.2)
    assert "soak_f_per_min" not in result
    session.observe(finished + 5, 200)
    assert session.heat is False
    assert session.result["on_seconds"] == result["on_seconds"]


def test_stopping_during_heat_keeps_the_samples_without_a_slope():
    session = InitialHeatingSession()
    session.observe(3, 100)
    session.observe(33, 103)
    session.abort(33, 103)
    result = session.result
    assert result["found"] is False
    assert result["stopped"] is True
    assert result["on_seconds"] == pytest.approx(30.0, abs=0.1)
    assert result["off_seconds"] is None
    assert result["start_temp_f"] == pytest.approx(100.0, abs=0.1)
    assert result["end_temp_f"] == pytest.approx(103.0, abs=0.1)
    assert session.heat is False
    assert session.pending_marks == []


def test_abort_keeps_whatever_was_measured():
    settled = InitialHeatingSession()
    settled.observe(1, 70)
    settled.abort(2, 70.4)
    assert settled.result["stopped"] is True
    assert settled.result["found"] is False
    assert settled.result["on_seconds"] is None
    assert settled.result["end_temp_f"] == 70.4
    assert settled.heat is False
    settled.abort(3, 80)
    assert settled.result["end_temp_f"] == 70.4

    heating = InitialHeatingSession()
    heating.observe(3, 70)
    heating.observe(13, 72)
    heating.abort(13, 72)
    assert heating.result["found"] is False
    assert heating.result["stopped"] is True
    assert heating.result["on_seconds"] == pytest.approx(10.0, abs=0.1)
    assert heating.result["off_seconds"] is None
    assert heating.result["end_temp_f"] == pytest.approx(72.0, abs=0.05)
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

    saw_heat = False
    saw_coast = False
    for step in range(1, 400):
        source.temps = [75.0, 62.0]
        controller.tick(1)
        test = controller.status()["test"]
        if test and test["phase"] == "heating":
            assert source.heaters == [True, True]
            saw_heat = True
        if test and test["phase"] == "coast":
            assert source.heaters == [False, False]
            saw_coast = True
        if test and test["review"]:
            break
    assert saw_heat is True
    assert saw_coast is True
    status = controller.status()
    assert status["mode"] == "idle"
    assert status["test"]["review"] is True
    assert status["test"]["found"] is True
    assert status["test"]["on_seconds"] == pytest.approx(180.0, abs=0.1)
    assert status["test"]["off_seconds"] == pytest.approx(180.0, abs=0.1)
    assert source.heaters == [False, False]
    assert store.get_settings().thermal_model == before
    assert store.get_settings().learned_coast_f == 3
    run = store.get_run(status["test"]["run_id"])
    assert [mark["name"] for mark in run["marks"]] == ["Start", "Initial Heating", "Heaters Off"]
    assert any(item["event"] == "test" and "recorded" in item["message"] for item in run["logs"])
    listed = controller.runs()
    assert listed["stats"]["total_runs"] == 0
    assert listed["stats"]["calibration_runs"] == 0
    assert listed["runs"][0]["kind"] == "test"

    controller._refresh_display()
    assert controller._display.lines[0] == "Recording saved"
    assert controller._display.lines[1] == "On:             3:00"
    assert controller._display.lines[2] == "Off:            3:00"
    assert controller._display.lines[3] == "STOP end"
    assert indicator_leds("idle", emergency=False, review=True) == (False, True)

    with pytest.raises(ControllerError, match="End the test"):
        controller.start()
    cleared = controller.end_test()
    assert cleared["test"] is None
    controller._refresh_display()
    assert controller._display.lines[0] == "Hold:"


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
    controller._panel = _Panel((0, 0, 0, 3))
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
