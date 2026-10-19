"""Software menu screens, and a Stop hold versus a short press."""

from brewski.menu import SoftwareMenu
from brewski.panel import STOP_HOLD_SECONDS, Panel
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
    panel = Panel(_Button(), stop, _Leds(), None)
    return panel, stop


def test_a_stop_release_is_a_short_press():
    panel, stop = _panel()
    assert stop.hold_time == STOP_HOLD_SECONDS
    stop.when_pressed()
    stop.when_released()
    assert panel.take() == (0, 1, 0, 0)


def test_a_stop_hold_is_not_a_short_press(monkeypatch):
    panel, stop = _panel()
    now = {"t": 1_000.0}
    monkeypatch.setattr("brewski.panel.time.monotonic", lambda: now["t"])
    stop.is_pressed = True
    stop.when_pressed()
    now["t"] += STOP_HOLD_SECONDS
    assert panel.take() == (0, 0, 1, 0)
    stop.is_pressed = False
    stop.when_released()
    assert panel.take() == (0, 0, 0, 0)


def test_a_short_release_does_not_leave_a_hold_behind(monkeypatch):
    panel, stop = _panel()
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
