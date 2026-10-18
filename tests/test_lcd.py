import os
from dataclasses import replace
from pathlib import Path

from brewski.controller import Controller
from brewski.lcd import (
    COLUMNS,
    NullDisplay,
    display_key,
    frame,
    open_display,
    progress_lines,
    prompt_lines,
    status_lines,
)
from brewski.sources.dummy import DummySource
from brewski.store import I2cDevice, default_settings


def _lines_fit(lines):
    framed = frame(lines)
    assert len(framed) == 4
    assert all(len(line) <= COLUMNS for line in framed)


def _screen(**overrides):
    fields = {
        "mode": "running",
        "elapsed_seconds": 0,
        "target_f": 165,
        "probes": [("Boiler", 168.5)],
        "heaters": [(True, False), (True, False)],
    }
    fields.update(overrides)
    return status_lines(**fields)


def test_running_screen_fills_every_line():
    lines = _screen()
    assert lines == [
        "Running:    00:00:00",
        "Target:        165 F",
        "Boiler:      168.5 F",
        "H1: OFF      H2: OFF",
    ]
    assert all(len(line) == COLUMNS for line in lines)


def test_two_probe_line_and_heater_padding():
    both = _screen(probes=[("Boiler", 168.2), ("Column", 154.1)])
    assert both[1] == "Target:        165 F"
    assert both[2] == "T1:168.2F  T2:154.1F"
    assert both[3] == "H1: OFF      H2: OFF"
    assert all(len(line) == COLUMNS for line in both)

    assert _screen(heaters=[(True, True), (True, False)])[3] == "H1: ON       H2: OFF"
    assert _screen(heaters=[(True, False), (True, True)])[3] == "H1: OFF      H2: ON "
    assert _screen(heaters=[(True, True), (True, True)])[3] == "H1: ON       H2: ON "
    assert _screen(heaters=[(False, True), (False, True)])[3] == "H1: OFF      H2: OFF"

    fault = _screen(probes=[("Boiler", None), ("Column", 154.1)])
    assert fault[2] == "T1:FAULT   T2:154.1F"
    assert len(fault[2]) == COLUMNS


def test_shorter_target_keeps_the_degree_flush_right():
    lines = _screen(target_f=90, probes=[("Boiler", 90.0)])
    assert lines[1] == "Target:         90 F"
    assert len(lines[1]) == COLUMNS
    assert _screen(probes=[("Thermometer", 168.5)])[2] == "Thermometer: 168.5 F"


def test_calibration_prompt_and_clock():
    prompt = _screen(mode="idle", needs_calibration=True)
    assert prompt[0] == "Calibrate:     START"
    assert prompt[1] == "Target:        165 F"
    assert prompt[2] == "Boiler:      168.5 F"
    assert prompt[3] == "H1: OFF      H2: OFF"
    assert all(len(line) == COLUMNS for line in prompt)

    running = _screen(calibrating=True)
    assert running[0] == "Calibrate:  00:00:00"
    assert len(running[0]) == COLUMNS


def test_idle_paused_and_cooling():
    idle = _screen(mode="idle")
    assert idle[0] == "Idle:"
    assert idle[1] == "Target:        165 F"
    assert idle[3] == "H1: OFF      H2: OFF"

    paused = _screen(mode="paused", elapsed_seconds=3661, probes=[("Boiler", 160.0)])
    assert paused[0] == "Paused:     01:01:01"
    assert len(paused[0]) == COLUMNS

    cooling = _screen(mode="cooling", elapsed_seconds=5, probes=[("Boiler", 150.0)])
    assert cooling[0] == "Cooling:    00:00:05"
    assert len(cooling[0]) == COLUMNS


def test_emergency_screens():
    heaters = [(True, False), (True, False)]
    cabinet = _screen(
        emergency_code="E01",
        cabinet_temp_f=171.2,
        cabinet_limit_f=170,
        heaters=heaters,
    )
    assert cabinet == [
        "Emergency:       E01",
        "Limit:         170 F",
        "Cabinet:     171.2 F",
        "H1: OFF      H2: OFF",
    ]
    assert all(len(line) == COLUMNS for line in cabinet)

    unread = _screen(emergency_code="E02", cabinet_fault=True, cabinet_limit_f=170)
    assert unread[0] == "Emergency:       E02"
    assert unread[1] == "Limit:         170 F"
    assert unread[2] == "Cabinet:       FAULT"
    assert unread[3] == "H1: OFF      H2: OFF"
    assert all(len(line) == COLUMNS for line in unread)

    probe = _screen(emergency_code="E03", probes=[("Boiler", None)])
    assert probe[0] == "Emergency:       E03"
    assert probe[1] == "Target:        165 F"
    assert probe[2] == "Boiler:        FAULT"
    assert all(len(line) == COLUMNS for line in probe)

    spi = _screen(emergency_code="E04", probes=[("Boiler", None), ("Column", None)])
    assert spi[0] == "Emergency:       E04"
    assert spi[1] == "Target:        165 F"
    assert spi[2] == "T1:FAULT    T2:FAULT"
    assert len(spi[2]) == COLUMNS


def test_prompt_and_progress_screens():
    prompt = prompt_lines("0.2.0", "0.3.0", 60)
    assert prompt[0] == "Update available"
    assert prompt[1] == "0.2.0 -> 0.3.0"
    assert prompt[2] == "START update"
    assert prompt[3] == "STOP keep 60s"
    _lines_fit(prompt)
    _lines_fit(progress_lines("downloading", "0.2.0", "0.3.0"))
    _lines_fit(progress_lines("building", "0.2.0", "0.3.0"))
    _lines_fit(progress_lines("installing", "0.2.0", "0.3.0"))
    failed = progress_lines("failed", "0.2.0", "0.3.0")
    assert failed[0] == "Update failed"
    assert "0.2.0" in failed[1]
    _lines_fit(failed)
    done = progress_lines("complete", "0.2.0", "0.3.0")
    assert done[0] == "Update complete"
    assert done[1] == "0.3.0"
    _lines_fit(done)


def test_display_stays_closed_until_setup_saves_the_pins():
    settings = default_settings()
    assert display_key(settings) is None
    assert not open_display(settings).hardware

    ready = replace(settings, setup_complete=True)
    assert display_key(ready) == (2, 3)

    gpio = ready.gpio
    moved = replace(ready, gpio=replace(gpio, i2c_sda=4, i2c_scl=5))
    assert display_key(moved) == (4, 5)
    assert not open_display(moved).hardware

    disabled = tuple(
        I2cDevice(device.id, device.name, False) if device.id == "lcd" else device
        for device in gpio.i2c_devices
    )
    off = replace(ready, gpio=replace(gpio, i2c_devices=disabled))
    assert display_key(off) is None
    assert not open_display(off).hardware


def test_controller_does_not_open_the_display_before_setup(store, source, monkeypatch):
    opened = []

    def fake_open(settings):
        opened.append(settings.setup_complete)
        return NullDisplay()

    monkeypatch.setattr("brewski.controller.open_display", fake_open)
    controller = Controller(store, source)
    controller._refresh_display()
    assert opened == []
    assert controller._display_key is None


def test_a_failed_update_gives_the_display_back(store, source, monkeypatch):
    from brewski.store import merge_settings
    from brewski.updates import write_status

    monkeypatch.setattr("brewski.controller.open_display", lambda _settings: NullDisplay())
    store.save_settings(merge_settings(store.get_settings(), {"setup_complete": True}))
    controller = Controller(store, source)
    controller._hold_display = True
    controller._refresh_display()
    assert controller._hold_display is True
    assert controller._display_key is None

    write_status(
        phase="failed",
        current="0.2.0",
        latest="0.3.0",
        error="The installer did not finish.",
        directory=store.path.parent,
    )
    controller._refresh_display()
    assert controller._hold_display is False
    assert controller._display_key == (2, 3)


def test_controller_opens_the_saved_pins_after_setup(store, source, monkeypatch):
    from brewski.store import merge_settings

    opened = []

    def fake_open(settings):
        opened.append((settings.gpio.i2c_sda, settings.gpio.i2c_scl))
        display = NullDisplay()
        display.write(["Idle"])
        return display

    monkeypatch.setattr("brewski.controller.open_display", fake_open)
    store.save_settings(merge_settings(store.get_settings(), {"setup_complete": True}))
    controller = Controller(store, DummySource(noise=False))
    controller._refresh_display()
    assert opened == [(2, 3)]
    assert controller._display.lines[0] == "Calibrate:     START"


def test_lgpio_is_imported_from_a_writable_directory(tmp_path, monkeypatch):
    from brewski import lcd

    monkeypatch.setattr(lcd, "_lgpio_directory", lambda: tmp_path)
    seen = {}

    def load():
        seen["cwd"] = os.getcwd()
        return object()

    monkeypatch.setattr(lcd, "_load_lgpio_module", load)
    before = os.getcwd()
    assert lcd._ensure_lgpio() is True
    assert Path(seen["cwd"]) == tmp_path
    assert os.getcwd() == before


def test_a_broken_lgpio_import_does_not_stop_startup(tmp_path, monkeypatch):
    from brewski import lcd

    monkeypatch.setattr(lcd, "_lgpio_directory", lambda: tmp_path)

    def load():
        raise FileNotFoundError(".lgd-nfy0")

    monkeypatch.setattr(lcd, "_load_lgpio_module", load)
    before = os.getcwd()
    assert lcd._ensure_lgpio() is False
    assert os.getcwd() == before
