"""Software menu on the LCD.

A long press of Stop opens it while the still is idle or cooling. The
target dial moves the highlight, Start chooses the line, and Stop goes back.
"""

from __future__ import annotations

from brewski.lcd import COLUMNS, _pair, clip_line
from brewski.program import MAX_PROGRAM_HOURS, PROGRAM_MODES
from brewski.store import TARGET_MAX_F, TARGET_MIN_F
from brewski.suite import TEST_INITIAL_HEATING, TEST_LABEL
from brewski.updates import ReleaseInfo, about_identity, offer_ends

# The panel is four rows. The title stays put and the list under it scrolls.
VISIBLE = 3
ITEMS = ("program", "about", "networking", "suite", "update", "reboot")
LABELS = {
    "program": "Program",
    "about": "About",
    "networking": "Networking",
    "suite": "Test Suite",
    "update": "Check for Update",
    "reboot": "Reboot",
}
PROGRAM_LABELS = {"hold": "Hold"}
HOLD_FIELDS = ("target", "hours", "minutes", "seconds", "continuous")
HOLD_LABELS = {
    "target": "Target",
    "hours": "Hours",
    "minutes": "Minutes",
    "seconds": "Seconds",
    "continuous": "Continuous",
}
TESTS = (TEST_INITIAL_HEATING,)
TEST_LABELS = {TEST_INITIAL_HEATING: TEST_LABEL}


class SoftwareMenu:
    """Which row is highlighted, and which screen is on the display."""

    def __init__(self) -> None:
        self.screen = "list"
        self.index = 0
        self.top = 0
        self.release: ReleaseInfo | None = None
        self.can_apply = False
        self.failed = False
        self.suite_index = 0
        self.suite_top = 0
        self.chosen_test = TEST_INITIAL_HEATING
        self.notice: tuple[str, str] = ("", "")
        self.program_index = 0
        self.program_top = 0
        self.hold_index = 0
        self.hold_top = 0
        self.editing = False
        self.draft = 0
        self.fault = ""
        self.target = 165
        self.continuous = True
        self.hours = 1
        self.minutes = 0
        self.seconds = 0

    def move(self, steps: int) -> bool:
        """Move the highlight by every dial step accumulated since the last paint."""

        if not steps:
            return False
        if self.screen == "list":
            return self._slide(len(ITEMS), steps, "index", "top")
        if self.screen == "suite":
            return self._slide(len(TESTS), steps, "suite_index", "suite_top")
        if self.screen == "program":
            return self._slide(len(PROGRAM_MODES), steps, "program_index", "program_top")
        if self.screen == "hold" and not self.editing:
            self.fault = ""
            return self._slide(len(HOLD_FIELDS), steps, "hold_index", "hold_top")
        return False

    def back(self) -> bool:
        """Leave the current screen. False means the menu itself should close."""

        if self.screen in {"list", "rebooting"}:
            return self.screen == "rebooting"
        if self.screen == "suite-confirm":
            self.screen = "suite"
            return True
        if self.screen == "hold" and self.editing:
            self.editing = False
            return True
        if self.screen == "hold":
            self.screen = "program"
            self.fault = ""
            return True
        self.fault = ""
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
            if item == "program":
                self.screen = "program"
                self.program_index = 0
                self.program_top = 0
                return None
            if item == "about":
                self.screen = "about"
                return None
            if item == "networking":
                self.screen = "networking"
                return None
            if item == "suite":
                self.screen = "suite"
                self.suite_index = 0
                self.suite_top = 0
                return None
            if item == "update":
                self.screen = "checking"
                self.release = None
                self.can_apply = False
                return "check"
            self.screen = "reboot"
            self.failed = False
            return None
        if self.screen == "suite":
            self.chosen_test = TESTS[self.suite_index]
            self.screen = "suite-confirm"
            return None
        if self.screen == "suite-confirm":
            return "test"
        if self.screen == "program":
            if PROGRAM_MODES[self.program_index] == "hold":
                self.screen = "hold"
                self.hold_index = 0
                self.hold_top = 0
                self.editing = False
                self.fault = ""
            return None
        if self.screen == "hold":
            return self._select_hold()
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

    def show_notice(self, heading: str, detail: str = "") -> None:
        """A short refusal. Stop returns to the software list."""

        self.screen = "notice"
        self.notice = (heading, detail)

    def leds(self) -> tuple[bool, bool]:
        """Start LED, then stop LED. On only while that button will do something."""

        if self.screen == "rebooting":
            return False, False
        start_on = self.screen in {"list", "suite", "suite-confirm", "program", "hold"} or self._can_install() or (
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
        elif self.screen == "suite":
            rows = self._suite_lines()
        elif self.screen == "program":
            rows = self._program_lines()
        elif self.screen == "hold":
            rows = self._hold_lines()
        elif self.screen == "suite-confirm":
            label = TEST_LABELS.get(self.chosen_test, TEST_LABEL)
            rows = [f"{label}?", "On 3 min, off 3 min", "START begin", "STOP cancel"]
        elif self.screen == "notice":
            heading, detail = self.notice
            rows = [heading, detail, "", "STOP back"]
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

    def _slide(self, count: int, steps: int, index_name: str, top_name: str) -> bool:
        index = getattr(self, index_name) + int(steps)
        if index < 0:
            index = 0
        elif index >= count:
            index = count - 1
        if index == getattr(self, index_name):
            return False
        setattr(self, index_name, index)
        setattr(self, top_name, _window_top(index, getattr(self, top_name), count))
        return True

    def _reveal(self) -> None:
        """Slide the window so the highlight stays on the three item rows."""

        self.top = _window_top(self.index, self.top, len(ITEMS))

    def _list_lines(self) -> list[str]:
        self._reveal()
        return _menu_rows("Software Menu", ITEMS, LABELS, self.index, self.top)

    def _suite_lines(self) -> list[str]:
        self.suite_top = _window_top(self.suite_index, self.suite_top, len(TESTS))
        return _menu_rows("Test Suite", TESTS, TEST_LABELS, self.suite_index, self.suite_top)

    def _program_lines(self) -> list[str]:
        self.program_top = _window_top(self.program_index, self.program_top, len(PROGRAM_MODES))
        return _menu_rows("Program", PROGRAM_MODES, PROGRAM_LABELS, self.program_index, self.program_top)

    def load_program(self, target: int, continuous: bool, duration_seconds: int) -> None:
        """Show the program that is saved, unless a field is being edited."""

        self.target = int(target)
        self.continuous = bool(continuous)
        total = max(0, int(duration_seconds))
        self.hours, remainder = divmod(total, 3600)
        self.minutes, self.seconds = divmod(remainder, 60)

    def duration_seconds(self) -> int:
        return self.hours * 3600 + self.minutes * 60 + self.seconds

    def nudge(self, steps: int) -> bool:
        """Change the Hold field that Start opened. The dial does this."""

        if self.screen != "hold" or not self.editing or not steps:
            return False
        field = HOLD_FIELDS[self.hold_index]
        if field == "target":
            low, high = int(TARGET_MIN_F), int(TARGET_MAX_F)
        elif field == "hours":
            low, high = 0, MAX_PROGRAM_HOURS
        else:
            low, high = 0, 59
        draft = min(high, max(low, int(self.draft) + int(steps)))
        if draft == self.draft:
            return False
        self.draft = draft
        return True

    def _select_hold(self) -> str | None:
        field = HOLD_FIELDS[self.hold_index]
        if self.editing:
            self._commit_draft()
            self.editing = False
            return "program-save"
        if field == "continuous":
            self.continuous = not self.continuous
            return "program-save"
        self.editing = True
        self.draft = self._stored_field(field)
        self.fault = ""
        return None

    def _commit_draft(self) -> None:
        field = HOLD_FIELDS[self.hold_index]
        value = int(self.draft)
        if field == "target":
            self.target = value
        elif field == "hours":
            self.hours = value
        elif field == "minutes":
            self.minutes = value
        elif field == "seconds":
            self.seconds = value

    def _stored_field(self, field: str) -> int:
        if field == "target":
            return self.target
        if field == "hours":
            return self.hours
        if field == "minutes":
            return self.minutes
        return self.seconds

    def _field_text(self, field: str) -> str:
        if field == "continuous":
            return "On" if self.continuous else "Off"
        value = self.draft if self.editing and HOLD_FIELDS[self.hold_index] == field else self._stored_field(field)
        if field == "target":
            return f"{int(value)} F"
        return str(int(value))

    def _hold_lines(self) -> list[str]:
        self.hold_top = _window_top(self.hold_index, self.hold_top, len(HOLD_FIELDS))
        if self.fault:
            title = self.fault
        elif self.editing:
            title = "Editing"
        else:
            title = "Hold"
        visible = min(VISIBLE, len(HOLD_FIELDS))
        rows = [menu_title(scroll_hint(self.hold_top, visible, len(HOLD_FIELDS)), title)]
        for offset in range(visible):
            item_index = self.hold_top + offset
            field = HOLD_FIELDS[item_index]
            mark = ">" if item_index == self.hold_index else " "
            rows.append(_pair(f"{mark} {HOLD_LABELS[field]}", self._field_text(field)))
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


def _window_top(index: int, top: int, count: int) -> int:
    visible = min(VISIBLE, count)
    if index < top:
        return index
    if index >= top + visible:
        return index - visible + 1
    return top


def _menu_rows(
    title: str,
    items: tuple[str, ...],
    labels: dict[str, str],
    index: int,
    top: int,
) -> list[str]:
    visible = min(VISIBLE, len(items))
    hint = scroll_hint(top, visible, len(items))
    rows = [menu_title(hint, title)]
    for offset in range(visible):
        item_index = top + offset
        mark = ">" if item_index == index else " "
        rows.append(f"{mark} {labels[items[item_index]]}")
    while len(rows) < 4:
        rows.append("")
    return rows


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


def menu_title(hint: str, left: str = "Software Menu") -> str:
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
