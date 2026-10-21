import pytest

from brewski.calibration import CalibrationSession
from brewski.thermal import (
    CALIBRATION_TEMP_F,
    APPROACH_MARGIN_F,
    ThermalModel,
    blend_model,
    coast_rise_for,
    fit_startup_quad,
    coast_sized_should_heat,
    hold_should_heat,
    predicted_crest,
    project_temperature,
    remaining_coast_f,
    seconds_to_landing,
)


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
    # Half the shutoff slope stores a quarter of the coast rise.
    assert coast_rise_for(model, 10) == pytest.approx(2)


def test_linear_heat_projects_the_same_crest_from_the_current_temp():
    model = ThermalModel(
        cold_start_seconds=20,
        cold_start_quad=0.01,
        linear_rate_f_per_sec=0.4,
        coast_rise_f=4,
        coast_seconds=20,
        coast_quad=-4 / 400,
    )
    # 61°F of straight heat, then the same 4°F crest over 20s.
    assert seconds_to_landing(100, 165, 40, model, heating=True, horizon=1000) == pytest.approx(
        61 / 0.4 + 20
    )
    # Already inside that crest. Shut off now; 3°F of the 4°F parabola is 10s.
    assert seconds_to_landing(162, 165, 40, model, heating=True, horizon=1000) == pytest.approx(10)
    # A hotter point uses the same crest, so less of the parabola is left.
    later = seconds_to_landing(163, 165, 80, model, heating=True, horizon=1000)
    assert later == pytest.approx(5.858, abs=0.01)
    assert later < 10


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
    # Slope 0.2 stores 2°F. 146 + 2 is still short of 150.
    assert coast_sized_should_heat(146, 150, 12.0, model, 1.0, slope_f_per_sec=0.2) is True
    # Slope 0.3 stores 4.5°F, so opening now coasts to the target.
    assert coast_sized_should_heat(146, 150, 16.0, model, 1.0, slope_f_per_sec=0.3) is False
    # The same slope rule ends a pulse sooner when the gap is only 1°F.
    assert coast_sized_should_heat(149, 150, 4.0, model, 1.0, slope_f_per_sec=0.1) is True
    assert coast_sized_should_heat(149, 150, 6.0, model, 1.0, slope_f_per_sec=0.15) is False
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
    session.model = model
    session.sheath_slope = 0.0
    session._set_heat(10.0, 146.0, 5.0, 1.0)
    assert session.heat is True
    session.sheath_slope = 0.3
    session._set_heat(20.0, 146.0, 15.0, 1.0)
    assert session.heat is False
    assert session.predicted_crest == pytest.approx(150.5, abs=0.05)


def test_the_next_minute_is_the_coast_from_cutting_the_heaters_off_now():
    model = ThermalModel(
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=40,
        cooldown_f_per_min=1.2,
    )
    points = dict(project_temperature(100, 0.4, model, 1.2))
    assert points[0] == pytest.approx(100)
    # Full crest at 40s, then 20s of the 1.2°F/min fall. Not 100 + 0.4*60.
    assert points[40] == pytest.approx(108, abs=0.05)
    assert points[60] == pytest.approx(107.6, abs=0.05)
    # Half the shutoff slope stores a quarter of the crest, so the minute ends lower.
    shorter = dict(project_temperature(100, 0.2, model, 1.2))
    assert shorter[20] == pytest.approx(102, abs=0.05)
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
    # Run 3 on the still: Stage I crest 13.96°F at 0.329°F/s. Stage II was
    # still steepening past that rate when the heaters opened at 129°F.
    model = ThermalModel(
        linear_rate_f_per_sec=0.32917,
        coast_rise_f=13.96,
        coast_seconds=275.53,
    )
    assert remaining_coast_f(model, 0.32917) == pytest.approx(13.96, abs=0.05)
    steeper = remaining_coast_f(model, 0.5)
    assert steeper == pytest.approx(13.96 * (0.5 / 0.32917), abs=0.05)
    assert 125 + steeper >= CALIBRATION_TEMP_F - APPROACH_MARGIN_F

    session = CalibrationSession()
    session.phase = "approach"
    session.model = model
    session.sheath_slope = 0.32917
    session.samples = [(float(t), 125.0 - 0.5 * (15 - t)) for t in range(16)]
    session._set_heat(15.0, 125.0, 40.0, 1.0)
    assert session.heat is False
    assert session.approach_coasting is True
