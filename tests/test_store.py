import pytest

from brewski.store import SettingsError, heater_on_seconds, merge_settings, utc_now


def test_default_settings_are_two_heaters_and_two_sensors(store):
    settings = store.get_settings()
    assert settings.heater_count == 2
    assert settings.sensor_count == 2
    assert settings.heater_names == ["Heater 1", "Heater 2"]
    assert settings.sensor_names == ["Boiler", "Column"]
    assert settings.target_temp_f == 165


def test_settings_round_trip(store):
    updated = merge_settings(
        store.get_settings(),
        {
            "heater_count": 1,
            "sensor_count": 1,
            "heater_names": ["Kettle", "Spare"],
            "sensor_names": ["Pot", "Head"],
            "target_temp_f": 172.5,
        },
    )
    store.save_settings(updated)
    saved = store.get_settings()
    assert saved == updated


def test_settings_validation_rejects_bad_values(store):
    current = store.get_settings()
    with pytest.raises(SettingsError):
        merge_settings(current, {"heater_count": 3})
    with pytest.raises(SettingsError):
        merge_settings(current, {"target_temp_f": 20})
    with pytest.raises(SettingsError):
        merge_settings(current, {"sensor_names": ["Only one"]})
    with pytest.raises(SettingsError):
        merge_settings(current, {"heater_count": True})


def test_heater_on_time_uses_the_gap_between_samples(store):
    settings = store.get_settings()
    run = store.create_run(settings)
    store.add_sample(run["id"], 0, [70, 62], [True, False], 165)
    store.add_sample(run["id"], 10, [80, 70], [True, False], 165)
    store.update_run(run["id"], status="stopped", elapsed_seconds=10, ended_at=utc_now())

    listed = store.list_runs()
    saved = listed["runs"][0]
    assert saved["heater_on_seconds"] == pytest.approx([10, 0])
    assert listed["stats"]["total_runs"] == 1
    assert listed["stats"]["average_duration_seconds"] == pytest.approx(10)
    assert listed["stats"]["heater_on_seconds"] == pytest.approx([10, 0])

    detail = store.get_run(run["id"])
    assert len(detail["samples"]) == 2
    assert detail["sensor_names"] == ["Boiler", "Column"]


def test_open_runs_are_closed_on_the_next_startup(store):
    run = store.create_run(store.get_settings())
    store.close_open_runs()
    saved = store.get_run(run["id"])
    assert saved["status"] == "stopped"
    assert saved["ended_at"]


def test_heater_math_adds_the_tail_after_the_last_sample():
    totals = heater_on_seconds(
        [
            {"elapsed_seconds": 0, "heaters_on": [True, True]},
            {"elapsed_seconds": 5, "heaters_on": [False, True]},
        ],
        8,
    )
    assert totals == pytest.approx([5, 8])
