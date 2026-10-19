"""Software menu screens, and a Stop hold versus a short press."""

from brewski.menu import SoftwareMenu
from brewski.panel import HOLD_RELEASE_OFF_SECONDS, STOP_HOLD_SECONDS, Panel
from brewski.updates import ReleaseInfo


class _Button:
    def __init__(self) -> None:
        self.when_pressed = None
        self.when_held = None
        self.when_released = None
        self.hold_time = 1


class _Leds:
    def set(self, start_on: bool, stop_on: bool) -> None:
        self.state = (start_on, stop_on)

    def close(self) -> None:
        self.closed = True


def _panel():
    stop = _Button()
    leds = _Leds()
    panel = Panel(_Button(), stop, leds, None)
    return panel, stop, leds


def test_a_stop_release_is_a_short_press(monkeypatch):
    panel, stop, _leds = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    stop.is_pressed = True
    stop.when_pressed()
    now["t"] += 0.06
    stop.is_pressed = False
    stop.when_released()
    assert panel.take() == (0, 1, 0, 0)


def test_a_stop_spike_does_not_count_or_block_start(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    start = _Button()
    stop = _Button()
    leds = _Leds()
    panel = Panel(start, stop, leds, None)
    panel.set_leds(True, True)
    stop.is_pressed = True
    stop.when_pressed()
    assert leds.state == (True, False)
    now["t"] += 0.02
    stop.is_pressed = False
    stop.when_released()
    assert leds.state == (True, True)
    start.is_pressed = True
    start.when_pressed()
    now["t"] += 0.06
    start.is_pressed = False
    start.when_released()
    assert panel.take() == (1, 0, 0, 0)


def test_a_quick_stop_counts_when_the_release_callback_is_missed(monkeypatch):
    panel, stop, _leds = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    stop.is_pressed = True
    stop.when_pressed()
    stop.is_pressed = False
    now["t"] += 0.06
    assert panel.take() == (0, 1, 0, 0)


def test_a_quick_start_counts_once_on_the_way_up(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    start = _Button()
    panel = Panel(start, _Button(), _Leds(), None)
    start.is_pressed = True
    start.when_pressed()
    start.when_pressed()
    assert panel.take() == (0, 0, 0, 0)
    now["t"] += 0.06
    start.is_pressed = False
    start.when_released()
    assert panel.take() == (1, 0, 0, 0)
    now["t"] += 0.05
    start.is_pressed = True
    start.when_pressed()
    now["t"] += 0.06
    start.is_pressed = False
    start.when_released()
    assert panel.take() == (1, 0, 0, 0)


def test_a_short_stop_still_counts_when_start_blips(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    start = _Button()
    stop = _Button()
    panel = Panel(start, stop, _Leds(), None)
    stop.is_pressed = True
    stop.when_pressed()
    start.is_pressed = True
    start.when_pressed()
    now["t"] += 0.02
    start.is_pressed = False
    start.when_released()
    now["t"] += 0.04
    stop.is_pressed = False
    stop.when_released()
    assert panel.take() == (0, 1, 0, 0)


def test_a_short_start_still_counts_when_stop_blips(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    start = _Button()
    stop = _Button()
    panel = Panel(start, stop, _Leds(), None)
    start.is_pressed = True
    start.when_pressed()
    stop.is_pressed = True
    stop.when_pressed()
    now["t"] += 0.02
    stop.is_pressed = False
    stop.when_released()
    now["t"] += 0.04
    start.is_pressed = False
    start.when_released()
    assert panel.take() == (1, 0, 0, 0)


def test_a_start_press_does_not_count_as_a_stop(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    start = _Button()
    stop = _Button()
    panel = Panel(start, stop, _Leds(), None)
    start.is_pressed = True
    start.when_pressed()
    now["t"] += 0.2
    stop.is_pressed = True
    stop.when_pressed()
    stop.is_pressed = False
    stop.when_released()
    assert panel.take() == (0, 0, 0, 0)
    start.is_pressed = False
    start.when_released()
    assert panel.take() == (1, 0, 0, 0)


def test_a_stop_press_does_not_count_as_a_start(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    start = _Button()
    stop = _Button()
    panel = Panel(start, stop, _Leds(), None)
    stop.is_pressed = True
    stop.when_pressed()
    start.is_pressed = True
    start.when_pressed()
    start.is_pressed = False
    start.when_released()
    assert panel.take() == (0, 0, 0, 0)
    now["t"] += 0.2
    stop.is_pressed = False
    stop.when_released()
    assert panel.take() == (0, 1, 0, 0)


def test_a_hold_after_a_finished_tap_cannot_reopen_the_menu(monkeypatch):
    panel, stop, _leds = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    stop.is_pressed = True
    stop.when_pressed()
    generation = panel._gestures.stop.generation
    now["t"] += 0.2
    stop.when_released()
    stop.is_pressed = True
    panel._gestures.fire(generation)
    assert panel.take() == (0, 1, 0, 0)


def test_the_hold_timer_lights_the_lamp_before_the_next_poll(monkeypatch):
    panel, stop, leds = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    panel.set_leds(True, True)
    stop.is_pressed = True
    stop.when_pressed()
    assert leds.state == (True, False)
    panel._gestures.fire(panel._gestures.stop.generation)
    assert leds.state == (True, True)
    assert panel.take() == (0, 0, 1, 0)
    stop.is_pressed = False
    stop.when_released()
    assert leds.state == (True, False)
    assert panel.take() == (0, 0, 0, 0)


def test_a_hold_that_arrives_after_release_is_ignored(monkeypatch):
    panel, stop, _leds = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    stop.is_pressed = True
    stop.when_pressed()
    generation = panel._gestures.stop.generation
    now["t"] += 0.2
    stop.is_pressed = False
    stop.when_released()
    assert panel.take() == (0, 1, 0, 0)
    stop.is_pressed = True
    panel._gestures.fire(generation)
    assert panel.take() == (0, 0, 0, 0)


def test_the_hold_lamp_comes_on_and_goes_out_with_the_button(monkeypatch):
    panel, stop, leds = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    panel.set_leds(True, True)
    stop.is_pressed = True
    stop.when_pressed()
    assert leds.state == (True, False)
    now["t"] += STOP_HOLD_SECONDS
    assert panel.take() == (0, 0, 1, 0)
    assert leds.state == (True, True)
    stop.is_pressed = False
    stop.when_released()
    assert leds.state == (True, False)
    assert panel.take() == (0, 0, 0, 0)
    now["t"] += HOLD_RELEASE_OFF_SECONDS
    panel.take()
    assert leds.state == (True, True)


def test_a_short_release_does_not_leave_a_hold_behind(monkeypatch):
    panel, stop, _leds = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    stop.is_pressed = True
    stop.when_pressed()
    now["t"] += 0.2
    stop.is_pressed = False
    stop.when_released()
    assert panel.take() == (0, 1, 0, 0)
    assert panel.take() == (0, 0, 0, 0)


def test_software_menu_lines_and_leds():
    menu = SoftwareMenu()
    assert menu.lines("0.6.5") == [
        "Software Menu",
        "> About",
        "  Check for Update",
        "  Reboot",
    ]
    assert menu.leds() == (True, True)
    assert menu.move(4) is True
    assert menu.lines("0.6.5")[2] == "> Check for Update"
    menu.index = 0
    assert menu.move(-1) is True
    assert menu.lines("0.6.5")[3] == "> Reboot"
    menu.select()
    assert menu.lines("0.6.5") == ["Reboot the Pi?", "START reboot", "STOP cancel", ""]
    assert menu.leds() == (True, True)
    assert menu.back() is True
    assert menu.lines("0.6.5")[0] == "Software Menu"

    menu.index = 0
    menu.select()
    assert menu.lines("9.9.9") == ["About", "Brewski", "9.9.9", "STOP back"]
    assert menu.leds() == (False, True)
    assert menu.select() is None

    menu.back()
    menu.index = 1
    assert menu.select() == "check"
    assert menu.lines("0.6.5")[1] == "Checking..."
    menu.show_release(
        ReleaseInfo(current="0.6.5", latest="0.6.5", available=False, reachable=True),
        False,
    )
    assert menu.lines("0.6.5") == ["Check for Update", "Up to date", "0.6.5", "STOP back"]
    assert menu.leds() == (False, True)

    menu.show_release(
        ReleaseInfo(current="0.6.5", latest=None, available=False, reachable=False),
        False,
    )
    assert menu.lines("0.6.5")[1] == "GitHub unreachable"

    menu.show_release(
        ReleaseInfo(current="0.6.5", latest="0.6.6", available=True, reachable=True),
        False,
    )
    assert menu.lines("0.6.5")[2] == "Cannot install here"
    assert menu.select() is None

    menu.show_release(
        ReleaseInfo(current="0.6.5", latest="0.6.6", available=True, reachable=True),
        True,
    )
    assert menu.lines("0.6.5")[2] == "START install"
    assert menu.leds() == (True, True)
    assert menu.select() == "apply"

    menu.screen = "reboot"
    menu.failed = False
    menu.select()
    assert menu.lines("0.6.5")[0] == "Rebooting"
    assert menu.leds() == (False, False)
    menu.fail_reboot()
    assert menu.lines("0.6.5")[0] == "Reboot failed"
    assert menu.lines("0.6.5")[1] == "No permission"
    assert menu.leds() == (False, True)
