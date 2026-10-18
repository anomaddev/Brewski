"""v0.6.0: emergency stop, the target dial, and live-source selection."""

import pytest

from brewski.cli import _build_runtime
from brewski.controller import Controller, ControllerError
from brewski.emergency import CODES, check_code_list
from brewski.panel import indicator_leds
from brewski.sources.hts221 import calibration_from_bytes, compensate
from brewski.sources.max31865 import decode_rtd, temperature_f
from brewski.store import merge_settings


class _Panel:
    def __init__(self, events):
        self.events = events

    def take(self):
        events = self.events
        self.events = (0, 0, 0)
        return events

    def set_leds(self, *_args):
        return None

    def close(self):
        return None


def test_emergency_codes_reject_a_duplicate_or_a_renumbered_id():
    check_code_list([item.code for item in CODES])
    with pytest.raises(ValueError):
        check_code_list(["E01", "E01", "E03", "E04"])
    with pytest.raises(ValueError):
        check_code_list(["E01", "E03", "E04"])
    with pytest.raises(ValueError):
        check_code_list(["E02", "E01", "E03", "E04"])


def test_panel_leds_follow_the_action():
    assert indicator_leds("idle", emergency=False) == (True, False)
    assert indicator_leds("running", emergency=False) == (False, True)
    assert indicator_leds("paused", emergency=False) == (True, True)
    assert indicator_leds("cooling", emergency=False) == (True, False)
    assert indicator_leds("running", emergency=True) == (False, False)


def test_encoder_step_saves_the_target(controller, store):
    controller._panel = _Panel((0, 0, 1))
    controller.tick(0)
    assert store.get_settings().target_temp_f == 166

    store.save_settings(merge_settings(store.get_settings(), {"target_temp_f": 240}))
    controller._panel = _Panel((0, 0, 1))
    controller.tick(0)
    assert store.get_settings().target_temp_f == 240

    store.save_settings(merge_settings(store.get_settings(), {"target_temp_f": 90}))
    controller._panel = _Panel((0, 0, -1))
    controller.tick(0)
    assert store.get_settings().target_temp_f == 90


def test_uncalibrated_start_begins_calibration_and_stop_does_nothing_before_that(store, source):
    store.save_settings(merge_settings(store.get_settings(), {"setup_complete": True, "charge_gallons": 5}))
    controller = Controller(store, source)
    controller._panel = _Panel((0, 1, 0))
    controller.tick(0)
    assert controller.status()["mode"] == "idle"

    controller._panel = _Panel((1, 0, 0))
    controller.tick(0)
    status = controller.status()
    assert status["mode"] == "running"
    assert status["run_kind"] == "calibration"
    controller.shutdown()


def test_cabinet_over_the_limit_opens_every_ssr_and_refuses_start(controller, source):
    store = controller.store
    store.save_settings(merge_settings(store.get_settings(), {"heater_min_on_seconds": 30}))
    controller.start()
    controller.tick(1)
    assert any(source.heaters)
    source.cabinet_temp_f = 171
    controller.tick(0.2)
    status = controller.status()
    assert status["emergency_codes"] == ["E01"]
    assert status["emergency"][0]["summary"].startswith("Cabinet temperature")
    assert status["mode"] != "running"
    assert status["heaters_on"] == [False, False]
    assert source.heaters == [False, False]
    with pytest.raises(ControllerError) as caught:
        controller.start()
    assert caught.value.status == 409
    source.cabinet_temp_f = 170
    controller.tick(0.2)
    assert controller.status()["emergency_codes"] == []
    controller.start()


def test_unanswered_cabinet_sensor_trips_and_a_disabled_one_does_not(controller, source, store):
    source.climate_fault = True
    controller.tick(1)
    assert controller.status()["emergency_codes"] == ["E02"]
    with pytest.raises(ControllerError):
        controller.start()

    gpio = store.get_settings().gpio.to_dict()
    for device in gpio["i2c_devices"]:
        if device["id"] == "hts221":
            device["enabled"] = False
    store.save_settings(merge_settings(store.get_settings(), {"gpio": gpio}))
    source.cabinet_temp_f = 190
    controller.tick(1)
    assert controller.status()["emergency_codes"] == []
    controller.start()


def test_control_probe_fault_forces_heaters_off(controller, source):
    controller.start()
    controller.tick(1)
    assert any(source.heaters)
    source.probe_faults = [True, False]
    controller.tick(0.2)
    status = controller.status()
    assert status["emergency_codes"] == ["E03"]
    assert status["heaters_on"] == [False, False]
    assert source.heaters == [False, False]
    assert status["temps_f"][0] is None


def test_max31865_and_hts221_conversions():
    raw = round(100 / 430 * 32768)
    assert temperature_f(raw, 430, 100) == pytest.approx(32.0, abs=0.2)
    reading, fault = decode_rtd(0x3A, 0x01)
    assert fault is True
    assert reading == 0x1D00

    block = bytearray(16)
    block[1] = 200
    block[3] = 0x20
    block[5] = 0x0C
    block[10] = 100
    block[14] = 100
    calibration = calibration_from_bytes(bytes(block))
    temp_f, humidity = compensate(calibration, 40, 25)
    assert humidity == pytest.approx(40.0)
    assert temp_f == pytest.approx(77.0)


def test_installed_service_uses_the_pi_source(monkeypatch, tmp_path):
    monkeypatch.setenv("BREWSKI_DATA_DIR", str(tmp_path))

    def refuse_dummy(*_args, **_kwargs):
        raise AssertionError("the installed service must not construct DummySource")

    monkeypatch.setattr("brewski.cli.DummySource", refuse_dummy)

    class FakePi:
        name = "pi"

        def __init__(self, store):
            self.store = store

        def read_temperatures(self, count):
            return [70.0, 62.0][:count]

        def set_heaters(self, states):
            return None

        def read_climate(self):
            return 70.0, 40.0

        def close(self):
            return None

    monkeypatch.setattr("brewski.sources.pi.PiSource", FakePi)
    controller, _app = _build_runtime(False)
    assert isinstance(controller.source, FakePi)
    assert controller.source.name == "pi"
    controller.shutdown()


def test_dev_command_still_uses_the_dummy_source(monkeypatch, tmp_path):
    monkeypatch.setenv("BREWSKI_DATA_DIR", str(tmp_path))
    controller, _app = _build_runtime(True)
    assert controller.source.name == "dummy"
    controller.shutdown()
