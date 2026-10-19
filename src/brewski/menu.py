"""Software menu on the LCD.

A long press of Stop opens it while the still is idle or cooling. The
target dial moves the highlight, Start chooses the line, and Stop goes back.
"""

from __future__ import annotations

from brewski.lcd import COLUMNS, _pair, clip_line
from brewski.updates import ReleaseInfo, about_identity, offer_ends

# The panel is four rows. The title stays put and the list under it scrolls.
VISIBLE = 3
ITEMS = ("about", "networking", "update", "reboot")
LABELS = {
    "about": "About",
    "networking": "Networking",
    "update": "Check for Update",
    "reboot": "Reboot",
}


class SoftwareMenu:
    """Which row is highlighted, and which screen is on the display."""

    def __init__(self) -> None:
        self.screen = "list"
        self.index = 0
        self.top = 0
        self.release: ReleaseInfo | None = None
        self.can_apply = False
        self.failed = False

    def move(self, steps: int) -> bool:
        """Move the highlight by every dial step accumulated since the last paint."""

        if self.screen != "list" or not steps:
            return False
        count = len(ITEMS)
        index = self.index + int(steps)
        if index < 0:
            index = 0
        elif index >= count:
            index = count - 1
        if index == self.index:
            return False
        self.index = index
        self._reveal()
        return True

    def back(self) -> bool:
        """Leave the current screen. False means the menu itself should close."""

        if self.screen in {"list", "rebooting"}:
            return self.screen == "rebooting"
        self.screen = "list"
        self.release = None
        self.can_apply = False
        self.failed = False
        return True

    def select(self) -> str | None:
        """Choose the highlighted line or confirm the current screen.

        Returns ``check``, ``apply``, or ``reboot`` when the controller has
        to do something. About, and a result with nothing to confirm, return
        None.
        """

        if self.screen == "list":
            item = ITEMS[self.index]
            if item == "about":
                self.screen = "about"
                return None
            if item == "networking":
                self.screen = "networking"
                return None
            if item == "update":
                self.screen = "checking"
                self.release = None
                self.can_apply = False
                return "check"
            self.screen = "reboot"
            self.failed = False
            return None
        if self.screen == "update" and self._can_install():
            return "apply"
        if self.screen == "reboot" and not self.failed:
            self.screen = "rebooting"
            return "reboot"
        return None

    def show_release(self, release: ReleaseInfo, can_apply: bool) -> None:
        self.release = release
        self.can_apply = can_apply
        self.screen = "update"

    def fail_reboot(self) -> None:
        self.screen = "reboot"
        self.failed = True

    def leds(self) -> tuple[bool, bool]:
        """Start LED, then stop LED. On only while that button will do something."""

        if self.screen == "rebooting":
            return False, False
        start_on = self.screen == "list" or self._can_install() or (
            self.screen == "reboot" and not self.failed
        )
        return start_on, True

    def lines(
        self,
        version: str,
        *,
        address: str = "",
        hostname: str = "",
        update_source: str = "main",
        revision: str | None = None,
    ) -> list[str]:
        if self.screen == "about":
            rows = ["About", "Brewski", about_line(update_source, version, revision), "STOP back"]
        elif self.screen == "networking":
            rows = network_lines(address, hostname)
        elif self.screen == "checking":
            rows = ["Check for Update", "Checking...", "", "STOP cancel"]
        elif self.screen == "update":
            rows = self._update_lines(version)
        elif self.screen == "rebooting":
            rows = ["Rebooting", "", "", ""]
        elif self.screen == "reboot" and self.failed:
            rows = ["Reboot failed", "No permission", "", "STOP back"]
        elif self.screen == "reboot":
            rows = ["Reboot the Pi?", "START reboot", "STOP cancel", ""]
        else:
            rows = self._list_lines()
        return [clip_line(row) for row in rows]

    def _reveal(self) -> None:
        """Slide the window so the highlight stays on the three item rows."""

        visible = min(VISIBLE, len(ITEMS))
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + visible:
            self.top = self.index - visible + 1

    def _list_lines(self) -> list[str]:
        self._reveal()
        visible = min(VISIBLE, len(ITEMS))
        hint = scroll_hint(self.top, visible, len(ITEMS))
        rows = [menu_title(hint)]
        for offset in range(visible):
            index = self.top + offset
            mark = ">" if index == self.index else " "
            rows.append(f"{mark} {LABELS[ITEMS[index]]}")
        while len(rows) < 4:
            rows.append("")
        return rows

    def _can_install(self) -> bool:
        release = self.release
        return bool(
            self.screen == "update"
            and release is not None
            and release.available
            and release.reachable
            and self.can_apply
        )

    def _update_lines(self, version: str) -> list[str]:
        release = self.release
        if release is None or not release.reachable:
            return ["Check for Update", "GitHub unreachable", "", "STOP back"]
        if not release.available:
            left, _right = offer_ends(release)
            return ["Check for Update", "Up to date", left or version, "STOP back"]
        left, right = offer_ends(release)
        if not left:
            left = version
        action = "START install" if self.can_apply else "Cannot install here"
        return ["Update available", f"{left} -> {right}", action, "STOP back"]


def scroll_hint(top: int, visible: int, count: int) -> str:
    """A mark for rows above the window, rows below it, or both."""

    above = top > 0
    below = top + visible < count
    if above and below:
        return "^v"
    if above:
        return "^"
    if below:
        return "v"
    return ""


def menu_title(hint: str) -> str:
    left = "Software Menu"
    if not hint:
        return left
    gap = COLUMNS - len(left) - len(hint)
    if gap < 1:
        return clip_line(f"{left} {hint}")
    return f"{left}{' ' * gap}{hint}"


def about_line(source: str, version: str, revision: str | None = None) -> str:
    """One LCD row: the commit on beta, or the release on main."""

    label, value = about_identity(source, version, revision)
    if not value:
        return label
    return f"{label} {value}"


def network_lines(address: str, hostname: str) -> list[str]:
    """IP and hostname. The address line includes the port when it fits."""

    shown = (address or "").strip() or "No address"
    return ["Networking", shown, _host_line(hostname), "STOP back"]


def _host_line(hostname: str) -> str:
    name = (hostname or "").strip() or "—"
    room = COLUMNS - len("Host:") - 1
    if room < 1:
        return clip_line(name)
    return _pair("Host:", name[:room])
