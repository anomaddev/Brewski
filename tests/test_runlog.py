import csv
import io

from brewski.runlog import (
    analysis_message,
    control_log_csv,
    format_control,
    model_message,
    start_message,
    suite_result_message,
)


def test_a_heating_line_shows_the_probe_slope_when_the_sheath_is_still_flat():
    message = format_control(
        {
            "path": "cold_start",
            "temp_f": 77.2,
            "heaters_on": [True, True],
            "sheath_slope_f_per_sec": 0,
            "live_slope_f_per_sec": 0.48,
            "seconds_to_target": 90,
        }
    )
    assert "Probe slope 0.480°F/s" in message
    assert "sheath slope 0.000°F/s" in message
    assert "90s to target" in message


def test_a_flat_minimum_pulse_is_not_described_as_a_climb():
    message = format_control(
        {
            "path": "model",
            "temp_f": 69.9,
            "target_f": 165,
            "predicted_crest_f": 69.9,
            "heaters_on": [True, True],
            "slope_f_per_sec": 0,
            "on_seconds": 0,
            "gain_f": 0,
        }
    )
    assert "Crest 69.9°F versus 165°F" in message
    assert "minimum pulse" not in message


def test_a_stage_two_line_shows_the_crest_math():
    message = format_control(
        {
            "path": "approach",
            "temp_f": 128.4,
            "remaining_coast_f": 8.2,
            "predicted_crest_f": 136.6,
            "goal_f": 140,
            "slope_f_per_sec": 0.041,
            "heaters_on": [True, True],
            "seconds_to_target": 42,
        }
    )
    assert "128.4°F + 8.2°F coast = crest 136.6°F" in message
    assert "under the 140°F step" in message
    assert "Heaters on" in message
    assert "0.041°F/s" in message
    assert "42s" in message


def test_a_run_line_shows_the_shutoff_coast_and_a_held_heater():
    message = format_control(
        {
            "path": "cutoff",
            "temp_f": 160.2,
            "shutoff_coast_f": 3,
            "cutoff_f": 162,
            "slope_f_per_sec": 0.02,
            "soak": 0.4,
            "heaters_on": [True, False],
            "dwell": "min_on",
        }
    )
    assert "Shutoff coast 3.0°F" in message
    assert "slope 0.020°F/s" in message
    assert "soak 0.40" in message
    assert "Cutoff 162°F" in message
    assert "minimum on time" in message


def test_start_and_model_lines_name_the_numbers_a_run_began_with():
    assert start_message("calibration", 140, None, None) == (
        "Calibration started at 140°F. No prior heating model."
    )
    assert start_message("test", 165, None, 3) == "Initial Heating test started."
    assert suite_result_message(
        {"found": True, "on_seconds": 180, "off_seconds": 180}
    ) == "Initial Heating recorded 3:00 on and 3:00 off. The curve is in this run."
    assert suite_result_message({"found": False, "stopped": True}) == (
        "Initial Heating stopped before the heaters came on."
    )
    assert "linear 0.400°F/s" in start_message(
        "run",
        165,
        {"linear_rate_f_per_sec": 0.4, "coast_rise_f": 6.4, "coast_seconds": 18},
        6.4,
    )
    assert model_message(
        {"linear_rate_f_per_sec": 0.4, "coast_rise_f": 6.4, "coast_seconds": 18, "cooldown_f_per_min": 0.8}
    ) == "Model saved. linear 0.400°F/s, coast 6.4°F over 18s, cooling 0.8°F/min."
    assert "Kept the local fit" in analysis_message("linear", 0.4, 0.5, False)
    assert analysis_message("full", None, None, False) == "Full-run analysis was not applied."


def test_the_control_log_exports_temperature_heaters_and_the_calculation():
    text = control_log_csv(
        [
            {
                "id": 1,
                "recorded_at": "2026-10-20T00:00:00Z",
                "elapsed_seconds": 1.0,
                "event": "sample",
                "message": "Run. 100.0°F. Heaters on. Crest 104.0°F versus 165°F.",
                "detail": {
                    "phase": "run",
                    "path": "model",
                    "temp_f": 100.0,
                    "target_f": 165,
                    "heat": True,
                    "heaters_on": [True, False],
                    "predicted_crest_f": 104.0,
                    "model": {"linear_rate_f_per_sec": 0.4},
                },
            }
        ]
    )
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0]["elapsed_seconds"] == "1.0"
    assert rows[0]["temp_f"] == "100.0"
    assert rows[0]["heater_1_on"] == "1"
    assert rows[0]["heater_2_on"] == "0"
    assert rows[0]["predicted_crest_f"] == "104.0"
    assert rows[0]["model_linear_rate_f_per_sec"] == "0.4"
    assert "Crest 104.0°F" in rows[0]["message"]
