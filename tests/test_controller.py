import math
from dataclasses import replace

import pytest

from brewski.controller import (
    CALIBRATION_TEMP_F,
    LEARN_TEMP_F,
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
    smooth_cooldown_eta,
    sheath_soak,
    slope_f_per_sec,
    sustain_pulse_seconds,
    tune_coast,
)
from brewski.sources.dummy import DummySource
from brewski.store import merge_settings
from brewski.thermal import ThermalModel


def test_status_reports_how_fast_the_control_probe_is_changing(store):
    source = StaticSource([70.0, 62.0])
    controller = Controller(store, source)
    assert controller.status()["rate_f_per_min"] is None
    for step in range(1, 16):
        source.temps = [70.0 + 0.2 * step, 62.0]
        controller.tick(1)
    status = controller.status()
    assert status["rate_f_per_sec"] == pytest.approx(0.2, abs=0.02)
    assert status["rate_f_per_min"] == pytest.approx(12.0, abs=0.5)

    start = source.temps[0]
    for step in range(1, 21):
        source.temps = [start - 0.1 * step, 62.0]
        controller.tick(1)
    falling = controller.status()
    assert falling["rate_f_per_min"] == pytest.approx(-6.0, abs=0.5)


def test_the_live_chart_projects_one_minute_of_cutoff_coast(controller):
    controller._mode = "running"
    controller._run_model = ThermalModel(
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=40,
    )
    controller._heaters = [True, False]
    controller._sheath_slope = 0.4
    controller._live_temps = [100.0, 90.0]
    points = {item["offset_s"]: item["temp_f"] for item in controller.status()["projection"]}
    assert points[0] == pytest.approx(100, abs=0.05)
    assert points[60] == pytest.approx(100 + 8 * (1 - math.exp(-3)), abs=0.1)
    assert 60 in points

    controller._heaters = [False, False]
    controller._predict_clock = 12
    controller._predict_samples = [(0, 110), (4, 109.6), (8, 109.2), (12, 108.8)]
    cooling = {item["offset_s"]: item["temp_f"] for item in controller.status()["projection"]}
    assert cooling[60] < 100


def test_an_open_coast_projects_only_the_rise_still_ahead(controller):
    controller._mode = "running"
    controller._run_model = ThermalModel(
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=40,
    )
    controller._heaters = [False, False]
    controller._coast_open_temp = 100.0
    controller._coast_open_slope = 0.4
    controller._live_temps = [104.0, 90.0]
    # The recent window is still steep. That must not stack another full coast
    # on the 4°F already gained since shutoff.
    controller._predict_clock = 12
    controller._predict_samples = [(0, 100.0), (4, 101.6), (8, 103.2), (12, 104.0)]
    points = {item["offset_s"]: item["temp_f"] for item in controller.status()["projection"]}
    assert points[0] == pytest.approx(104, abs=0.05)
    assert points[60] == pytest.approx(104 + 4 * (1 - math.exp(-3)), abs=0.15)
    assert points[60] < 108


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
    assert seconds_to_cooldown(160.0, None, learned_rate_f_per_min=2.0) == pytest.approx((160 - 100) / (2 / 60))
    assert seconds_to_cooldown(160.0, 0.2) is None
    # A lowered setpoint is measured to that setpoint, not to 100°F.
    assert seconds_to_cooldown(160.0, -0.1, floor_f=150) == pytest.approx(100)
    assert seconds_to_cooldown(160.0, None, learned_rate_f_per_min=2.0, floor_f=150) == pytest.approx(
        (160 - 150) / (2 / 60)
    )


def test_a_dip_while_holding_is_not_a_cooldown(store):
    # The same setpoint, a flat window, and a real downward wiggle. None of
    # those is a cooldown. Lowering the setpoint is, until the probe arrives.
    # After the run, the clock is the fall back under 100°F.
    store.save_settings(
        merge_settings(
            store.get_settings(),
            {"learned_cooldown_f_per_min": 2, "target_temp_f": 165},
        )
    )
    source = StaticSource([165.0, 90.0])
    controller = Controller(store, source)
    controller._mode = "running"
    controller._live_temps = [165.0, 90.0]
    controller._predict_clock = 12
    controller._predict_samples = [(0, 165.0), (4, 165.0), (8, 165.0), (12, 165.0)]
    assert controller.status()["seconds_to_cooldown"] is None

    controller._live_temps = [164.8, 90.0]
    controller._predict_samples = [(0, 165.4), (4, 165.2), (8, 165.0), (12, 164.8)]
    dipped = controller.status()
    assert dipped["seconds_to_target"] is None
    assert dipped["seconds_to_cooldown"] is None

    controller._dial_target = 150
    controller._live_temps = [162.0, 90.0]
    controller._predict_samples = [(0, 165.0), (4, 164.0), (8, 163.0), (12, 162.0)]
    lowered = controller.status()
    assert lowered["target_temp_f"] == 150
    assert lowered["seconds_to_cooldown"] == pytest.approx((162 - 150) / 0.25, abs=0.2)
    assert lowered["seconds_to_cooldown"] < (162 - 100) / 0.25

    controller._dial_target = 170
    raised = controller.status()
    assert raised["seconds_to_cooldown"] is None

    controller._dial_target = 150
    controller._live_temps = [162.0, 90.0]
    waiting = controller.status()
    assert waiting["seconds_to_cooldown"] is not None
    controller._live_temps = [150.4, 90.0]
    controller._predict_samples = [(0, 151.6), (4, 151.2), (8, 150.8), (12, 150.4)]
    assert controller.status()["seconds_to_cooldown"] is None

    controller._mode = "cooling"
    controller._dial_target = None
    controller._live_temps = [150.0, 90.0]
    controller._predict_samples = [(0, 153.0), (4, 152.0), (8, 151.0), (12, 150.0)]
    cooling = controller.status()
    assert cooling["mode"] == "cooling"
    assert cooling["seconds_to_cooldown"] == pytest.approx((150 - 100) / 0.25, abs=0.2)


def test_cooldown_estimate_counts_down_instead_of_following_probe_noise(store):
    # A short window can look like a plunge, then a rise, from ordinary probe
    # noise. The published time stays a countdown either way.
    store.save_settings(
        merge_settings(store.get_settings(), {"learned_cooldown_f_per_min": 1.0, "target_temp_f": 170})
    )
    controller = Controller(store, StaticSource([160.0]))
    controller._mode = "cooling"
    controller._live_temps = [160.0]
    controller._predict_clock = 20.0
    controller._predict_samples = [(5.0, 160.3), (10.0, 160.2), (15.0, 160.1), (20.0, 160.0)]
    first = controller.status()["seconds_to_cooldown"]
    assert first == pytest.approx((160.0 - 100.0) / 0.02, abs=0.2)

    controller._predict_clock = 21.0
    controller._live_temps = [158.0]
    controller._predict_samples = [(6.0, 160.5), (11.0, 160.0), (16.0, 159.0), (21.0, 158.0)]
    plunged = controller.status()["seconds_to_cooldown"]
    assert plunged is not None
    assert abs(plunged - (first - 1.0)) <= 4.0

    controller._predict_clock = 22.0
    controller._live_temps = [158.4]
    controller._predict_samples = [(7.0, 158.0), (12.0, 158.1), (17.0, 158.3), (22.0, 158.4)]
    bounced = controller.status()["seconds_to_cooldown"]
    assert bounced is not None
    assert abs(bounced - (plunged - 1.0)) <= 4.0

    # The same tick must not revise the number a second time.
    again = controller.status()["seconds_to_cooldown"]
    assert again == pytest.approx(bounced, abs=0.05)


def test_smooth_cooldown_eta_counts_down_and_ignores_a_missing_reading():
    assert smooth_cooldown_eta(None, 600.0, 1.0) == pytest.approx(600.0)
    eased = smooth_cooldown_eta(600.0, 120.0, 1.0)
    assert eased is not None
    assert 596.0 <= eased <= 599.0
    assert smooth_cooldown_eta(eased, None, 1.0) == pytest.approx(eased - 1.0)


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


def test_calibration_learns_the_soak_then_opens_under_140(store):
    source = StaticSource([70.0, 70.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration", "charge_gallons": 5, "target_temp_f": 190})
    assert store.get_settings().target_temp_f == 165
    assert controller.status()["target_temp_f"] == LEARN_TEMP_F
    assert controller.status()["calibration_step"] == "settle"
    assert source.heaters[:2] == [False, False]

    initial = _drive(controller, source, _heating_temp, 10, "initial")
    assert initial["elapsed_seconds"] == pytest.approx(3)
    assert initial["target_temp_f"] == LEARN_TEMP_F
    assert source.heaters[:2] == [True, True]
    paused = controller.pause()
    controller.tick(5)
    assert controller.status()["elapsed_seconds"] == paused["elapsed_seconds"]
    assert source.heaters[:2] == [False, False]
    controller.resume()
    assert controller.status()["calibration_step"] == "initial"

    linear = _drive(controller, source, _heating_temp, 80, "linear")
    assert linear["target_temp_f"] == LEARN_TEMP_F
    assert controller._panel_lines(store.get_settings())[0].startswith("Linear:")
    coast = _drive(controller, source, _heating_temp, 40, "coast")
    assert coast["target_temp_f"] == LEARN_TEMP_F
    assert source.heaters[:2] == [False, False]
    assert coast["temps_f"][0] < LEARN_TEMP_F
    assert controller._panel_lines(store.get_settings())[0].startswith("Coast:")
    settings = store.get_settings()
    assert settings.learned_coast_f == 3
    model = settings.thermal_model
    assert model["linear_rate_f_per_sec"] == pytest.approx(0.4, abs=0.05)
    assert model["coast_rise_f"] is None
    run = store.get_run(coast["run_id"])
    assert [mark["name"] for mark in run["marks"]] == [
        "Start",
        "Initial heating",
        "Linear heating",
        "Coasting",
    ]
    assert run["analysis"] == []

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


def test_a_finished_calibration_closes_as_complete(store):
    source = StaticSource([80.0, 70.0])
    controller = Controller(store, source)
    started = controller.start({"kind": "calibration"})
    controller._session.finished = True
    controller.tick(1)
    saved = store.get_run(started["run_id"])
    assert saved["status"] == "complete"
    closing = [item for item in saved["logs"] if item["detail"].get("path") == "complete"]
    assert closing
    assert closing[-1]["message"].startswith("Complete.")
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


def test_a_long_tick_does_not_stack_hold_pulses(store):
    # Calibration 17 ran the target 4°F hot: the hold fired again before the
    # coast of the last pulse showed up, and a multi-second tick toggled the
    # SSR once per simulated second. A long tick still changes the heaters
    # once. The pulse opens when the coast already stored reaches the target,
    # before the probe reads it, and does not start another while that coast
    # is still arriving.
    source = StaticSource([159.5, 140.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration", "charge_gallons": 5})
    session = controller._session
    assert session is not None
    session.phase = "hold"
    session.phase_started = controller._elapsed
    edges: list[list[bool]] = []
    seen = list(source.heaters)
    original = source.set_heaters

    def track(states):
        now = [bool(item) for item in list(states)[:2]]
        if now != seen:
            edges.append(now)
            seen[:] = now
        original(states)

    source.set_heaters = track
    controller.tick(3)
    assert edges == [[True, True]]
    assert source.heaters[:2] == [True, True]

    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    assert edges == [[True, True], [False, False]]

    source.temps = [160.2, 140.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    assert edges == [[True, True], [False, False]]

    source.temps = [159.0, 140.0]
    controller.tick(0.4)
    assert source.heaters[:2] == [False, False]

    for step in range(12):
        source.temps = [min(164.5, 159.8 + 0.35 * step), 140.0]
        controller.tick(1)
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


def test_a_run_that_stops_at_or_under_100_has_no_cooldown(store):
    source = StaticSource([100.0, 70.0])
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


def test_cooldown_runs_until_the_probe_falls_under_100(store):
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

    source.temps = [99.0, 80.0]
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


def test_a_restart_already_under_100_closes_the_cooldown(store):
    source = StaticSource([165.0, 150.0])
    first = Controller(store, source)
    first.start()
    first.tick(1)
    run_id = first.stop()["cooldown_run_id"]

    resumed = Controller(store, StaticSource([99.0, 70.0]))
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


def test_a_calibration_run_does_not_ask_grok(store, monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("BREWSKI_ANALYSIS_INLINE", "1")
    calls = {"rate": 0, "model": 0}

    def rate(samples, kind):
        calls["rate"] += 1
        return 0.41

    def revised(samples, local):
        calls["model"] += 1
        return {
            "linear_rate_f_per_sec": 0.41,
            "coast_rise_f": (local.get("coast_rise_f") or 0) + 0.2,
            "cooldown_f_per_min": local.get("cooldown_f_per_min") or 2,
        }

    monkeypatch.setattr("brewski.analysis.analyze_rate", rate)
    monkeypatch.setattr("brewski.analysis.analyze_model", revised)
    source = StaticSource([70.0, 70.0])
    controller = Controller(store, source)
    controller.start({"kind": "calibration"})
    _drive(controller, source, _heating_temp, 80, "linear")
    _drive(controller, source, _heating_temp, 40, "coast")
    fitted = store.get_settings().thermal_model["linear_rate_f_per_sec"]
    source.temps = [80.0, 70.0]
    controller.stop()
    assert calls == {"rate": 0, "model": 0}
    assert store.get_settings().thermal_model["linear_rate_f_per_sec"] == fitted
    assert controller.runs()["runs"][0]["analysis"] == []


def test_queued_grok_analysis_can_still_replace_an_agreeing_rate(store, monkeypatch):
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
    local = controller._session.model.linear_rate_f_per_sec
    controller._queue_analysis(
        {
            "kind": "linear",
            "field": "linear_rate_f_per_sec",
            "local": local,
            "samples": [[0.0, 70.0], [1.0, 70.4]],
        }
    )
    assert store.get_settings().thermal_model["linear_rate_f_per_sec"] == pytest.approx(0.41)
    session = controller._session
    controller._session = None
    controller._queue_full_analysis(session)
    revised = store.get_settings().thermal_model
    assert revised["linear_rate_f_per_sec"] == pytest.approx(0.41)
    kinds = {item["kind"]: item["accepted"] for item in controller.runs()["runs"][0]["analysis"]}
    assert kinds["linear"] is True
    assert kinds["full"] is True


def test_a_later_calibration_averages_into_the_saved_model(store):
    current = store.get_settings()
    store.save_settings(
        replace(
            current,
            learned_coast_f=2,
            learned_coast_seconds=10,
            learned_cooldown_f_per_min=1,
            thermal_model={
                "cold_start_seconds": 20,
                "linear_rate_f_per_sec": 0.2,
                "coast_rise_f": 2,
                "coast_seconds": 10,
                "coast_quad": -2 / 100,
                "cooldown_f_per_min": 1,
                "calibration_count": 1,
            },
        )
    )
    controller = Controller(store, StaticSource([70.0, 70.0]))
    controller.start({"kind": "calibration"})
    controller._session.model.linear_rate_f_per_sec = 0.4
    controller._session.model.coast_rise_f = 4
    controller._session.model.coast_seconds = 20
    controller._session.model.cooldown_f_per_min = 3
    controller._commit_model(store.get_settings())
    saved = store.get_settings()
    assert saved.thermal_model["linear_rate_f_per_sec"] == pytest.approx(0.3)
    assert saved.thermal_model["coast_rise_f"] == pytest.approx(3)
    assert saved.thermal_model["calibration_count"] == 2
    assert saved.thermal_model["cold_start_seconds"] == pytest.approx(20)
    assert saved.learned_coast_f == pytest.approx(3)

    controller._session.model.coast_rise_f = 6
    controller._commit_model(store.get_settings())
    saved = store.get_settings()
    assert saved.thermal_model["coast_rise_f"] == pytest.approx(4)
    assert saved.thermal_model["calibration_count"] == 2


def test_clean_calibration_forgets_the_model_and_locks_runs(store):
    current = store.get_settings()
    store.save_settings(
        replace(
            current,
            learned_coast_f=4,
            learned_coast_seconds=12,
            learned_cooldown_f_per_min=2,
            thermal_model={
                "linear_rate_f_per_sec": 0.4,
                "coast_rise_f": 4,
                "calibration_count": 2,
            },
        )
    )
    controller = Controller(store, StaticSource([80.0, 70.0]))
    controller.start()
    with pytest.raises(ControllerError):
        controller.clear_calibration()
    controller.stop()
    status = controller.clear_calibration()
    assert status["learned_coast_f"] is None
    assert status["learned_cooldown_f_per_min"] is None
    assert status["calibration_count"] is None
    assert store.get_settings().thermal_model is None
    with pytest.raises(ControllerError):
        controller.start()
    started = controller.start({"kind": "calibration"})
    assert started["run_kind"] == "calibration"


def test_a_run_log_records_the_shutoff_calculation(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    started = controller.start()
    run_id = started["run_id"]
    opened = store.get_run(run_id)["logs"]
    assert opened[0]["event"] == "start"
    assert "Learned coast" in opened[0]["message"]
    assert any(item["event"] == "sample" and item["detail"]["heaters_on"] == [True, True] for item in opened)

    _soak_sheath(controller)
    source.temps = [162.0, 150.0]
    controller.tick(1)
    heat = [item for item in store.get_run(run_id)["logs"] if item["event"] == "heat"]
    assert heat
    assert heat[-1]["detail"]["temp_f"] == pytest.approx(162)
    assert heat[-1]["detail"]["heaters_on"] == [False, False]
    assert heat[-1]["detail"]["shutoff_coast_f"] == pytest.approx(3)
    assert "coast" in heat[-1]["message"].lower()


def test_a_ready_model_logs_the_crest_it_shut_off_on(store):
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
    started = controller.start()
    assert "linear" in store.get_run(started["run_id"])["logs"][0]["message"]
    _soak_sheath(controller)
    source.temps = [161.0, 90.0]
    controller.tick(1)
    heat = [item for item in store.get_run(started["run_id"])["logs"] if item["event"] == "heat"]
    assert heat[-1]["detail"]["heaters_on"] == [False, False]
    assert heat[-1]["detail"]["predicted_crest_f"] == pytest.approx(165)
    assert "crest" in heat[-1]["message"].lower()


def test_calibration_log_follows_the_stage_and_the_heaters(store):
    source = StaticSource([70.0, 70.0])
    controller = Controller(store, source)
    started = controller.start({"kind": "calibration"})
    run_id = started["run_id"]
    opened = store.get_run(run_id)["logs"]
    assert opened[0]["event"] == "start"
    assert "Calibration started" in opened[0]["message"]
    assert any(
        item["event"] == "sample" and item["detail"]["phase"] == "settle" and item["detail"]["heaters_on"] == [False, False]
        for item in opened
    )

    _drive(controller, source, _heating_temp, 10, "initial")
    logs = store.get_run(run_id)["logs"]
    phases = [item for item in logs if item["event"] == "phase"]
    assert phases[-1]["detail"]["phase"] == "initial"
    assert "Initial heating" in phases[-1]["message"]
    heats = [item for item in logs if item["event"] == "heat"]
    assert heats[-1]["detail"]["heaters_on"] == [True, True]
    assert heats[-1]["detail"]["temp_f"] == pytest.approx(70)


def test_cooldown_log_stays_on_the_run(store):
    source = StaticSource([165.0, 150.0])
    controller = Controller(store, source)
    controller.start()
    controller.tick(1)
    run_id = controller.stop()["cooldown_run_id"]
    logs = store.get_run(run_id)["logs"]
    assert any(item["detail"].get("path") == "stop" for item in logs)
    cooling = [item for item in logs if item["detail"].get("path") == "cooldown"]
    assert cooling
    assert cooling[-1]["detail"]["heaters_on"] == [False, False]
    assert "Cooldown" in cooling[-1]["message"]


def test_the_control_log_keeps_each_second_while_chart_samples_stay_at_five(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    run_id = controller.start()["run_id"]
    controller.tick(1)
    controller.tick(1)
    controller.tick(1)
    samples = [item for item in store.get_run(run_id)["logs"] if item["event"] == "sample"]
    assert [item["elapsed_seconds"] for item in samples] == pytest.approx([0, 1, 2, 3])
    assert len(store.get_run(run_id)["samples"]) == 1

    controller.tick(5)
    saved = store.get_run(run_id)
    samples = [item for item in saved["logs"] if item["event"] == "sample"]
    assert [item["elapsed_seconds"] for item in samples] == pytest.approx([0, 1, 2, 3, 4, 5, 6, 7, 8])
    assert [item["elapsed_seconds"] for item in saved["samples"]] == pytest.approx([0, 5])


def test_a_paused_run_keeps_logging_temperature_without_extra_samples(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    run_id = controller.start()["run_id"]
    controller.pause()
    before = len(store.get_run(run_id)["samples"])
    source.temps = [110.0, 90.0]
    controller.tick(1)
    saved = store.get_run(run_id)
    assert len(saved["samples"]) == before
    paused = [item for item in saved["logs"] if item["detail"].get("path") == "pause"]
    assert paused[-1]["detail"]["temp_f"] == pytest.approx(110)


def _ready_model(store):
    current = store.get_settings()
    store.save_settings(
        replace(
            current,
            thermal_model={
                "soak_tau_seconds": 37,
                "linear_rate_f_per_sec": 0.4,
                "coast_rise_f": 4,
                "coast_seconds": 30,
                "coast_tau_seconds": 10,
                "cooldown_f_per_min": 1,
            },
        )
    )


def _shut_off_on_the_crest(controller, source, temp=161.0):
    """Open the SSR where a hot sheath's predicted crest is the 165°F target."""

    controller._sheath_slope = 0.4
    controller._heater_on_for = [10_000.0, 10_000.0]
    source.temps = [temp, 90.0]
    controller.tick(1)


def test_a_ready_model_publishes_the_climb_and_its_crest(store):
    _ready_model(store)
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    started = controller.start()
    assert started["mode"] == "running"
    assert started["run_step"] == "climb"
    assert started["predicted_crest_f"] is not None
    assert store.get_run(started["run_id"])["samples"][0]["phase"] == "climb"
    assert source.heaters[:2] == [True, True]


def test_an_unspent_coast_does_not_start_another_pulse(store):
    _ready_model(store)
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _shut_off_on_the_crest(controller, source)
    assert source.heaters[:2] == [False, False]

    source.temps = [162.0, 90.0]
    controller.tick(1)
    status = controller.status()
    assert source.heaters[:2] == [False, False]
    assert status["run_step"] == "coast"
    assert status["mode"] == "running"
    assert status["predicted_crest_f"] == pytest.approx(165, abs=0.2)
    assert controller._decision["path"] == "coast"


def test_a_ready_model_holds_inside_the_band_until_stop(store):
    _ready_model(store)
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    run_id = controller.start()["run_id"]
    _shut_off_on_the_crest(controller, source)
    source.temps = [165.0, 90.0]
    controller.tick(1)
    status = controller.status()
    assert status["run_step"] == "hold"
    assert status["mode"] == "running"
    assert source.heaters[:2] == [False, False]
    assert "Holding" in [mark["name"] for mark in store.get_run(run_id)["marks"]]
    phases = [item["detail"]["phase"] for item in store.get_run(run_id)["logs"] if item["event"] == "phase"]
    assert "hold" in phases

    source.temps = [165.0, 90.0]
    controller.tick(30)
    assert controller.status()["mode"] == "running"
    assert controller.status()["run_step"] == "hold"
    assert source.heaters[:2] == [False, False]

    controller._sheath_slope = 0.0
    source.temps = [164.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]
    assert controller.status()["run_step"] == "hold"
    assert controller._decision["path"] == "hold"

    source.temps = [165.2, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    assert controller.status()["mode"] == "running"
    assert controller.status()["run_step"] == "hold"


def test_a_short_hold_crest_raises_the_next_aim(store):
    controller = Controller(store, StaticSource([160.0, 90.0]))
    controller._hold_peak_f = 159.5
    controller._settle_hold_bias(160.0)
    assert controller._hold_bias_f == pytest.approx(0.5)

    controller._hold_peak_f = 160.2
    controller._settle_hold_bias(160.0)
    assert controller._hold_bias_f == pytest.approx(0.3)

    controller._hold_peak_f = 159.0
    controller._settle_hold_bias(160.0)
    assert controller._hold_bias_f == pytest.approx(1.0)


def test_a_coast_that_finishes_short_climbs_again(store):
    _ready_model(store)
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _shut_off_on_the_crest(controller, source)
    source.temps = [158.0, 90.0]
    controller.tick(8)
    assert controller.status()["run_step"] == "climb"
    assert source.heaters[:2] == [True, True]
    assert controller.status()["mode"] == "running"


def test_raising_the_target_leaves_hold_and_climbs_on_the_budgeted_crest(store):
    _ready_model(store)
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    controller.start()
    _shut_off_on_the_crest(controller, source)
    source.temps = [165.0, 90.0]
    controller.tick(1)
    assert controller.status()["run_step"] == "hold"

    current = store.get_settings()
    store.save_settings(replace(current, target_temp_f=180))
    source.temps = [165.0, 90.0]
    controller.tick(1)
    assert controller.status()["run_step"] == "climb"
    assert controller._decision["path"] == "model"
    assert source.heaters[:2] == [True, True]

    controller._predict_samples.clear()
    controller._sheath_slope = 0.4
    controller._heater_on_for = [10_000.0, 10_000.0]
    source.temps = [175.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [True, True]

    controller._predict_samples.clear()
    controller._sheath_slope = 0.4
    source.temps = [176.0, 90.0]
    controller.tick(1)
    assert source.heaters[:2] == [False, False]
    assert controller._decision["predicted_crest_f"] == pytest.approx(180, abs=0.2)
    assert controller.status()["mode"] == "running"


def test_a_model_that_is_not_ready_still_uses_the_coast_cutoff(store):
    source = StaticSource([100.0, 90.0])
    controller = Controller(store, source)
    started = controller.start()
    assert started["run_step"] is None
    assert started["predicted_crest_f"] is None
    controller.tick(1)
    assert controller.status()["run_step"] is None
    assert controller._decision["path"] == "cutoff"
