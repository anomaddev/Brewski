"""20×4 I2C LCD, the boot-time buttons, and the LED relays.

The display stays closed until setup has saved the pins. The Pi's hardware
I2C controller is ``/dev/i2c-1`` on BCM 2 (SDA) and BCM 3 (SCL). Any other
saved pair is left unused. The backpack address is probed, not stored.

The button pins and the LED relay pins are different. Start is a normally
open contact to ground, so its pin sits high and a press is low. Stop is a
normally closed contact to ground, so its pin sits low and a press lets the
10 kΩ pull-up take it high. The internal pull-up stays on for Start only.
Each LED is switched by its own relay.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Protocol

from brewski.press import PRESS_BOUNCE_SECONDS, PressPair
from brewski.store import Settings, default_gpio

logger = logging.getLogger(__name__)

COLUMNS = 20
ROWS = 4
# Hardware I2C on every Pi header. Other BCM pairs need a software bus.
HARDWARE_SDA = 2
HARDWARE_SCL = 3
I2C_BUS = 1
BACKPACK_ADDRESSES = (0x27, 0x3F)
# LED relay modules, on the saved LED pins, not the button pins. Those
# boards energize when the pin is low, so "on" has to drive the pin low.
LED_ACTIVE_HIGH = False
# Both buttons have a 10 kΩ from 3.3 V to the GPIO. Start's normally open
# contact shorts that pin to ground, so the internal pull-up stays on and a
# press is low. Stop's normally closed contact already shorts its pin, and a
# press opens it, so a press is high. gpiozero 2.0 raises if active_state is
# set while pull_up is True or False, and that exception closes the whole
# panel before the lamps open. Stop leaves the pin floating and sets
# active_state instead. The 10 kΩ is the pull-up. There is no capacitor.
# gpiozero's bounce timer is left off; PressPair ignores a spike shorter than
# PRESS_BOUNCE_SECONDS.
BUTTON_BOUNCE_SECONDS = PRESS_BOUNCE_SECONDS
STOP_ACTIVE_STATE = True
_SYSTEM_LGPIO = "/usr/lib/python3/dist-packages"

_logged: set[str] = set()
_pin_factory: object | None = None
_pin_factory_failed = False


class Display(Protocol):
    @property
    def hardware(self) -> bool: ...

    def write(self, lines: list[str]) -> None: ...

    def clear(self) -> None: ...

    def close(self, *, clear: bool = True) -> None: ...


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

    def close(self, *, clear: bool = True) -> None:
        self.closed = True
        self.closed_clear = clear


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
        previous = self._last
        if framed == previous:
            return
        # A target change used to repaint all four rows. The I2C backpack
        # is slow enough that the number then fell behind the dial.
        for row, line in enumerate(framed):
            if previous is not None and previous[row] == line:
                continue
            self._lcd.cursor_pos = (row, 0)  # type: ignore[attr-defined]
            self._lcd.write_string(f"{line:<{COLUMNS}}")  # type: ignore[attr-defined]
        self._last = framed

    def clear(self) -> None:
        if self._closed:
            return
        self._lcd.clear()  # type: ignore[attr-defined]
        self._last = [""] * ROWS

    def close(self, *, clear: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._lcd.close(clear=clear)  # type: ignore[attr-defined]
        except TypeError:
            self._lcd.close()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Could not close the LCD")


class ButtonReader:
    """Start and stop buttons wired to ground, held high by a 10 kΩ to 3.3 V.

    The first completed tap wins. A button already held when the prompt opens
    does not count, so a stuck switch cannot accept the update by itself.
    A blip on the shared ground does not count as the other button.
    """

    def __init__(self, start: object, stop: object) -> None:
        self._start = start
        self._stop = stop
        self._choice: str | None = None
        self._pair = PressPair(start, stop, hold_seconds=None)
        start.when_pressed = self._start_down  # type: ignore[attr-defined]
        start.when_released = self._start_up  # type: ignore[attr-defined]
        stop.when_pressed = self._stop_down  # type: ignore[attr-defined]
        stop.when_released = self._stop_up  # type: ignore[attr-defined]

    def _start_down(self) -> None:
        self._pair.down("start", time.monotonic())

    def _start_up(self) -> None:
        self._pair.up("start", time.monotonic())

    def _stop_down(self) -> None:
        self._pair.down("stop", time.monotonic())

    def _stop_up(self) -> None:
        self._pair.up("stop", time.monotonic())

    def pressed(self) -> str | None:
        if self._choice is not None:
            return self._choice
        now = time.monotonic()
        self._pair.poll(now)
        starts, stops, _holds = self._pair.consume()
        if stops:
            self._choice = "stop"
        elif starts:
            self._choice = "start"
        return self._choice

    def close(self) -> None:
        self._pair.close()
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


class PromptLeds:
    """Hold the start and stop indicators solid on while a prompt is open."""

    def __init__(self, start: object, stop: object) -> None:
        self._start = start
        self._stop = stop
        self._shown: tuple[bool, bool] | None = None
        self.set(True, True)

    def set(self, start_on: bool, stop_on: bool) -> None:
        shown = (bool(start_on), bool(stop_on))
        previous = self._shown
        if shown == previous:
            return
        self._shown = shown
        # Write only the lamp that changed. Restating a pin makes that relay
        # click. Stop is written first so a later Start write is the level
        # that stays on the pin.
        if previous is None or previous[1] != shown[1]:
            _drive_led(self._stop, shown[1])
        if previous is None or previous[0] != shown[0]:
            _drive_led(self._start, shown[0])

    def close(self) -> None:
        for led in (self._start, self._stop):
            try:
                led.off()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Could not turn off a panel LED")
            try:
                led.close()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Could not release a panel LED")


def _drive_led(led: object, on: bool) -> None:
    try:
        if on:
            led.on()  # type: ignore[attr-defined]
        else:
            led.off()  # type: ignore[attr-defined]
    except Exception:
        logger.exception("Could not change a panel LED")


def open_buttons(settings: Settings) -> ButtonReader | None:
    """Read the saved start and stop pins. None when they cannot be opened."""

    if not settings.setup_complete:
        return None
    gpio = settings.gpio or default_gpio()
    factory = panel_pin_factory()
    if factory is None:
        _log_once("buttons", "Start and stop buttons could not be opened: lgpio is unavailable.")
        return None
    try:
        from gpiozero import Button
    except ImportError:
        _log_once("gpiozero", "gpiozero is not installed. Panel buttons left unread.")
        return None
    start = stop = None
    try:
        start = Button(
            gpio.button_start,
            pull_up=True,
            bounce_time=None,
            pin_factory=factory,
        )
        stop = Button(
            gpio.button_stop,
            pull_up=None,
            active_state=STOP_ACTIVE_STATE,
            bounce_time=None,
            pin_factory=factory,
        )
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


def open_prompt_leds(settings: Settings) -> PromptLeds | None:
    """Turn the saved start and stop LED relays solid on. None when they cannot be opened."""

    if not settings.setup_complete:
        return None
    gpio = settings.gpio or default_gpio()
    factory = panel_pin_factory()
    if factory is None:
        _log_once("leds", "Start and stop LEDs were left off because lgpio is unavailable.")
        return None
    try:
        from gpiozero import LED
    except ImportError:
        _log_once("gpiozero-led", "gpiozero is not installed. Panel LEDs left off.")
        return None
    start = stop = None
    try:
        start = LED(
            gpio.led_start,
            active_high=LED_ACTIVE_HIGH,
            initial_value=True,
            pin_factory=factory,
        )
        stop = LED(
            gpio.led_stop,
            active_high=LED_ACTIVE_HIGH,
            initial_value=True,
            pin_factory=factory,
        )
        return PromptLeds(start, stop)
    except Exception as exc:
        _log_once("leds", "Start and stop LEDs could not be opened: %s", exc)
        for led in (start, stop):
            if led is not None:
                try:
                    led.close()
                except Exception:
                    pass
        return None


def panel_pin_factory() -> object | None:
    """The lgpio pin factory, shared so buttons and LEDs open one gpiochip."""

    global _pin_factory, _pin_factory_failed
    if _pin_factory is not None:
        return _pin_factory
    if _pin_factory_failed:
        return None
    if not _ensure_lgpio():
        _pin_factory_failed = True
        return None
    try:
        from gpiozero.pins.lgpio import LGPIOFactory

        _pin_factory = LGPIOFactory()
    except Exception as exc:
        _log_once("factory", "Could not open the lgpio pin factory: %s", exc)
        _pin_factory_failed = True
        return None
    return _pin_factory


def _lgpio_directory() -> Path | None:
    """A directory the current user can write, where lgpio can create its pipe."""

    from brewski.paths import data_directory

    for folder in (data_directory(False), Path("/tmp")):
        if folder.is_dir() and os.access(folder, os.W_OK | os.X_OK):
            return folder
    return None


def _load_lgpio_module():
    import lgpio

    return lgpio


def _import_lgpio():
    """Import lgpio from a writable directory, then restore the previous cwd.

    The library creates a notify pipe in the current directory while it is
    imported. systemd starts this service in ``/``. The service account cannot
    create that pipe there, the import raises, and the new process exits. The
    files from the update are on disk, but the controller stays down until a
    later start happens to get past it.
    """

    folder = _lgpio_directory()
    previous = None
    if folder is not None:
        try:
            previous = os.getcwd()
        except OSError:
            previous = None
        os.chdir(folder)
    try:
        return _load_lgpio_module()
    except Exception:
        return None
    finally:
        if previous is not None:
            try:
                os.chdir(previous)
            except OSError:
                pass


def _ensure_lgpio() -> bool:
    """Import lgpio from the venv, or from Raspberry Pi OS if the venv hid it.

    gpiozero's own fallback is the legacy /dev/gpiomem interface. On current
    Pi OS that open returns EINVAL, so the panel pins never come up.
    """

    if _import_lgpio() is not None:
        return True
    if not os.path.isdir(_SYSTEM_LGPIO):
        return False
    if _SYSTEM_LGPIO not in sys.path:
        sys.path.append(_SYSTEM_LGPIO)
    return _import_lgpio() is not None


def frame(lines: list[str]) -> list[str]:
    """Four lines, each clipped to the width of the panel."""

    clipped = [clip_line(line) for line in lines[:ROWS]]
    while len(clipped) < ROWS:
        clipped.append("")
    return clipped


def clip_line(text: str) -> str:
    """Keep interior spaces so a right-aligned value stays in the last column."""

    return str(text).replace("\n", " ")[:COLUMNS]


def status_lines(
    *,
    mode: str,
    elapsed_seconds: float,
    target_f: float,
    probes: list[tuple[str, float | None]],
    heaters: list[tuple[bool, bool]],
    calibrating: bool = False,
    needs_calibration: bool = False,
    emergency_code: str | None = None,
    cabinet_temp_f: float | None = None,
    cabinet_limit_f: int = 170,
    cabinet_fault: bool = False,
    calibration_step: str | None = None,
    calibration_rise_f: float | None = None,
    calibration_peak_f: float | None = None,
    calibration_rate_f: float | None = None,
    eta_seconds: float | None = None,
    eta_kind: str | None = None,
) -> list[str]:
    """Four 20-column lines. Labels sit on the left and values on the right."""

    heater_row = _heater_line(heaters)
    probe_row = _probe_line(probes)
    if emergency_code in {"E01", "E02"}:
        cabinet = None if cabinet_fault or cabinet_temp_f is None else cabinet_temp_f
        return [
            _pair("Emergency:", emergency_code),
            _value_line("Limit:", f"{int(cabinet_limit_f)} F"),
            _reading_line("Cabinet", cabinet),
            heater_row,
        ]
    if emergency_code:
        return [
            _pair("Emergency:", emergency_code),
            _value_line("Target:", _whole_temp(target_f)),
            _probe_line(probes),
            heater_row,
        ]
    clock = format_clock(elapsed_seconds)
    if mode == "cooling" and calibration_peak_f is not None:
        if calibration_rate_f is None:
            result = _pair("Coast:", _signed_degrees(calibration_peak_f))
        else:
            result = _pair(_signed_degrees(calibration_peak_f), _degrees_per_min(calibration_rate_f))
        return [_pair("Cooling:", clock), result, probe_row, heater_row]
    if calibrating and mode == "paused":
        return [
            _pair("Paused:", clock),
            _pair("Step:", _calibration_step_name(calibration_step)),
            probe_row,
            heater_row,
        ]
    if calibrating and mode == "running":
        return [
            *_calibration_head(calibration_step, clock, target_f, calibration_rise_f, calibration_peak_f, calibration_rate_f),
            probe_row,
            heater_row,
        ]
    if needs_calibration and mode == "idle":
        head = _pair("Calibrate:", "START")
    elif mode in {"running", "paused", "cooling"}:
        label = _mode_label(mode, calibrating)
        head = _pair(label, clock)
    else:
        head = "Idle:"
    if eta_seconds is None:
        target_line = _value_line("Target:", _whole_temp(target_f))
    else:
        target_line = _pair(_whole_temp(target_f), _eta_label(eta_seconds, eta_kind))
    return [
        head,
        target_line,
        probe_row,
        heater_row,
    ]


def _calibration_step_name(step: str | None) -> str:
    return {"coasting": "Coasting", "falling": "Falling"}.get(step or "", "Heating")


def _calibration_head(
    step: str | None,
    clock: str,
    target_f: float,
    rise_f: float | None,
    peak_f: float | None,
    rate_f: float | None,
) -> list[str]:
    if step == "coasting":
        return [_pair("Coasting:", clock), _pair("Rise:", _signed_degrees(rise_f))]
    if step == "falling":
        if rate_f is None:
            detail = _pair("Peak:", _signed_degrees(peak_f))
        else:
            detail = _pair("Rate:", _degrees_per_min(rate_f))
        return [_pair("Falling:", clock), detail]
    return [_pair("Heating:", clock), _value_line("Target:", _whole_temp(target_f))]


def _signed_degrees(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.1f} F"


def _degrees_per_min(value: float) -> str:
    return f"{float(value):.1f} F/min"


def _mode_label(mode: str, calibrating: bool) -> str:
    if calibrating and mode in {"running", "paused"}:
        return "Calibrate:"
    return {"running": "Running:", "paused": "Paused:", "cooling": "Cooling:"}.get(mode, "Brewski:")


def _pair(left: str, right: str) -> str:
    """One line with ``left`` flush left and ``right`` flush right."""

    left = str(left)
    right = str(right)
    gap = COLUMNS - len(left) - len(right)
    if gap < 1:
        left = left[: max(0, COLUMNS - len(right) - 1)]
        gap = COLUMNS - len(left) - len(right)
    return f"{left}{' ' * gap}{right}"[:COLUMNS]


def _value_line(label: str, value: str) -> str:
    return _pair(label, value)


def _reading_line(name: str, temp_f: float | None) -> str:
    value = "FAULT" if temp_f is None else f"{format_temp(temp_f)} F"
    return _pair(f"{name}:", value)


def _probe_line(probes: list[tuple[str, float | None]]) -> str:
    enabled = list(probes[:2])
    if len(enabled) >= 2:
        left = _compact_probe(1, enabled[0][1])
        right = _compact_probe(2, enabled[1][1])
        gap = COLUMNS - len(left) - len(right)
        return f"{left}{' ' * max(gap, 1)}{right}"[:COLUMNS]
    if enabled:
        return _reading_line(enabled[0][0], enabled[0][1])
    return _reading_line("Probe", None)


def _compact_probe(index: int, temp_f: float | None) -> str:
    if temp_f is None:
        return f"T{index}:FAULT"
    return f"T{index}:{format_temp(temp_f)}F"


def _heater_line(heaters: list[tuple[bool, bool]]) -> str:
    """Both heater slots. ``ON`` is padded so ``H1`` and ``H2`` stay put."""

    slots = list(heaters[:2])
    while len(slots) < 2:
        slots.append((False, False))
    left = _heater_token(1, slots[0][0], slots[0][1])
    right = _heater_token(2, slots[1][0], slots[1][1])
    return _pair(left, right)


def _heater_token(index: int, enabled: bool, on: bool) -> str:
    state = "ON " if enabled and on else "OFF"
    return f"H{index}: {state}"


def _whole_temp(value: float) -> str:
    return f"{int(round(float(value)))} F"


def _eta_label(seconds: float, kind: str | None) -> str:
    """Short clock for a 20-column line. ``cool 8m`` is a fall; ``~12m`` is a rise."""

    total = max(0, int(round(float(seconds))))
    if total >= 3600:
        hours, minutes = divmod(total // 60, 60)
        clock = f"{hours}h" if minutes == 0 else f"{hours}h{minutes}m"
    elif total >= 60:
        clock = f"{total // 60}m"
    else:
        clock = f"{total}s"
    if kind == "cool":
        return f"cool {clock}"
    return f"~{clock}"


def startup_lines(step: str) -> list[str]:
    """Boot status. The step sits under the name until the live screen takes over."""

    return frame(["Brewski", step, "", ""])


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
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_temp(value: float) -> str:
    return f"{float(value):.1f}"


def _log_once(key: str, message: str, *args: object) -> None:
    if key in _logged:
        return
    _logged.add(key)
    logger.warning(message, *args)
