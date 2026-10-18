from dataclasses import replace

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


def test_status_screen_for_each_mode():
    idle = status_lines(
        mode="idle",
        elapsed_seconds=0,
        probe_name="Boiler",
        temp_f=70,
        target_f=165,
        heaters=[(0, False), (1, False)],
    )
    assert idle[0] == "Idle"
    assert idle[1] == "Boiler 70 F"
    assert idle[2] == "Target 165 F"
    assert idle[3] == "Heaters off"
    _lines_fit(idle)

    running = status_lines(
        mode="running",
        elapsed_seconds=754,
        probe_name="Boiler",
        temp_f=168.4,
        target_f=172,
        heaters=[(0, True), (1, False)],
    )
    assert running[0] == "Running 12:34"
    assert running[1] == "Boiler 168.4 F"
    assert running[3] == "H1 on  H2 off"
    _lines_fit(running)

    paused = status_lines(
        mode="paused",
        elapsed_seconds=3661,
        probe_name="Boiler",
        temp_f=160,
        target_f=172,
        heaters=[(0, False)],
    )
    assert paused[0] == "Paused 1:01:01"
    _lines_fit(paused)

    cooling = status_lines(
        mode="cooling",
        elapsed_seconds=5,
        probe_name="Boiler",
        temp_f=150,
        target_f=172,
        heaters=[(0, False), (1, False)],
    )
    assert cooling[0] == "Cooling 00:05"
    assert cooling[3] == "Heaters off"
    _lines_fit(cooling)


def test_long_probe_name_keeps_the_temperature():
    lines = status_lines(
        mode="idle",
        elapsed_seconds=0,
        probe_name="Very long boiler probe name",
        temp_f=70.2,
        target_f=165,
        heaters=[],
    )
    assert lines[1].endswith("70.2 F")
    assert len(lines[1]) <= COLUMNS


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
    assert controller._display.lines[0] == "Idle"
