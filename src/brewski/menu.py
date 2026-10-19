"""Software menu on the LCD.

A long press of Stop opens it while the still is idle or cooling. The
target dial moves the highlight, Start chooses the line, and Stop goes back.
"""

from __future__ import annotations

from brewski.lcd import clip_line
from brewski.updates import ReleaseInfo

ITEMS = ("about", "update", "reboot")
LABELS = {
    "about": "About",
    "update": "Check for Update",
    "reboot": "Reboot",
}


class SoftwareMenu:
    """Which row is highlighted, and which screen is on the display."""

    def __init__(self) -> None:
        self.screen = "list"
        self.index = 0
        self.release: ReleaseInfo | None = None
        self.can_apply = False
        self.failed = False

    def move(self, steps: int) -> bool:
        """Move the highlight by every dial step accumulated since the last paint."""

        if self.screen != "list" or not steps:
            return False
        self.index = (self.index + int(steps)) % len(ITEMS)
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

    def lines(self, version: str) -> list[str]:
        if self.screen == "about":
            rows = ["About", "Brewski", version, "STOP back"]
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
            rows = ["Software Menu"]
            for index, item in enumerate(ITEMS):
                mark = ">" if index == self.index else " "
                rows.append(f"{mark} {LABELS[item]}")
        return [clip_line(row) for row in rows]

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
            return ["Check for Update", "Up to date", release.current or version, "STOP back"]
        current = release.current or version
        latest = release.latest or ""
        action = "START install" if self.can_apply else "Cannot install here"
        return ["Update available", f"{current} -> {latest}", action, "STOP back"]
