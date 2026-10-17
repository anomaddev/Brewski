import sqlite3

import pytest

from brewski.store import Channel, SettingsError, Store, heater_on_seconds, merge_settings, utc_now


def test_default_settings_are_two_heaters_and_two_sensors(store):
    settings = store.get_settings()
    assert settings.heater_count == 2
    assert settings.sensor_count == 2
    assert settings.heater_names == ["Heater 1", "Heater 2"]
    assert settings.sensor_names == ["Boiler", "Column"]
    assert [channel.enabled for channel in settings.heaters] == [True, True]
    assert [channel.gpio for channel in settings.heaters] == [17, 27]
    assert [channel.gpio for channel in settings.sensors] == [4, 4]
    assert settings.target_temp_f == 165


def test_settings_round_trip(store):
    updated = merge_settings(
        store.get_settings(),
        {
            "heaters": [
                {"name": "Kettle", "enabled": True, "gpio": 23},
                {"name": "Spare", "enabled": False, "gpio": 24},
            ],
            "sensors": [
                {"name": "Pot", "enabled": True, "gpio": 5},
                {"name": "Head", "enabled": True, "gpio": 5},
            ],
            "target_temp_f": 173,
        },
    )
    store.save_settings(updated)
    saved = store.get_settings()
    assert saved == updated
    assert saved.heater_count == 1


def test_settings_validation_rejects_bad_values(store):
    current = store.get_settings()
    heaters = [channel.to_dict() for channel in current.heaters]
    with pytest.raises(SettingsError):
        merge_settings(current, {"heater_count": 3})
    with pytest.raises(SettingsError):
        merge_settings(current, {"target_temp_f": 20})
    with pytest.raises(SettingsError):
        merge_settings(current, {"target_temp_f": 251})
    assert merge_settings(current, {"target_temp_f": 250}).target_temp_f == 250
    with pytest.raises(SettingsError):
        merge_settings(current, {"target_temp_f": 172.5})
    with pytest.raises(SettingsError):
        merge_settings(current, {"sensors": [current.sensors[0].to_dict()]})
    with pytest.raises(SettingsError):
        merge_settings(current, {"heaters": [{**heaters[0], "enabled": "yes"}, heaters[1]]})
    off = [{**channel, "enabled": False} for channel in heaters]
    with pytest.raises(SettingsError, match="One heater must stay enabled"):
        merge_settings(current, {"heaters": off})
    sensors = [channel.to_dict() for channel in current.sensors]
    with pytest.raises(SettingsError, match="One temperature probe must stay enabled"):
        merge_settings(current, {"sensors": [{**channel, "enabled": False} for channel in sensors]})
    shared = [{**heaters[0], "gpio": 4}, heaters[1]]
    with pytest.raises(SettingsError, match="cannot share"):
        merge_settings(current, {"heaters": shared})
    same_pin = [heaters[0], {**heaters[1], "gpio": heaters[0]["gpio"]}]
    with pytest.raises(SettingsError, match="own GPIO"):
        merge_settings(current, {"heaters": same_pin})
    with pytest.raises(SettingsError, match="BCM"):
        merge_settings(current, {"heaters": [{**heaters[0], "gpio": 1}, heaters[1]]})


def test_older_databases_keep_counts_and_gain_default_pins(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            heater_count INTEGER NOT NULL,
            sensor_count INTEGER NOT NULL,
            heater_names TEXT NOT NULL,
            sensor_names TEXT NOT NULL,
            target_temp_f REAL NOT NULL
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            target_temp_f REAL NOT NULL,
            heater_count INTEGER NOT NULL,
            sensor_count INTEGER NOT NULL,
            heater_names TEXT NOT NULL,
            sensor_names TEXT NOT NULL,
            elapsed_seconds REAL NOT NULL DEFAULT 0
        );
        """
    )
    connection.execute(
        "INSERT INTO settings VALUES (1, 1, 2, ?, ?, 165.4)",
        ('["Kettle", "Spare"]', '["Pot", "Head"]'),
    )
    connection.execute(
        """
        INSERT INTO runs (
            started_at, status, target_temp_f, heater_count, sensor_count, heater_names, sensor_names
        ) VALUES ('2026-01-01T00:00:00+00:00', 'stopped', 160, 1, 1, ?, ?)
        """,
        ('["Kettle", "Spare"]', '["Pot", "Head"]'),
    )
    connection.commit()
    connection.close()

    store = Store(path)
    settings = store.get_settings()
    assert settings.heaters[0] == Channel("Kettle", True, 17)
    assert settings.heaters[1].enabled is False
    assert settings.sensors[1].name == "Head"
    assert settings.target_temp_f == 165
    run = store.get_run(1)
    assert run["heaters"][0]["enabled"] is True
    assert run["heaters"][1]["enabled"] is False
    assert run["heaters"][0]["gpio"] is None


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
