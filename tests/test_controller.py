import math
from dataclasses import replace

import pytest

from brewski.controller import (
    CALIBRATION_TEMP_F,
    PREDICT_MAX_HORIZON_SECONDS,
    Controller,
    ControllerError,
    coast_tau_seconds,
    heater_should_be_on,
    predicted_coast_f,
    projected_linear_rate,
    reheat_temperature,
    seconds_along_coast,
    seconds_to_cooldown,
    seconds_to_target,
    sheath_soak,
    slope_f_per_sec,
    sustain_pulse_seconds,
    tune_coast,
)
from brewski.sources.dummy import DummySource
from brewski.store import merge_settings


def _soak_sheath(controller):
    """Mark the elements hot without adding rise samples.

    A following one-tick jump is not long enough to become a slope, so the
    shutoff stays on the learned coast.
    """

    controller._heater_on_for = [10_000.0, 10_000.0]


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
    _soak_sheath(controller)
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


def test_a_slowing_rise_has_a_time_constant_and_the_forecast_follows_it():
    # 0.5°F/s decaying with tau 10s. Remaining degrees are rate * tau.
    samples = []
    for second in range(25):
        samples.append((float(second), 100.0 + 5.0 * (1.0 - math.exp(-second / 10.0))))
    tau = coast_tau_seconds(samples)
    assert tau == pytest.approx(10.0, rel=0.15)
    assert coast_tau_seconds([(0, 100), (8, 108), (16, 116)]) is None
    assert seconds_along_coast(162, 165, 0.5, 10) == pytest.approx(-10 * math.log(1 - 3 / 5))
    assert seconds_along_coast(160, 165, 0.5, 10) is None
    assert sheath_soak(float("inf"), 0, 8) == 0
    assert sheath_soak(float("inf"), 8, 8) == pytest.approx(1 - math.exp(-1))
    assert projected_linear_rate(0.2, 0.5) == pytest.approx(0.4)
    assert projected_linear_rate(0.2, 0.1) is None
    assert projected_linear_rate(0.2, 0.95) == pytest.approx(0.2)


def test_the_landing_miss_moves_the_coast_the_prediction_will_use():
    # On the floor, 2°F past 120°F adds 2°F. The same slow rate still predicts it.
    coast, seconds = tune_coast(5, 8, 5, 0.1, 2, 20, tau_seconds=8)
    assert coast == pytest.approx(7)
    assert predicted_coast_f(coast, seconds, 0.1) == pytest.approx(7)
    # Short of 120°F subtracts.
    short, short_seconds = tune_coast(5, 8, 5, 0.1, -1.5, 20, tau_seconds=8)
    assert short == pytest.approx(3.5)
    assert predicted_coast_f(short, short_seconds, 0.1) == pytest.approx(3.5)
    # No fit and no rate yet: the overshoot is the first coast, timed to the crest.
    first, first_seconds = tune_coast(None, None, 0, None, 12, 40, tau_seconds=None)
    assert first == pytest.approx(12)
    assert first_seconds == pytest.approx(40)
    # A fast rise keeps the next prediction, at that same rate, on the corrected coast.
    fast, fast_seconds = tune_coast(3, 8, 8, 1.0, 2, 10, tau_seconds=4)
    assert predicted_coast_f(fast, fast_seconds, 1.0) == pytest.approx(10, abs=0.02)


def test_a_cold_sheath_waits_longer_to_open_the_ssr(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    source.temps = [162.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]
    _soak_sheath(controller)
    controller.tick(1)
    assert source.heaters[:2] == [False, False]


def test_a_short_window_does_not_move_the_learned_coast():
    # 3°F over 8s implies 0.375°F/s. One fast jump is not a window, so the
    # shutoff stays at 162°F.
    assert slope_f_per_sec([(0, 100), (1, 110), (2, 120)]) is None
    assert predicted_coast_f(3, 8, None) == 3
    samples = [(0.0, 100.0), (3.0, 106.0), (6.0, 112.0), (7.0, 114.0)]
    assert slope_f_per_sec(samples) is None


def test_a_fast_rise_opens_the_ssr_before_the_learned_coast(store):
    # Learned coast is 3°F over 8s (0.375°F/s). A 1°F/s climb scales that to 8°F,
    # so 158°F is already past the shutoff on the way to 165°F.
    source = StaticSource([140.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _soak_sheath(controller)
    for step in range(10):
        source.temps = [140.0 + step, 90.0]
        controller.tick(1)
    assert any(source.heaters)
    source.temps = [158.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    assert controller.status()["seconds_to_target"] is not None
    assert controller.status()["seconds_to_cooldown"] is None


def test_a_flat_slope_does_not_heat_past_the_learned_coast(store):
    # 160°F is the bottom of the 2°F band under a 3°F coast, so the SSR is on.
    # A flat window must not widen that coast and open it early.
    source = StaticSource([160.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _soak_sheath(controller)
    for _ in range(10):
        controller.tick(1)
    assert source.heaters[:2] == [True, True]
    source.temps = [162.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]


def test_time_to_target_includes_the_coast_and_cooldown_waits_for_a_fall():
    # 1°F/s, 3°F coast over 8s. From 154°F the powered gap to a 162°F shutoff
    # is 8°F, then the full 8s tail.
    assert seconds_to_target(154, 165, 3, 1.0, 3, 8) == pytest.approx(16.0)
    assert seconds_to_target(164, 165, 3, 0.5, 3, 8) == pytest.approx(2.0)
    assert seconds_to_target(70, 240, 3, 0.01, 3, 8) == PREDICT_MAX_HORIZON_SECONDS
    falling = [(0.0, 160.0), (5.0, 155.0), (10.0, 150.0), (15.0, 145.0)]
    rate = slope_f_per_sec(falling)
    assert rate is not None and rate < 0
    cool = seconds_to_cooldown(145.0, rate)
    assert cool is not None
    assert 0 < cool <= PREDICT_MAX_HORIZON_SECONDS
    assert seconds_to_cooldown(160.0, None, learned_rate_f_per_min=2.0) == pytest.approx((160 - 90) / (2 / 60))
    assert seconds_to_cooldown(160.0, 0.2) is None


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


def _heating_temp(elapsed: float) -> float:
    """Cold-start parabola for 20s of heat, then a straight 0.4°F/s climb."""

    if elapsed <= 3:
        return 70.0
    heat = elapsed - 3.0
    if heat <= 20:
        return 70.0 + 0.01 * heat * heat
    return 74.0 + 0.4 * (heat - 20.0)


def _drive(controller, source, profile, limit: int, step: str) -> dict:
    for _ in range(limit):
        status = controller.status()
        if status["calibration_step"] == step:
            return status
        source.temps = [profile(status["elapsed_seconds"] + 1), 70.0]
        controller.tick(1)
    raise AssertionError(controller.status()["calibration_step"])


def test_calibration_records_the_parabola_and_then_the_cooling_rate(store):
    source = StaticSource([70.0, 70.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration", "charge_gallons": 5, "target_temp_f": 190})
    assert store.get_settings().target_temp_f == 165
    assert controller.status()["target_temp_f"] == CALIBRATION_TEMP_F
    assert controller.status()["calibration_step"] == "settle"
    assert source.heaters[:2] == [False, False]

    cold = _drive(controller, source, _heating_temp, 10, "cold_start")
    assert cold["elapsed_seconds"] == pytest.approx(3)
    assert source.heaters[:2] == [True, True]
    paused = controller.pause()
    controller.tick(5)
    assert controller.status()["elapsed_seconds"] == paused["elapsed_seconds"]
    assert source.heaters[:2] == [False, False]
    controller.resume()
    assert controller.status()["calibration_step"] == "cold_start"

    _drive(controller, source, _heating_temp, 80, "linear")
    assert controller._panel_lines(store.get_settings())[0].startswith("Heating:")
    coast = _drive(controller, source, _heating_temp, 40, "coast")
    assert source.heaters[:2] == [False, False]
    assert controller._panel_lines(store.get_settings())[0].startswith("Coast:")
    opened = coast["elapsed_seconds"]
    base = _heating_temp(opened)

    def coasting(elapsed: float) -> float:
        age = elapsed - opened
        if age <= 12:
            return base + 0.2 * age
        return base + 0.2 * 12 - 1.2

    momentum = _drive(controller, source, coasting, 140, "momentum")
    assert controller._panel_lines(store.get_settings())[0].startswith("Momentum:")
    settings = store.get_settings()
    assert settings.learned_coast_f == pytest.approx(1.58, abs=0.3)
    assert settings.learned_coast_seconds > 0
    model = settings.thermal_model
    assert model["cold_start_quad"] == pytest.approx(0.01, abs=0.003)
    assert model["linear_rate_f_per_sec"] == pytest.approx(0.4, abs=0.05)
    assert model["coast_quad"] < 0

    fallen = source.temps[0]

    def cooling(elapsed: float) -> float:
        return fallen - (2.0 / 60.0) * (elapsed - momentum["elapsed_seconds"])

    approach = _drive(controller, source, cooling, 150, "approach")
    assert approach["calibration_step"] == "approach"
    settings = store.get_settings()
    assert settings.learned_cooldown_f_per_min == pytest.approx(2.0, abs=0.4)
    assert settings.charge_gallons == 5
    run = store.get_run(approach["run_id"])
    assert [mark["name"] for mark in run["marks"]] == [
        "Start",
        "Cold Start",
        "Heating",
        "Momentum",
        "Stage II",
    ]
    assert all(item["grok"] is None and item["accepted"] is False for item in run["analysis"])

    source.temps = [80.0, 70.0]
    controller.stop()
    listed = controller.runs()
    assert listed["stats"]["total_runs"] == 0
    assert listed["stats"]["calibration_runs"] == 1
    assert listed["runs"][0]["kind"] == "calibration"
    assert listed["runs"][0]["status"] == "stopped"
    assert listed["runs"][0]["calibration_settled"] is False
    assert listed["runs"][0]["coast_f"] == pytest.approx(settings.learned_coast_f)
    assert listed["runs"][0]["charge_gallons"] == 5
    assert controller.status()["mode"] == "idle"


def test_a_slow_climb_does_not_end_the_coast(store):
    source = StaticSource([70.0, 70.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration"})
    coast = _drive(controller, source, _heating_temp, 120, "coast")
    opened = coast["elapsed_seconds"]
    base = source.temps[0]
    for step in range(1, 41):
        source.temps = [base + 0.05 * step, 70.0]
        controller.tick(1)
        assert controller.status()["elapsed_seconds"] > opened
    assert controller.status()["mode"] == "running"
    assert controller.status()["calibration_step"] == "coast"


def test_a_cooling_rate_pulses_near_the_target_instead_of_the_deadband(store):
    store.save_settings(
        merge_settings(
            store.get_settings(),
            {
                "learned_coast_f": 15,
                "learned_coast_seconds": 90,
                "learned_cooldown_f_per_min": 2,
                "target_temp_f": 165,
            },
        )
    )
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _soak_sheath(controller)
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


def test_the_climb_coasts_from_the_learned_cutoff_instead_of_pulsing(store):
    # Coast of 15°F shuts the element off at 150°F on the way to 165°F.
    # The sustain point is about 164°F. Heating through 151–163°F spends
    # the whole coast on top of the target.
    store.save_settings(
        merge_settings(
            store.get_settings(),
            {
                "learned_coast_f": 15,
                "learned_coast_seconds": 90,
                "learned_cooldown_f_per_min": 2,
                "target_temp_f": 165,
            },
        )
    )
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _soak_sheath(controller)
    controller.tick(2)

    source.temps = [150.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    source.temps = [158.0, 90.0]
    controller.tick(5)
    assert source.heaters[:2] == [False, False]

    reheat = reheat_temperature(165, 2, sustain_pulse_seconds(15, 90))
    source.temps = [reheat - 0.5, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]

    # Arrival, then a sag, is what earns the short pulse.
    source.temps = [164.5, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    source.temps = [reheat, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]

    # Falling through the cutoff starts a new climb, which coasts again.
    source.temps = [140.0, 90.0]
    controller.tick(2)
    assert source.heaters[:2] == [True, True]
    _soak_sheath(controller)
    source.temps = [158.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]


def test_calibration_keeps_running_while_the_average_is_still_changing(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration", "target_temp_f": 165})
    _soak_sheath(controller)
    for _ in range(16):
        source.temps = [CALIBRATION_TEMP_F - 3.1, 150.0]
        controller.tick(1)
    source.temps = [CALIBRATION_TEMP_F - 3.0, 150.0]
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
    settings = store.get_settings()
    assert settings.learned_coast_f == pytest.approx(8)
    assert settings.learned_coast_seconds == 12


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
    assert settings.learned_coast_f == pytest.approx(8)
    assert settings.learned_cooldown_f_per_min == pytest.approx(4)


def test_a_short_pulse_does_not_teach_the_coast(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration"})
    source.temps = [CALIBRATION_TEMP_F + 1.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    source.temps = [164.0, 90.0]
    controller.tick(10)
    assert store.get_settings().learned_coast_f == 3
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
    assert history["stats"]["heater_life_seconds"] == pytest.approx([5, 5])


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


def test_a_run_that_stops_at_or_under_90_has_no_cooldown(store):
    source = StaticSource([80.0, 70.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(2)
    status = controller.stop()
    assert status["mode"] == "idle"
    assert status["cooldown_run_id"] is None
    assert controller.runs()["stats"]["average_cooldown_seconds"] is None


def test_a_run_that_never_reaches_target_still_cools_when_it_is_hot(store):
    source = StaticSource([140.0, 110.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(2)
    status = controller.stop()
    assert status["mode"] == "cooling"
    assert status["cooldown_run_id"] is not None
    assert controller.runs()["stats"]["average_cooldown_seconds"] is None


def test_cooldown_runs_until_the_probe_falls_under_90(store):
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

    source.temps = [89.0, 80.0]
    controller.tick(1)
    assert controller.status()["mode"] == "idle"

    saved = store.get_run(run_id)
    assert saved["cooldown_status"] == "complete"
    assert saved["cooldown_seconds"] == pytest.approx(30 + 60 + 1)
    assert saved["cooldown_baseline_f"] == 165
    assert any(sample["phase"] == "cooldown" for sample in saved["samples"])
    assert controller.runs()["stats"]["average_cooldown_seconds"] == pytest.approx(30 + 60 + 1)


def test_staying_hot_keeps_the_cooldown_open(store):
    source = StaticSource([165.0, 150.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(1)
    controller.stop()
    source.temps = [100.0, 90.0]
    controller.tick(30)
    source.temps = [120.0, 90.0]
    controller.tick(30)
    assert controller.status()["mode"] == "cooling"


def test_starting_again_leaves_an_unfinished_cooldown_out_of_the_average(store):
    source = StaticSource([165.0, 150.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(1)
    run_id = controller.stop()["cooldown_run_id"]
    controller.tick(3)
    controller.start()
    saved = store.get_run(run_id)
    assert saved["cooldown_status"] == "interrupted"
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


def test_a_restart_already_under_90_closes_the_cooldown(store):
    source = StaticSource([165.0, 150.0])
    first = Controller(store, source)
    first.start()
    first.tick(1)
    run_id = first.stop()["cooldown_run_id"]

    resumed = Controller(store, StaticSource([80.0, 70.0]))
    assert resumed.status()["mode"] == "idle"
    assert store.get_run(run_id)["cooldown_status"] == "complete"


def test_a_ready_model_shuts_off_when_the_predicted_crest_reaches_the_target(store):
    current = store.get_settings()
    store.save_settings(
        replace(
            current,
            thermal_model={
                "cold_start_seconds": 20,
                "cold_start_quad": 0.01,
                "linear_rate_f_per_sec": 0.4,
                "coast_rise_f": 4,
                "coast_seconds": 20,
                "coast_quad": -4 / 400,
                "cooldown_f_per_min": 1,
            },
        )
    )
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _soak_sheath(controller)
    source.temps = [160.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]
    source.temps = [161.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]


def test_grok_can_replace_a_stage_slope_when_it_agrees(store, monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("BREWSKI_ANALYSIS_INLINE", "1")
    monkeypatch.setattr("brewski.analysis.analyze_rate", lambda samples, kind: 0.41)
    monkeypatch.setattr(
        "brewski.analysis.analyze_model",
        lambda samples, local: {
            "linear_rate_f_per_sec": local["linear_rate_f_per_sec"],
            "coast_rise_f": (local.get("coast_rise_f") or 0) + 0.2,
            "cooldown_f_per_min": local.get("cooldown_f_per_min") or 2,
        },
    )
    source = StaticSource([70.0, 70.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration"})
    _drive(controller, source, _heating_temp, 80, "linear")
    _drive(controller, source, _heating_temp, 40, "coast")
    model = store.get_settings().thermal_model
    assert model["linear_rate_f_per_sec"] == pytest.approx(0.41)
    source.temps = [80.0, 70.0]
    controller.stop()
    revised = store.get_settings().thermal_model
    assert revised["linear_rate_f_per_sec"] == pytest.approx(0.41)
    assert revised["coast_rise_f"] is None
    kinds = {item["kind"]: item["accepted"] for item in controller.runs()["runs"][0]["analysis"]}
    assert kinds["linear"] is True
    assert kinds["full"] is True
