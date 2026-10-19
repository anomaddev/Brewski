import sqlite3

import pytest

from brewski.store import (
    Channel,
    SettingsError,
    Store,
    default_gpio,
    heater_on_seconds,
    merge_settings,
    shape_changed,
    utc_now,
)


def test_default_settings_are_two_heaters_and_two_sensors(store):
    settings = store.get_settings()
    assert settings.heater_count == 2
    assert settings.sensor_count == 1
    assert settings.heater_names == ["Heater 1", "Heater 2"]
    assert settings.sensor_names == ["Boiler", "Column"]
    assert [channel.enabled for channel in settings.heaters] == [True, True]
    assert [channel.enabled for channel in settings.sensors] == [True, False]
    assert [channel.gpio for channel in settings.heaters] == [12, 16]
    assert [channel.gpio for channel in settings.sensors] == [17, 8]
    assert settings.gpio == default_gpio()
    assert [channel.wires for channel in settings.sensors] == [2, 2]
    assert [channel.rtd for channel in settings.sensors] == ["pt100", "pt100"]
    assert [channel.ref_ohms for channel in settings.sensors] == [430.0, 430.0]
    assert settings.target_temp_f == 165
    assert settings.cabinet_limit_f == 170
    assert settings.heater_min_on_seconds == 1.0
    assert settings.heater_min_off_seconds == 1.0
    assert settings.update_source == "main"
    assert settings.learned_coast_f is None
    assert settings.learned_cooldown_f_per_min is None
    assert settings.charge_gallons is None
    assert settings.setup_complete is False


def test_settings_round_trip(store):
    updated = merge_settings(
        store.get_settings(),
        {
            "heaters": [
                {"name": "Kettle", "enabled": True, "gpio": 18},
                {"name": "Spare", "enabled": False, "gpio": 19},
            ],
            "sensors": [
                {"name": "Pot", "enabled": True, "gpio": 20, "wires": 3, "rtd": "pt100", "ref_ohms": 430},
                {
                    "name": "Head",
                    "enabled": True,
                    "gpio": 21,
                    "wires": 4,
                    "rtd": "pt1000",
                    "ref_ohms": 4300,
                },
            ],
            "target_temp_f": 173,
        },
    )
    store.save_settings(updated)
    saved = store.get_settings()
    assert saved == updated
    assert saved.heater_count == 1
    assert saved.sensors[1].gpio == 21
    assert saved.sensors[1].wires == 4
    assert saved.sensors[1].rtd == "pt1000"
    assert saved.sensors[1].ref_ohms == 4300.0


def test_update_source_round_trips_and_is_not_wiring(store):
    current = store.get_settings()
    updated = merge_settings(current, {"update_source": "beta"})
    assert updated.update_source == "beta"
    assert shape_changed(current, updated) is False
    store.save_settings(updated)
    assert store.get_settings().update_source == "beta"
    assert store.get_settings().to_dict()["update_source"] == "beta"
    with pytest.raises(SettingsError, match="Update source must be main or beta"):
        merge_settings(current, {"update_source": "nightly"})
    restored = merge_settings(updated, {"update_source": "main"})
    store.save_settings(restored)
    assert store.get_settings().update_source == "main"


def test_settings_validation_rejects_bad_values(store):
    current = store.get_settings()
    heaters = [channel.to_dict() for channel in current.heaters]
    with pytest.raises(SettingsError):
        merge_settings(current, {"heater_count": 3})
    with pytest.raises(SettingsError):
        merge_settings(current, {"target_temp_f": 20})
    with pytest.raises(SettingsError):
        merge_settings(current, {"target_temp_f": 89})
    with pytest.raises(SettingsError):
        merge_settings(current, {"target_temp_f": 241})
    assert merge_settings(current, {"target_temp_f": 90}).target_temp_f == 90
    assert merge_settings(current, {"target_temp_f": 240}).target_temp_f == 240
    with pytest.raises(SettingsError):
        merge_settings(current, {"cabinet_limit_f": 99})
    with pytest.raises(SettingsError):
        merge_settings(current, {"cabinet_limit_f": 201})
    assert merge_settings(current, {"cabinet_limit_f": 100}).cabinet_limit_f == 100
    assert merge_settings(current, {"cabinet_limit_f": 200}).cabinet_limit_f == 200
    sensors = [channel.to_dict() for channel in current.sensors]
    accepted = [{**sensors[0], "name": "Thermometer"}, sensors[1]]
    assert merge_settings(current, {"sensors": accepted}).sensors[0].name == "Thermometer"
    with pytest.raises(SettingsError, match="1 to 11"):
        merge_settings(current, {"sensors": [{**sensors[0], "name": "ThermometerX"}, sensors[1]]})
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
    shared = [{**heaters[0], "gpio": 8}, heaters[1]]
    with pytest.raises(SettingsError, match="cannot share"):
        merge_settings(current, {"heaters": shared})
    same_select = [sensors[0], {**sensors[1], "gpio": sensors[0]["gpio"]}]
    with pytest.raises(SettingsError, match="own chip select"):
        merge_settings(current, {"sensors": same_select})
    with pytest.raises(SettingsError, match="SPI bus"):
        merge_settings(current, {"heaters": [{**heaters[0], "gpio": 10}, heaters[1]]})
    with pytest.raises(SettingsError, match="SPI bus"):
        merge_settings(current, {"sensors": [{**sensors[0], "gpio": 11}, sensors[1]]})
    with pytest.raises(SettingsError, match="wires must be 2, 3, or 4"):
        merge_settings(current, {"sensors": [{**sensors[0], "wires": 1}, sensors[1]]})
    with pytest.raises(SettingsError, match="rtd must be pt100 or pt1000"):
        merge_settings(current, {"sensors": [{**sensors[0], "rtd": "thermocouple"}, sensors[1]]})
    same_pin = [heaters[0], {**heaters[1], "gpio": heaters[0]["gpio"]}]
    with pytest.raises(SettingsError, match="own GPIO"):
        merge_settings(current, {"heaters": same_pin})
    with pytest.raises(SettingsError, match="BCM"):
        merge_settings(current, {"heaters": [{**heaters[0], "gpio": 1}, heaters[1]]})
    gpio = current.gpio.to_dict()
    with pytest.raises(SettingsError, match="encoder"):
        merge_settings(current, {"heaters": [{**heaters[0], "gpio": gpio["encoder"]["a"]}, heaters[1]]})
    with pytest.raises(SettingsError, match="button"):
        merge_settings(
            current, {"heaters": [{**heaters[0], "gpio": gpio["buttons"]["start"]}, heaters[1]]}
        )
    with pytest.raises(SettingsError, match="I2C"):
        merge_settings(current, {"heaters": [{**heaters[0], "gpio": gpio["i2c"]["sda"]}, heaters[1]]})
    gpio["encoder"]["b"] = gpio["encoder"]["a"]
    with pytest.raises(SettingsError, match="shared by"):
        merge_settings(current, {"gpio": gpio})
    with pytest.raises(SettingsError, match="between 0.5 and 60"):
        merge_settings(current, {"heater_min_on_seconds": 0.49})
    with pytest.raises(SettingsError, match="between 0.5 and 60"):
        merge_settings(current, {"heater_min_on_seconds": 60.01})
    assert merge_settings(current, {"heater_min_on_seconds": 0.5}).heater_min_on_seconds == 0.5
    assert merge_settings(current, {"heater_min_on_seconds": 1}).heater_min_on_seconds == 1.0
    with pytest.raises(SettingsError, match="heater_min_off_seconds must be between 0.5 and 60"):
        merge_settings(current, {"heater_min_off_seconds": 0.49})
    assert merge_settings(current, {"heater_min_off_seconds": 2}).heater_min_off_seconds == 2.0
    with pytest.raises(SettingsError, match="Unknown settings: early_cutoff_f"):
        merge_settings(current, {"early_cutoff_f": 2.5})


def test_probe_fields_default_to_a_two_wire_pt100(store):
    current = store.get_settings()
    updated = merge_settings(
        current,
        {
            "sensors": [
                {"name": "Pot", "enabled": True, "gpio": 18},
                {"name": "Head", "enabled": True, "gpio": 19, "rtd": "pt1000"},
            ]
        },
    )
    assert updated.sensors[0].wires == 2
    assert updated.sensors[0].rtd == "pt100"
    assert updated.sensors[0].ref_ohms == 430.0
    assert updated.sensors[1].wires == 2
    assert updated.sensors[1].rtd == "pt1000"
    assert updated.sensors[1].ref_ohms == 4300.0


def test_shared_one_wire_pins_become_separate_chip_selects(store):
    import json

    raw = [
        {"name": "Boiler", "enabled": True, "gpio": 4},
        {"name": "Column", "enabled": True, "gpio": 4},
    ]
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE settings SET sensors = ? WHERE id = 1", (json.dumps(raw),))
    connection.commit()
    connection.close()

    loaded = store.get_settings()
    assert [channel.gpio for channel in loaded.sensors] == [17, 8]
    assert loaded.sensors[0].name == "Boiler"
    assert loaded.sensors[1].rtd == "pt100"
    assert loaded.sensors[1].wires == 2
    assert loaded.sensors[1].ref_ohms == 430.0


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
    assert settings.heaters[0] == Channel("Kettle", True, 12)
    assert settings.heaters[1].enabled is False
    assert settings.heaters[1].gpio == 16
    assert settings.sensors[1].name == "Head"
    assert [channel.gpio for channel in settings.sensors] == [17, 8]
    assert settings.gpio == default_gpio()
    assert settings.sensors[0].rtd == "pt100"
    assert settings.sensors[0].wires == 2
    assert settings.sensors[0].ref_ohms == 430.0
    assert settings.target_temp_f == 165
    assert settings.heater_min_on_seconds == 1.0
    assert settings.heater_min_off_seconds == 1.0
    assert settings.update_source == "main"
    assert settings.learned_coast_f is None
    assert settings.learned_cooldown_f_per_min is None
    assert settings.setup_complete is True
    run = store.get_run(1)
    assert run["kind"] == "run"
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


def test_element_hours_survive_a_data_reset_and_can_be_zeroed(store):
    store.add_heater_life([90, 15])
    store.add_heater_life([30, -1])
    assert store.heater_life_seconds() == pytest.approx([120, 15])
    assert store.list_runs()["stats"]["heater_life_seconds"] == pytest.approx([120, 15])
    store.reset()
    assert store.get_settings().target_temp_f == 165
    assert store.list_runs()["runs"] == []
    assert store.heater_life_seconds() == pytest.approx([120, 15])
    store.reset_heater_life(1)
    assert store.heater_life_seconds() == pytest.approx([120, 0])
    with pytest.raises(SettingsError, match="Heater slot"):
        store.reset_heater_life(2)


def test_existing_runs_seed_element_hours_once(tmp_path):
    path = tmp_path / "old.db"
    store = Store(path)
    settings = store.get_settings()
    run = store.create_run(settings)
    store.add_sample(run["id"], 0, [70, 62], [True, False], 165)
    store.add_sample(run["id"], 10, [80, 70], [False, False], 165)
    store.add_sample(run["id"], 40, [150, 140], [True, True], 165, phase="cooldown")
    store.update_run(run["id"], status="stopped", elapsed_seconds=10, ended_at=utc_now())
    calibration = store.create_run(settings, kind="calibration", charge_gallons=5)
    store.add_sample(calibration["id"], 0, [70, 62], [True, True], 165)
    store.add_sample(calibration["id"], 4, [74, 66], [False, False], 165)
    store.update_run(calibration["id"], status="stopped", elapsed_seconds=4, ended_at=utc_now())

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE heater_meters")

    reopened = Store(path)
    assert reopened.heater_life_seconds() == pytest.approx([14, 4])
    reopened.add_heater_life([1, 0])
    assert reopened.heater_life_seconds() == pytest.approx([15, 4])
    again = Store(path)
    assert again.heater_life_seconds() == pytest.approx([15, 4])


def test_old_target_limit_and_probe_name_are_brought_into_range(store):
    import json

    saved = store.get_settings()
    sensors = [channel.to_dict() for channel in saved.sensors]
    sensors[0]["name"] = "Very long probe name"
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE settings
            SET target_temp_f = 40, cabinet_limit_f = 250, sensors = ?, sensor_names = ?
            WHERE id = 1
            """,
            (json.dumps(sensors), json.dumps(["Very long probe name", sensors[1]["name"]])),
        )
    loaded = store.get_settings()
    assert loaded.target_temp_f == 90
    assert loaded.cabinet_limit_f == 200
    assert loaded.sensors[0].name == "Very long p"
    assert len(loaded.sensors[0].name) == 11


def test_saved_lamp_pins_that_were_documented_backwards_are_swapped(store):
    import json

    gpio = default_gpio().to_dict()
    gpio["leds"] = {"start": 23, "stop": 24}
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE settings SET gpio = ? WHERE id = 1", (json.dumps(gpio),))

    loaded = Store(store.path).get_settings()
    assert loaded.gpio.led_start == 24
    assert loaded.gpio.led_stop == 23
    again = Store(store.path).get_settings()
    assert again.gpio.led_start == 24
    assert again.gpio.led_stop == 23


def test_a_custom_lamp_pair_is_left_alone(store):
    import json

    gpio = default_gpio().to_dict()
    gpio["leds"] = {"start": 25, "stop": 26}
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE settings SET gpio = ? WHERE id = 1", (json.dumps(gpio),))

    loaded = Store(store.path).get_settings()
    assert loaded.gpio.led_start == 25
    assert loaded.gpio.led_stop == 26


def test_heater_math_adds_the_tail_after_the_last_sample():
    totals = heater_on_seconds(
        [
            {"elapsed_seconds": 0, "heaters_on": [True, True]},
            {"elapsed_seconds": 5, "heaters_on": [False, True]},
        ],
        8,
    )
    assert totals == pytest.approx([5, 8])
