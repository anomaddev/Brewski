"""20×4 I2C LCD and the boot-time start and stop buttons.

The display stays closed until setup has saved the pins. The Pi's hardware
I2C controller is ``/dev/i2c-1`` on BCM 2 (SDA) and BCM 3 (SCL). Any other
saved pair is left unused. The backpack address is probed, not stored.
"""

from __future__ import annotations

import logging
from typing import Protocol

from brewski.store import Settings, default_gpio

logger = logging.getLogger(__name__)

COLUMNS = 20
ROWS = 4
# Hardware I2C on every Pi header. Other BCM pairs need a software bus.
HARDWARE_SDA = 2
HARDWARE_SCL = 3
I2C_BUS = 1
BACKPACK_ADDRESSES = (0x27, 0x3F)

_logged: set[str] = set()


class Display(Protocol):
    @property
    def hardware(self) -> bool: ...

    def write(self, lines: list[str]) -> None: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...


class NullDisplay:
    """Stand-in for dev, tests, a disabled panel, and a display that did not answer."""

    def __init__(self) -> None:
        self.lines: list[str] = [""] * ROWS
        self.cleared = False
        self.closed = False

    @property
    def hardware(self) -> bool:
        return False

    def write(self, lines: list[str]) -> None:
        self.lines = frame(lines)
        self.closed = False

    def clear(self) -> None:
        self.lines = [""] * ROWS
        self.cleared = True

    def close(self) -> None:
        self.closed = True


class HardwareDisplay:
    def __init__(self, lcd: object) -> None:
        self._lcd = lcd
        self._last: list[str] | None = None
        self._closed = False

    @property
    def hardware(self) -> bool:
        return True

    def write(self, lines: list[str]) -> None:
        if self._closed:
            return
        framed = frame(lines)
        if framed == self._last:
            return
        for row, line in enumerate(framed):
            self._lcd.cursor_pos = (row, 0)  # type: ignore[attr-defined]
            self._lcd.write_string(f"{line:<{COLUMNS}}")  # type: ignore[attr-defined]
        self._last = framed

    def clear(self) -> None:
        if self._closed:
            return
        self._lcd.clear()  # type: ignore[attr-defined]
        self._last = [""] * ROWS

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._lcd.close(clear=True)  # type: ignore[attr-defined]
        except TypeError:
            self._lcd.close()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Could not close the LCD")


class ButtonReader:
    """Active-low start and stop buttons, using the Pi's internal pull-ups.

    The first new press wins. A button already held when the prompt opens
    does not count, so a stuck switch cannot accept the update by itself.
    """

    def __init__(self, start: object, stop: object) -> None:
        self._start = start
        self._stop = stop
        self._choice: str | None = None
        start.when_pressed = self._press_start  # type: ignore[attr-defined]
        stop.when_pressed = self._press_stop  # type: ignore[attr-defined]

    def _press_start(self) -> None:
        self._choose("start")

    def _press_stop(self) -> None:
        self._choose("stop")

    def _choose(self, choice: str) -> None:
        if self._choice is None:
            self._choice = choice

    def pressed(self) -> str | None:
        return self._choice

    def close(self) -> None:
        for button in (self._start, self._stop):
            try:
                button.close()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Could not release a panel button")


def display_key(settings: Settings) -> tuple[int, int] | None:
    """Pins that should own the display, or None while it stays closed."""

    if not settings.setup_complete:
        return None
    gpio = settings.gpio or default_gpio()
    if not gpio.lcd_enabled():
        return None
    return (gpio.i2c_sda, gpio.i2c_scl)


def open_display(settings: Settings) -> Display:
    """Open the LCD on the saved pins. Failure returns a null display."""

    key = display_key(settings)
    if key is None:
        return NullDisplay()
    sda, scl = key
    if (sda, scl) != (HARDWARE_SDA, HARDWARE_SCL):
        _log_once(
            "pins",
            "LCD I2C pins BCM %s and %s are not the hardware bus (BCM 2 and 3). Display left closed.",
            sda,
            scl,
        )
        return NullDisplay()
    try:
        from RPLCD.i2c import CharLCD
    except ImportError:
        _log_once("library", "LCD library is not installed. Display left closed.")
        return NullDisplay()
    last_error: Exception | None = None
    for address in BACKPACK_ADDRESSES:
        try:
            lcd = CharLCD(
                i2c_expander="PCF8574",
                address=address,
                port=I2C_BUS,
                cols=COLUMNS,
                rows=ROWS,
                auto_linebreaks=False,
                backlight_enabled=True,
            )
            logger.info("LCD opened at I2C address %#x", address)
            return HardwareDisplay(lcd)
        except Exception as exc:
            last_error = exc
    _log_once("probe", "LCD did not respond at 0x27 or 0x3F: %s", last_error)
    return NullDisplay()


def open_buttons(settings: Settings) -> ButtonReader | None:
    """Read the saved start and stop pins. None when they cannot be opened."""

    if not settings.setup_complete:
        return None
    gpio = settings.gpio or default_gpio()
    try:
        from gpiozero import Button
    except ImportError:
        _log_once("gpiozero", "gpiozero is not installed. Panel buttons left unread.")
        return None
    start = stop = None
    try:
        start = Button(gpio.button_start, pull_up=True, bounce_time=0.05)
        stop = Button(gpio.button_stop, pull_up=True, bounce_time=0.05)
        return ButtonReader(start, stop)
    except Exception as exc:
        _log_once("buttons", "Start and stop buttons could not be opened: %s", exc)
        for button in (start, stop):
            if button is not None:
                try:
                    button.close()
                except Exception:
                    pass
        return None


def frame(lines: list[str]) -> list[str]:
    """Four lines, each clipped to the width of the panel."""

    clipped = [clip_line(line) for line in lines[:ROWS]]
    while len(clipped) < ROWS:
        clipped.append("")
    return clipped


def clip_line(text: str) -> str:
    return " ".join(str(text).split())[:COLUMNS]


def status_lines(
    *,
    mode: str,
    elapsed_seconds: float,
    probe_name: str,
    temp_f: float | None,
    target_f: float,
    heaters: list[tuple[int, bool]],
) -> list[str]:
    """Live still screen. A clock is shown while a run or a cooldown is open."""

    label = {"idle": "Idle", "running": "Running", "paused": "Paused", "cooling": "Cooling"}.get(
        mode, "Brewski"
    )
    if mode in {"running", "paused", "cooling"}:
        head = f"{label} {format_clock(elapsed_seconds)}"
    else:
        head = label
    return [
        head,
        _temp_line(probe_name, temp_f),
        f"Target {format_temp(target_f)} F",
        _heater_line(heaters),
    ]


def prompt_lines(current: str, latest: str, seconds_left: int) -> list[str]:
    return [
        "Update available",
        f"{current} -> {latest}",
        "START update",
        f"STOP keep {max(0, int(seconds_left))}s",
    ]


def progress_lines(phase: str, current: str, latest: str) -> list[str]:
    if phase == "failed":
        return ["Update failed", f"Keeping {current}", "Starting", ""]
    if phase == "complete":
        return ["Update complete", latest or current, "Starting", ""]
    if phase == "kept":
        return ["Keeping this copy", current, "Starting", ""]
    title = {"downloading": "Downloading", "building": "Building", "installing": "Installing"}.get(
        phase, "Updating"
    )
    return ["Updating Brewski", title, latest or current, ""]


def format_clock(seconds: float) -> str:
    total = max(0, int(float(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_temp(value: float) -> str:
    number = round(float(value), 1)
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _temp_line(probe_name: str, temp_f: float | None) -> str:
    if temp_f is None:
        return "No probe"
    suffix = f" {format_temp(temp_f)} F"
    room = COLUMNS - len(suffix)
    name = probe_name[:room].rstrip() if room > 0 else ""
    if not name:
        return suffix.strip()
    return f"{name}{suffix}"


def _heater_line(heaters: list[tuple[int, bool]]) -> str:
    if not heaters or not any(on for _, on in heaters):
        return "Heaters off"
    return "  ".join(f"H{index + 1} {'on' if on else 'off'}" for index, on in heaters)


def _log_once(key: str, message: str, *args: object) -> None:
    if key in _logged:
        return
    _logged.add(key)
    logger.warning(message, *args)
