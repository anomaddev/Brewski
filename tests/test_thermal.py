import math

import pytest

from brewski.calibration import CalibrationSession
from brewski.thermal import (
    CALIBRATION_TEMP_F,
    LEARN_TEMP_F,
    SEED_COAST_RISE_F,
    SEED_COAST_TAU_SECONDS,
    SEED_LINEAR_RATE_F_PER_SEC,
    SEED_SOAK_TAU_SECONDS,
    TUNE_FRACTION,
    TUNE_RATE_CAP_F_PER_SEC,
    TUNE_RISE_CAP_F,
    TUNE_TAU_CAP_SECONDS,
    ThermalModel,
    blend_model,
    coast_entry_slope,
    coast_rise_for,
    fit_coast_exponential,
    fit_heating_lag,
    fit_startup_quad,
    heating_lag_coast,
    coast_sized_should_heat,
    hold_should_heat,
    predicted_crest,
    pulse_plan,
    rate_while_heating,
    project_temperature,
    remaining_coast_f,
    seconds_to_landing,
    seed_model,
    settled_linear_rate,
    step_sheath_slope,
    tune_model,
)


def test_a_soaking_element_fits_an_exponential_and_a_straight_line_does_not():
    rate = 0.25
    tau = 40.0
    samples = []
    for step in range(0, 141):
        elapsed = float(step)
        rise = rate * (elapsed - tau * (1 - math.exp(-elapsed / tau)))
        samples.append((elapsed, 70.0 + rise))
    fitted = fit_heating_lag(samples)
    assert fitted is not None
    assert fitted[0] == pytest.approx(rate, rel=0.08)
    assert fitted[1] == pytest.approx(tau, rel=0.15)
    coast = heating_lag_coast(fitted[0], fitted[1], 140.0)
    assert coast == pytest.approx(rate * tau * (1 - math.exp(-140 / tau)), rel=0.1)

    straight = [(float(step), 70.0 + 0.2 * step) for step in range(0, 80)]
    assert fit_heating_lag(straight) is None

    # 13°F in about a minute is only the start of a 265s soak. Reporting the
    # asymptote would claim ~89°F/min and a ~90°F coast.
    short = []
    wild_rate = 89.3 / 60.0
    wild_tau = 265.0
    for step in range(0, 71):
        elapsed = float(step)
        rise = wild_rate * (elapsed - wild_tau * (1.0 - math.exp(-elapsed / wild_tau)))
        short.append((elapsed, 128.8 + rise))
    assert fit_heating_lag(short) is None


def test_cold_start_is_a_parabola_and_a_shorter_coast_crests_lower():
    samples = [(float(t), 70.0 + 0.01 * t * t) for t in range(21)]
    assert fit_startup_quad(samples) == pytest.approx(0.01)

    model = ThermalModel(
        cold_start_seconds=20,
        cold_start_quad=0.01,
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=20,
        coast_quad=-8 / 400,
    )
    assert coast_rise_for(model, 40) == pytest.approx(8)
    assert predicted_crest(100, 40, 0, model) == pytest.approx(108)
    # Half the straight rate stores half the coast rise.
    assert coast_rise_for(model, 10) == pytest.approx(4)


def test_linear_heat_projects_the_same_crest_from_the_current_temp():
    model = ThermalModel(
        cold_start_seconds=20,
        cold_start_quad=0.01,
        linear_rate_f_per_sec=0.4,
        coast_rise_f=4,
        coast_seconds=20,
        coast_quad=-4 / 400,
    )
    # 61°F of straight heat, then the coast approaches the last 4°F.
    tau = 4 / 0.4
    tail = -tau * math.log(1 - 3.5 / 4)
    assert seconds_to_landing(100, 165, 40, model, heating=True, horizon=1000) == pytest.approx(
        61 / 0.4 + tail
    )
    # Already inside that crest. The exponential covers the last 3°F.
    inside = -tau * math.log(1 - 3 / 4)
    assert seconds_to_landing(162, 165, 40, model, heating=True, horizon=1000) == pytest.approx(inside)
    later = seconds_to_landing(163, 165, 80, model, heating=True, horizon=1000)
    assert later == pytest.approx(-tau * math.log(1 - 2 / 4))
    assert later < inside


def test_hold_pulses_instead_of_running_to_the_target():
    model = ThermalModel(
        cold_start_seconds=20,
        cold_start_quad=0.01,
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=20,
        coast_quad=-8 / 400,
    )
    # Inside one crest, still on, slope not yet at the line. Turn off.
    # Running until the probe reads 150 would release the whole crest past it.
    assert hold_should_heat(146, 150, 5.0, model, 1.0, slope_f_per_sec=0.0) is False
    # Far below: keep heating so the slope can reach the line.
    assert hold_should_heat(100, 150, 5.0, model, 1.0, slope_f_per_sec=0.05) is True
    # Off and cold: a minimum pulse still lands under the target, so start one.
    assert hold_should_heat(146, 150, 0.0, model, 1.0, slope_f_per_sec=0.0) is True
    # Off and already at the start of the crest: another pulse coasts past.
    assert hold_should_heat(146, 150, 0.0, model, 1.0, slope_f_per_sec=0.4) is False


def test_stage_three_holds_the_pulse_until_the_stored_coast_reaches_the_target():
    model = ThermalModel(
        cold_start_seconds=20,
        cold_start_quad=0.01,
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=20,
        coast_quad=-8 / 400,
    )
    # Cold sheath: nothing is stored yet, so the pulse keeps going.
    assert coast_sized_should_heat(146, 150, 5.0, model, 1.0, slope_f_per_sec=0.0) is True
    # Slope 0.2 stores half of 8°F. 146 + 4 lands on 150, so the pulse opens.
    assert coast_sized_should_heat(146, 150, 12.0, model, 1.0, slope_f_per_sec=0.2) is False
    # Slope 0.3 stores 6°F, so opening now coasts past the target.
    assert coast_sized_should_heat(146, 150, 16.0, model, 1.0, slope_f_per_sec=0.3) is False
    # Four seconds on this parabola is already 0.08°F/s, which stores 1.6°F.
    # The probe slope of 0.04°F/s is below that, so it does not shrink the coast.
    assert coast_sized_should_heat(149, 150, 4.0, model, 1.0, slope_f_per_sec=0.04) is False
    assert coast_sized_should_heat(149, 150, 6.0, model, 1.0, slope_f_per_sec=0.05) is False
    # Straight heat far below the target stays on until temp plus the full crest.
    assert coast_sized_should_heat(141, 150, 40.0, model, 1.0, slope_f_per_sec=0.4) is True
    assert coast_sized_should_heat(142, 150, 50.0, model, 1.0, slope_f_per_sec=0.4) is False
    # Off and cold: start the pulse. Off with a soaked sheath: the minimum
    # on time would coast past the target, so wait.
    assert coast_sized_should_heat(146, 150, 0.0, model, 1.0, slope_f_per_sec=0.0) is True
    assert coast_sized_should_heat(146, 150, 0.0, model, 1.0, slope_f_per_sec=0.4) is False
    assert coast_sized_should_heat(150, 150, 0.0, model, 1.0, slope_f_per_sec=0.0) is False

    session = CalibrationSession()
    session.phase = "hold"
    session.guess = model
    session.sheath_slope = 0.0
    session._set_heat(10.0, 146.0, 5.0, 1.0)
    assert session.heat is True
    session.sheath_slope = 0.4
    session._set_heat(20.0, 152.0, 15.0, 1.0)
    assert session.heat is False
    assert session.predicted_crest == pytest.approx(160.0, abs=0.05)


def test_the_next_minute_is_the_coast_from_cutting_the_heaters_off_now():
    model = ThermalModel(
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=40,
        cooldown_f_per_min=1.2,
    )
    points = dict(project_temperature(100, 0.4, model, 1.2))
    assert points[0] == pytest.approx(100)
    # τ = 20s. At 40s the exponential has delivered most of the 8°F, not 100 + 0.4*60.
    assert points[40] == pytest.approx(100 + 8 * (1 - math.exp(-2)), abs=0.05)
    assert points[60] == pytest.approx(100 + 8 * (1 - math.exp(-3)), abs=0.05)
    # Half the shutoff slope stores half the rise.
    shorter = dict(project_temperature(100, 0.2, model, 1.2))
    assert shorter[20] == pytest.approx(100 + 4 * (1 - math.exp(-1)), abs=0.05)
    assert shorter[60] < points[60]
    falling = dict(project_temperature(150, -0.01, model, 1.2))
    assert falling[60] == pytest.approx(149.4, abs=0.02)


def test_a_later_calibration_has_the_same_weight_as_the_ones_already_saved():
    first = blend_model(ThermalModel(), ThermalModel(linear_rate_f_per_sec=0.4, coast_rise_f=4, coast_seconds=10))
    assert first.linear_rate_f_per_sec == pytest.approx(0.4)
    assert first.calibration_count == 1
    assert first.coast_quad == pytest.approx(-4 / 100)

    second = blend_model(
        first,
        ThermalModel(linear_rate_f_per_sec=0.2, coast_rise_f=8, coast_seconds=20, cold_start_seconds=30),
    )
    assert second.calibration_count == 2
    assert second.linear_rate_f_per_sec == pytest.approx(0.3)
    assert second.coast_rise_f == pytest.approx(6)
    assert second.coast_seconds == pytest.approx(15)
    assert second.coast_quad == pytest.approx(-6 / 225, abs=1e-6)
    assert second.cold_start_seconds == pytest.approx(30)

    # A stage the new run did not measure stays put, and the new run counts once.
    third = blend_model(second, ThermalModel(linear_rate_f_per_sec=0.5))
    assert third.calibration_count == 3
    assert third.linear_rate_f_per_sec == pytest.approx((0.3 * 2 + 0.5) / 3, abs=1e-5)
    assert third.coast_rise_f == pytest.approx(6)
    assert third.cold_start_seconds == pytest.approx(30)


def test_a_steeper_live_slope_keeps_the_heaters_off_before_the_stage_one_crest():
    # Run 3 on the still: Stage I crest 13.96°F at 0.329°F/s. A probe still
    # steepening past that rate is storing a larger crest.
    model = ThermalModel(
        linear_rate_f_per_sec=0.32917,
        coast_rise_f=13.96,
        coast_seconds=275.53,
    )
    assert remaining_coast_f(model, 0.32917) == pytest.approx(13.96, abs=0.05)
    steeper = remaining_coast_f(model, 0.5)
    assert steeper == pytest.approx(13.96 * (0.5 / 0.32917), abs=0.05)
    assert 130 + steeper >= LEARN_TEMP_F


def test_sheath_slope_decays_on_the_coast_time_constant():
    model = ThermalModel(
        linear_rate_f_per_sec=0.346,
        coast_rise_f=14.31,
        coast_seconds=198.77,
    )
    tau = 14.31 / 0.346
    assert step_sheath_slope(model, 0.346, heating=False, dt=198.77) == pytest.approx(
        0.346 * math.exp(-198.77 / tau), abs=1e-4
    )
    assert step_sheath_slope(model, 0.346, heating=False, dt=tau) == pytest.approx(
        0.346 / math.e, abs=1e-4
    )


# Run 10, heat kept about every 2 seconds and the coast about every 8.
# Heaters opened at 184.6s while the probe was still climbing.
_RUN10_HEAT = [
    (3.8, 81.92), (5.8, 81.86), (7.9, 81.92), (9.9, 81.92), (11.9, 81.92),
    (13.9, 81.98), (15.9, 82.04), (17.9, 82.1), (19.9, 82.16), (22.0, 82.22),
    (24.0, 82.34), (26.0, 82.59), (28.0, 82.83), (30.0, 83.32), (32.1, 83.99),
    (34.1, 84.42), (36.1, 84.9), (38.1, 85.51), (40.1, 86.06), (42.2, 86.61),
    (44.2, 87.28), (46.2, 87.89), (48.2, 88.62), (50.3, 89.29), (52.3, 90.03),
    (54.3, 90.7), (56.3, 91.43), (58.3, 92.16), (60.3, 92.83), (62.4, 93.63),
    (64.4, 94.48), (66.4, 95.21), (68.4, 95.95), (70.4, 96.68), (72.4, 97.47),
    (74.5, 98.21), (76.5, 98.94), (79.0, 99.67), (81.0, 100.53), (83.0, 101.39),
    (85.1, 102.18), (87.1, 102.91), (89.1, 103.71), (91.1, 104.38), (93.1, 105.18),
    (95.2, 105.97), (97.2, 106.65), (99.2, 107.44), (101.2, 108.11), (103.2, 108.85),
    (105.3, 109.71), (107.3, 110.62), (109.3, 111.36), (111.3, 112.09), (113.3, 112.95),
    (115.4, 113.75), (117.4, 114.48), (119.4, 115.34), (121.5, 116.14), (123.5, 116.87),
    (125.5, 117.67), (127.5, 118.47), (129.6, 119.33), (131.6, 120.06), (133.6, 120.8),
    (135.6, 121.59), (137.6, 122.33), (139.6, 123.07), (141.6, 123.93), (143.7, 124.72),
    (145.7, 125.52), (147.7, 126.5), (149.8, 127.24), (151.8, 127.98), (153.8, 128.9),
    (155.8, 129.76), (157.8, 130.49), (159.8, 131.36), (161.8, 132.09), (164.3, 132.89),
    (166.3, 133.63), (168.3, 134.43), (170.4, 135.35), (172.4, 136.33), (174.4, 137.13),
    (176.5, 137.81), (178.5, 138.67), (180.6, 139.53), (182.6, 140.33), (183.6, 140.7),
]
_RUN10_COAST = [
    (184.6, 141.13), (192.7, 144.39), (200.8, 147.54), (208.9, 149.63),
    (217.0, 151.17), (225.1, 152.34), (233.6, 153.27), (241.7, 153.76),
    (249.8, 154.32), (257.9, 154.56), (265.9, 154.75), (274.0, 154.99),
    (282.1, 155.12), (290.1, 155.24), (298.2, 155.36), (306.3, 155.36),
    (314.4, 155.61), (322.7, 155.55), (331.6, 155.61), (339.7, 155.67),
    (347.8, 155.49), (355.9, 155.55), (364.0, 155.49),
]


def test_run_10_fits_soak_linear_heat_and_an_exponential_coast():
    fitted = fit_heating_lag(_RUN10_HEAT)
    assert fitted is not None
    rate, tau = fitted
    assert rate == pytest.approx(0.41, abs=0.03)
    assert tau == pytest.approx(37, rel=0.15)
    linear = settled_linear_rate(_RUN10_HEAT)
    assert linear == pytest.approx(0.40, abs=0.02)
    coast_fit = fit_coast_exponential(_RUN10_COAST, 0.407)
    assert coast_fit is not None
    rise, coast_tau = coast_fit
    assert coast_tau == pytest.approx(35, abs=8)
    assert rise == pytest.approx(14, abs=1.5)
    assert _RUN10_COAST[-1][1] - _RUN10_COAST[0][1] == pytest.approx(14.3, abs=0.5)
    origin_t, origin_v = _RUN10_COAST[0]
    misses = []
    for stamp, reading in _RUN10_COAST[1:]:
        age = stamp - origin_t
        predicted = origin_v + 0.407 * coast_tau * (1.0 - math.exp(-age / coast_tau))
        misses.append(reading - predicted)
    residual = math.sqrt(sum(miss * miss for miss in misses) / len(misses))
    assert residual < 0.75


def test_tune_model_ignores_a_miss_inside_the_deadband_and_caps_one_step():
    quiet = seed_model()
    tune_model(quiet, heat_delta_f=0.1, slope_delta=0.005, coast_delta_f=0.1)
    assert quiet.linear_rate_f_per_sec == pytest.approx(SEED_LINEAR_RATE_F_PER_SEC)
    assert quiet.soak_tau_seconds == pytest.approx(SEED_SOAK_TAU_SECONDS)
    assert quiet.coast_rise_f == pytest.approx(SEED_COAST_RISE_F)

    hot = seed_model()
    tune_model(hot, heat_delta_f=5, slope_delta=0.05)
    assert hot.linear_rate_f_per_sec - SEED_LINEAR_RATE_F_PER_SEC <= TUNE_RATE_CAP_F_PER_SEC + 1e-9
    assert hot.linear_rate_f_per_sec <= SEED_LINEAR_RATE_F_PER_SEC * (1 + TUNE_FRACTION) + 1e-9
    assert SEED_SOAK_TAU_SECONDS - hot.soak_tau_seconds <= TUNE_TAU_CAP_SECONDS + 1e-9
    assert hot.linear_rate_f_per_sec > SEED_LINEAR_RATE_F_PER_SEC
    assert hot.soak_tau_seconds < SEED_SOAK_TAU_SECONDS

    ahead = seed_model()
    tune_model(ahead, coast_delta_f=4)
    assert SEED_COAST_TAU_SECONDS - ahead.coast_tau_seconds <= TUNE_TAU_CAP_SECONDS + 1e-9
    assert ahead.coast_tau_seconds < SEED_COAST_TAU_SECONDS
    assert abs(ahead.coast_rise_f - SEED_COAST_RISE_F) <= TUNE_RISE_CAP_F + 1e-9

    cold = seed_model()
    tune_model(cold, heat_delta_f=-5, crest_delta_f=-8)
    assert SEED_LINEAR_RATE_F_PER_SEC - cold.linear_rate_f_per_sec <= TUNE_RATE_CAP_F_PER_SEC + 1e-9
    assert SEED_COAST_RISE_F - cold.coast_rise_f <= TUNE_RISE_CAP_F + 1e-9
    assert cold.linear_rate_f_per_sec < SEED_LINEAR_RATE_F_PER_SEC
    assert cold.coast_rise_f < SEED_COAST_RISE_F


def test_a_string_of_updates_pulls_a_stale_seed_toward_run_10():
    model = seed_model()
    model.linear_rate_f_per_sec = 0.25
    model.coast_rise_f = 8
    for _ in range(25):
        tune_model(
            model,
            heat_delta_f=(0.40 - model.linear_rate_f_per_sec) * 30,
            crest_delta_f=14 - model.coast_rise_f,
        )
    assert model.linear_rate_f_per_sec == pytest.approx(0.40, abs=0.015)
    assert model.coast_rise_f == pytest.approx(14, abs=0.5)


def test_learning_coast_opens_while_the_probe_is_still_under_140():
    session = CalibrationSession()
    session.phase = "initial"
    session.heat_started = 0.0
    session.heat_origin = 80.0
    # 126.5°F plus a 0.4°F/s slope times the 35s coast is already past 140.
    session.samples = [(float(t), 121.7 + 0.4 * t) for t in range(13)]
    session.sheath_slope = 0.4
    session._advance(12.0, 126.5)
    assert session.phase == "coast"
    assert session.heat is False
    assert 126.5 < LEARN_TEMP_F


def test_a_coast_short_of_140_lowers_the_rise_and_a_high_coast_raises_it():
    short = seed_model()
    tune_model(short, crest_delta_f=110 - LEARN_TEMP_F)
    assert short.coast_rise_f < SEED_COAST_RISE_F
    assert SEED_COAST_RISE_F - short.coast_rise_f <= TUNE_RISE_CAP_F + 1e-9

    high = seed_model()
    tune_model(high, crest_delta_f=148 - LEARN_TEMP_F)
    assert high.coast_rise_f > SEED_COAST_RISE_F
    assert high.coast_rise_f - SEED_COAST_RISE_F <= TUNE_RISE_CAP_F + 1e-9


def test_the_coast_enters_at_the_heating_slope_for_this_on_time():
    model = seed_model()
    # The probe has not moved, but the elements have. Five seconds and thirty
    # seconds are different places on the coast.
    short = pulse_plan(100.0, 160.0, 5.0, model, 1.0, slope_f_per_sec=0.0, budgeted=False)
    longer = pulse_plan(100.0, 160.0, 30.0, model, 1.0, slope_f_per_sec=0.0, budgeted=False)
    assert short["slope_f_per_sec"] == pytest.approx(rate_while_heating(model, 5.0), abs=1e-4)
    assert longer["slope_f_per_sec"] == pytest.approx(rate_while_heating(model, 30.0), abs=1e-4)
    assert longer["remaining_coast_f"] > short["remaining_coast_f"]
    assert short["remaining_coast_f"] == pytest.approx(
        remaining_coast_f(model, coast_entry_slope(model, 5.0, 0.0)),
        abs=0.01,
    )
    # A probe steeper than the soak raises the entry. A lower probe does not.
    steep = pulse_plan(100.0, 160.0, 30.0, model, 1.0, slope_f_per_sec=0.5, budgeted=False)
    assert steep["slope_f_per_sec"] == pytest.approx(0.5)
    assert steep["remaining_coast_f"] > longer["remaining_coast_f"]


def test_the_climb_pulse_stays_on_until_the_projection_crests_at_160():
    session = CalibrationSession()
    session.phase = "pulse"
    session.guess = seed_model()
    session.sheath_slope = session.guess.linear_rate_f_per_sec
    session._set_heat(10.0, 140.0, 30.0, 1.0)
    assert session.heat is True
    assert session.predicted_crest <= CALIBRATION_TEMP_F + 0.05

    session.sheath_slope = session.guess.linear_rate_f_per_sec
    # 146 plus the full 14°F rise is exactly 160, so this sample opens.
    session._set_heat(40.0, 146.0, 80.0, 1.0)
    assert session.heat is False
    assert session.predicted_crest == pytest.approx(CALIBRATION_TEMP_F, abs=0.2)


def test_a_short_pulse_is_followed_by_a_shorter_one_and_hold_opens_before_160():
    session = CalibrationSession()
    session.phase = "pulse"
    session.guess = seed_model()
    session.pulse_coasting = True
    session.open_temp = 150.0
    session.open_elapsed = 0.0
    session.shutoff_slope = 0.2
    session.peak_average = 156.0
    session.peak_elapsed = 40.0
    session.plateau_since = 0.0
    before = session.guess.coast_rise_f
    session._advance(200.0, 156.0)
    assert session.phase == "pulse"
    assert session.pulse_coasting is False
    assert session.guess.coast_rise_f <= before
    session.sheath_slope = 0.05
    session._set_heat(201.0, 156.0, 0.0, 1.0)
    assert session.heat is True
    assert session.predicted_crest <= CALIBRATION_TEMP_F + 0.05

    session.phase = "hold"
    session.pulse_coasting = False
    session.sheath_slope = 0.2
    # 153°F plus 0.2°F/s times the coast constant lands on 160, so the hold opens now.
    session._set_heat(300.0, 153.0, 20.0, 1.0)
    assert session.heat is False
    assert session.predicted_crest == pytest.approx(CALIBRATION_TEMP_F, abs=0.5)
    assert 153.0 < CALIBRATION_TEMP_F
