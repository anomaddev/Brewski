import pytest

from brewski.controller import COOLDOWN_STABLE_SECONDS, Controller
from brewski.store import merge_settings


class StaticSource:
    """A source whose temperatures only change when a test assigns them."""

    name = "static"

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


def test_deadband_holds_heater_state_near_the_target(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    assert source.heaters[:2] == [True, True]

    source.temps = [164.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]

    source.temps = [165.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    source.temps = [164.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    source.temps = [163.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]


def test_a_disabled_heater_stays_off_and_the_second_probe_can_control(store):
    current = store.get_settings()
    heaters = [channel.to_dict() for channel in current.heaters]
    sensors = [channel.to_dict() for channel in current.sensors]
    heaters[0]["enabled"] = False
    sensors[0]["enabled"] = False
    store.save_settings(merge_settings(current, {"heaters": heaters, "sensors": sensors}))

    source = StaticSource([100.0, 200.0])
    controller = Controller(store, source)
    controller.start()
    assert source.heaters == [False, False]

    source.temps = [200.0, 100.0]
    controller.tick(1)
    assert source.heaters == [False, True]
    status = controller.status()
    assert status["heater_names"] == ["Heater 2"]
    assert status["sensor_names"] == ["Column"]
    assert status["heaters_on"] == [True]
    assert status["temps_f"] == [100.0]


def test_control_follows_the_first_sensor_only(store):
    source = StaticSource([165.0, 70.0])
    controller = Controller(store, source)
    controller.start()
    assert source.heaters[:2] == [False, False]

    source.temps = [100.0, 200.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]


def test_pause_freezes_the_clock_and_stops_sampling(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(3)
    paused = controller.pause()
    samples_at_pause = len(controller.run(paused["run_id"])["samples"])
    controller.tick(10)
    status = controller.status()
    assert status["mode"] == "paused"
    assert status["elapsed_seconds"] == paused["elapsed_seconds"]
    assert status["heaters_on"] == [False, False]
    assert len(controller.run(paused["run_id"])["samples"]) == samples_at_pause


def test_restart_marks_an_open_run_stopped(store):
    source = StaticSource([100.0, 90.0])
    first = Controller(store, source)
    started = first.start()
    Controller(store, StaticSource([100.0, 90.0]))
    saved = store.get_run(started["run_id"])
    assert saved["status"] == "stopped"
    assert saved["ended_at"]


def test_a_run_that_never_reaches_target_has_no_cooldown(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    source.temps = [140.0, 110.0]
    controller.tick(2)
    status = controller.stop()
    assert status["mode"] == "idle"
    assert status["cooldown_run_id"] is None
    assert controller.runs()["stats"]["average_cooldown_seconds"] is None


def test_cooldown_runs_from_target_until_the_temperature_settles(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    source.temps = [165.0, 150.0]
    controller.tick(30)
    assert controller.status()["mode"] == "running"

    run_id = controller.stop()["cooldown_run_id"]
    assert controller.status()["mode"] == "cooling"
    assert controller.status()["elapsed_seconds"] == pytest.approx(30)

    source.temps = [140.0, 120.0]
    controller.tick(60)
    assert controller.status()["mode"] == "cooling"

    source.temps = [110.0, 100.0]
    controller.tick(COOLDOWN_STABLE_SECONDS)
    assert controller.status()["mode"] == "cooling"
    controller.tick(1)
    assert controller.status()["mode"] == "idle"

    saved = store.get_run(run_id)
    assert saved["cooldown_status"] == "complete"
    assert saved["cooldown_seconds"] == pytest.approx(30 + 60 + COOLDOWN_STABLE_SECONDS + 1)
    assert saved["cooldown_baseline_f"] == 165
    assert any(sample["phase"] == "cooldown" for sample in saved["samples"])
    assert controller.runs()["stats"]["average_cooldown_seconds"] == pytest.approx(
        30 + 60 + COOLDOWN_STABLE_SECONDS + 1
    )


def test_a_swing_wider_than_2f_keeps_the_cooldown_open(store):
    source = StaticSource([165.0, 150.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(1)
    controller.stop()
    source.temps = [100.0, 90.0]
    controller.tick(30)
    source.temps = [103.0, 90.0]
    controller.tick(1)
    source.temps = [100.0, 90.0]
    controller.tick(COOLDOWN_STABLE_SECONDS)
    assert controller.status()["mode"] == "cooling"
    controller.tick(1)
    assert controller.status()["mode"] == "idle"


def test_starting_again_leaves_an_unfinished_cooldown_out_of_the_average(store):
    source = StaticSource([165.0, 150.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(1)
    run_id = controller.stop()["cooldown_run_id"]
    controller.tick(3)
    controller.start()
    saved = store.get_run(run_id)
    assert saved["cooldown_status"] == "abandoned"
    assert controller.status()["mode"] == "running"
    assert controller.runs()["stats"]["average_cooldown_seconds"] is None


def test_cooldown_resumes_after_a_restart(store):
    source = StaticSource([165.0, 150.0])
    first = Controller(store, source)
    first.start()
    first.tick(10)
    run_id = first.stop()["cooldown_run_id"]
    first.tick(4)

    resumed = Controller(store, StaticSource([140.0, 110.0]))
    assert resumed.status()["mode"] == "cooling"
    assert resumed.status()["cooldown_run_id"] == run_id
    assert resumed.status()["elapsed_seconds"] == pytest.approx(14)
