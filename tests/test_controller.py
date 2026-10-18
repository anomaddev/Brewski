import pytest

from brewski.controller import (
    CALIBRATION_ABOVE_F,
    COOLDOWN_STABLE_SECONDS,
    Controller,
    ControllerError,
    heater_should_be_on,
    reheat_temperature,
    sustain_pulse_seconds,
)
from brewski.sources.dummy import DummySource
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


@pytest.fixture(autouse=True)
def _remember_a_coast(store, request):
    if request.node.get_closest_marker("fresh"):
        return
    store.save_settings(
        merge_settings(
            store.get_settings(),
            {"learned_coast_f": 3, "learned_coast_seconds": 8, "setup_complete": True},
        )
    )


@pytest.mark.fresh
def test_a_run_requires_calibration(store):
    controller = Controller(store, StaticSource([70.0, 62.0]))
    with pytest.raises(ControllerError, match="Calibrate"):
        controller.start()
    started = controller.start({"kind": "calibration"})
    assert started["mode"] == "running"
    assert started["run_kind"] == "calibration"


def test_anticipation_turns_the_element_off_before_the_setpoint():
    # Target 165°F. The default 3°F cutoff opens the SSR at 162°F, and the
    # 2°F band does not close it again until the probe falls to 160°F.
    assert heater_should_be_on(161.9, 165, True, early_cutoff_f=3) is True
    assert heater_should_be_on(162.0, 165, True, early_cutoff_f=3) is False
    # Still short of the setpoint. Residual heat is expected to finish the rise.
    assert heater_should_be_on(164.0, 165, False, early_cutoff_f=3) is False
    assert heater_should_be_on(160.1, 165, False, early_cutoff_f=3) is False
    assert heater_should_be_on(160.0, 165, False, early_cutoff_f=3) is True
    # Inside the band, keep whichever state the SSR is already in.
    assert heater_should_be_on(161.0, 165, True, early_cutoff_f=3) is True
    assert heater_should_be_on(161.0, 165, False, early_cutoff_f=3) is False


def test_deadband_holds_heater_state_around_the_early_cutoff(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    assert source.heaters[:2] == [True, True]

    # Still short of the cutoff. Target is 165°F, so power stays on below 162°F.
    source.temps = [161.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]

    # The cutoff is reached before the setpoint. The SSR opens.
    source.temps = [162.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    # Inside the band, and still below the setpoint, the SSR stays open.
    source.temps = [161.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    source.temps = [160.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]


def test_zero_early_cutoff_waits_for_the_setpoint(store):
    store.save_settings(merge_settings(store.get_settings(), {"early_cutoff_f": 0}))
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()

    source.temps = [164.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]

    source.temps = [165.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]


def test_a_larger_early_cutoff_opens_the_ssr_sooner(store):
    store.save_settings(merge_settings(store.get_settings(), {"early_cutoff_f": 5}))
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()

    source.temps = [160.0, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]


def test_heaters_stay_off_for_the_minimum_before_they_can_turn_on(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()

    source.temps = [165.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    source.temps = [100.0, 90.0]
    controller.tick(0.4)
    assert source.heaters[:2] == [False, False]
    controller.tick(0.6)
    assert source.heaters[:2] == [True, True]


def test_minimum_off_time_follows_the_setting(store):
    current = store.get_settings()
    store.save_settings(merge_settings(current, {"heater_min_off_seconds": 0.5}))
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()

    source.temps = [165.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    source.temps = [100.0, 90.0]
    controller.tick(0.4)
    assert source.heaters[:2] == [False, False]
    controller.tick(0.1)
    assert source.heaters[:2] == [True, True]


def test_resume_does_not_wait_out_the_minimum_off_time(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    controller.pause()
    resumed = controller.resume()
    assert resumed["heaters_on"] == [True, True]
    assert source.heaters[:2] == [True, True]


def _open_soaked_calibration(controller, source, target=165.0):
    controller.start({"kind": "calibration", "charge_gallons": 5, "target_temp_f": target})
    controller.tick(30)
    source.temps = [target, 150.0]
    controller.tick(1)
    opened = target + CALIBRATION_ABOVE_F
    source.temps = [opened, 150.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    return opened


def test_calibration_records_the_crest_and_then_the_cooling_rate(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    opened = _open_soaked_calibration(controller, source)
    assert controller.status()["calibration_step"] == "coasting"
    assert controller._panel_lines(store.get_settings())[0].startswith("Coasting:")

    peak = opened + 15.0
    for _ in range(15):
        source.temps = [peak, 150.0]
        controller.tick(1)
    assert controller.status()["mode"] == "running"
    assert store.get_settings().learned_coast_f == 3

    for _ in range(25):
        source.temps = [peak - 0.6, 150.0]
        controller.tick(1)
    assert controller.status()["mode"] == "running"
    assert controller.status()["calibration_step"] == "falling"
    settings = store.get_settings()
    assert settings.learned_coast_f == pytest.approx(15.0, abs=0.15)
    assert controller._panel_lines(settings)[0].startswith("Falling:")

    temp = peak - 0.6
    for _ in range(120):
        temp -= 2.0 / 60.0
        source.temps = [temp, 150.0]
        controller.tick(1)
        if controller.status()["mode"] != "running":
            break

    assert controller.status()["mode"] == "cooling"
    assert source.heaters[:2] == [False, False]
    settings = store.get_settings()
    assert settings.learned_coast_f == pytest.approx(15.0, abs=0.15)
    assert settings.learned_cooldown_f_per_min == pytest.approx(2.0, abs=0.4)
    assert settings.early_cutoff_f == pytest.approx(settings.learned_coast_f)
    assert settings.charge_gallons == 5
    listed = controller.runs()
    assert listed["stats"]["total_runs"] == 0
    assert listed["stats"]["calibration_runs"] == 1
    assert listed["runs"][0]["kind"] == "calibration"
    assert listed["runs"][0]["status"] == "stopped"
    assert listed["runs"][0]["calibration_settled"] is True
    assert listed["runs"][0]["coast_f"] == pytest.approx(settings.learned_coast_f)
    assert listed["runs"][0]["cooldown_f_per_min"] == pytest.approx(
        settings.learned_cooldown_f_per_min
    )
    assert listed["runs"][0]["charge_gallons"] == 5
    saved = controller._panel_lines(settings)
    assert saved[0].startswith("Cooling:")
    assert "F/min" in saved[1]


def test_a_slow_climb_does_not_end_the_coast(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    opened = _open_soaked_calibration(controller, source)
    for step in range(1, 41):
        source.temps = [opened + 0.05 * step, 150.0]
        controller.tick(1)
    assert controller.status()["mode"] == "running"
    assert controller.status()["calibration_step"] == "coasting"
    assert store.get_settings().learned_coast_f == 3


def test_a_cooling_rate_pulses_near_the_target_instead_of_the_deadband(store):
    store.save_settings(
        merge_settings(
            store.get_settings(),
            {
                "learned_coast_f": 15,
                "learned_coast_seconds": 90,
                "learned_cooldown_f_per_min": 2,
                "early_cutoff_f": 15,
                "target_temp_f": 165,
            },
        )
    )
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(2)
    source.temps = [150.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    controller.tick(1)

    source.temps = [164.5, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    duration = sustain_pulse_seconds(15, 90)
    reheat = reheat_temperature(165, 2, duration)
    assert reheat == pytest.approx(163.8, abs=0.1)
    source.temps = [reheat, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]

    source.temps = [164.5, 90.0]
    controller.tick(duration - 0.5)
    assert source.heaters[:2] == [True, True]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]


def test_calibration_keeps_running_while_the_average_is_still_changing(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration", "target_temp_f": 165})
    controller.tick(30)
    source.temps = [165.0 + CALIBRATION_ABOVE_F, 150.0]
    controller.tick(1)
    for step in range(1, 16):
        source.temps = [166.0 + step * 0.5, 150.0]
        controller.tick(1)
    assert controller.status()["mode"] == "running"
    assert store.get_settings().learned_coast_f == 3


def test_calibration_uses_the_still_size_from_setup(store):
    store.save_settings(merge_settings(store.get_settings(), {"charge_gallons": 6}))
    controller = Controller(store, StaticSource([100.0, 90.0]))
    started = controller.start({"kind": "calibration"})
    assert started["charge_gallons"] == 6
    assert store.get_run(started["run_id"])["charge_gallons"] == 6


def test_a_smaller_charge_scales_the_remembered_coast(store):
    current = store.get_settings()
    store.save_settings(
        merge_settings(
            current,
            {"learned_coast_f": 4, "learned_coast_seconds": 12, "charge_gallons": 8},
        )
    )
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration", "charge_gallons": 4})
    assert store.get_settings().early_cutoff_f == pytest.approx(8)


def test_a_smaller_charge_scales_the_cooling_rate(store):
    current = store.get_settings()
    store.save_settings(
        merge_settings(
            current,
            {
                "learned_coast_f": 4,
                "learned_coast_seconds": 12,
                "learned_cooldown_f_per_min": 2,
                "charge_gallons": 8,
            },
        )
    )
    controller = Controller(store, StaticSource([100.0, 90.0]))
    controller.start({"kind": "calibration", "charge_gallons": 4})
    settings = store.get_settings()
    assert settings.early_cutoff_f == pytest.approx(8)
    assert settings.learned_cooldown_f_per_min == pytest.approx(4)


def test_a_short_pulse_does_not_teach_the_coast(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration"})
    source.temps = [165.0 + CALIBRATION_ABOVE_F, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    source.temps = [164.0, 90.0]
    controller.tick(10)
    assert store.get_settings().early_cutoff_f == 3
    assert controller.status()["coast_f"] is None


def test_residual_heat_keeps_rising_after_the_ssr_opens(store):
    source = DummySource(noise=False, initial_temps=(158.0, 150.0))
    controller = Controller(store, source)
    controller.start()
    assert source.heaters == [True, True]

    turned_off_at = None
    for _ in range(40):
        controller.tick(1)
        if not any(source.heaters):
            turned_off_at = source.read_temperatures(1)[0]
            break

    assert turned_off_at is not None
    assert turned_off_at < 165
    controller.tick(3)
    assert source.read_temperatures(1)[0] > turned_off_at
    assert source.heaters == [False, False]


def test_heaters_stay_on_for_the_minimum_before_they_can_turn_off(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()

    source.temps = [165.0, 90.0]
    controller.tick(0.4)
    assert source.heaters[:2] == [True, True]

    controller.tick(0.6)
    assert source.heaters[:2] == [False, False]


def test_minimum_on_time_follows_the_setting(store):
    current = store.get_settings()
    store.save_settings(merge_settings(current, {"heater_min_on_seconds": 0.5}))
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()

    source.temps = [165.0, 90.0]
    controller.tick(0.4)
    assert source.heaters[:2] == [True, True]
    controller.tick(0.1)
    assert source.heaters[:2] == [False, False]


def test_pause_turns_heaters_off_before_the_minimum(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    paused = controller.pause()
    assert paused["heaters_on"] == [False, False]
    assert source.heaters[:2] == [False, False]


def test_element_hours_count_powered_time_and_survive_reset(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(10)
    assert controller.status()["heater_life_seconds"] == pytest.approx([10, 10])
    controller.pause()
    controller.tick(4)
    assert controller.status()["heater_life_seconds"] == pytest.approx([10, 10])
    controller.resume()
    controller.tick(2)
    controller.stop()
    assert store.heater_life_seconds() == pytest.approx([12, 12])

    with pytest.raises(ControllerError) as unknown:
        controller.replace_heater(2)
    assert unknown.value.status == 404
    status = controller.replace_heater(0)
    assert status["heater_life_seconds"] == pytest.approx([0, 12])
    controller.reset()
    assert controller.status()["heater_life_seconds"] == pytest.approx([0, 12])


def test_a_disabled_heater_does_not_collect_hours(store):
    current = store.get_settings()
    heaters = [channel.to_dict() for channel in current.heaters]
    heaters[0]["enabled"] = False
    store.save_settings(merge_settings(current, {"heaters": heaters}))
    controller = Controller(store, StaticSource([100.0, 90.0]))
    controller.start()
    controller.tick(5)
    controller.stop()
    assert store.heater_life_seconds() == pytest.approx([0, 5])
    assert controller.status()["heater_indexes"] == [1]


def test_calibration_counts_toward_element_hours_but_not_run_totals(store):
    controller = Controller(store, StaticSource([100.0, 90.0]))
    controller.start({"kind": "calibration", "charge_gallons": 5})
    controller.tick(8)
    controller.stop()
    history = controller.runs()
    assert history["stats"]["total_runs"] == 0
    assert history["stats"]["heater_on_seconds"] == pytest.approx([0, 0])
    assert history["stats"]["heater_life_seconds"] == pytest.approx([8, 8])


def test_a_disabled_heater_stays_off_and_the_second_probe_can_control(store):
    current = store.get_settings()
    heaters = [channel.to_dict() for channel in current.heaters]
    sensors = [channel.to_dict() for channel in current.sensors]
    heaters[0]["enabled"] = False
    sensors[0]["enabled"] = False
    sensors[1]["enabled"] = True
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
