"""Front-panel buttons, LED relays, and the target encoder.

The running controller keeps these open for the life of the process. A
button already held when the pins open does not count. LED relays are
active low and stay solid on only while that button will do something.
"""

from __future__ import annotations

import logging
import threading

from brewski.lcd import LED_ACTIVE_HIGH, PromptLeds, panel_pin_factory
from brewski.store import Settings, default_gpio

logger = logging.getLogger(__name__)


class Panel:
    """Presses and encoder steps collected outside the controller lock."""

    def __init__(self, start: object, stop: object, leds: PromptLeds, encoder: object | None) -> None:
        self._start_button = start
        self._stop_button = stop
        self._leds = leds
        self._encoder = encoder
        self._guard = threading.Lock()
        self._start_presses = 0
        self._stop_presses = 0
        self._steps = 0
        start.when_pressed = self._press_start  # type: ignore[attr-defined]
        stop.when_pressed = self._press_stop  # type: ignore[attr-defined]
        if encoder is not None:
            encoder.when_rotated_clockwise = self._clockwise  # type: ignore[attr-defined]
            encoder.when_rotated_counter_clockwise = self._counter   # type: ignore[attr-defined]
        self.set_leds(False, False)

    def _press_start(self) -> None:
        with self._guard:
            self._start_presses += 1

    def _press_stop(self) -> None:
        with self._guard:
            self._stop_presses += 1

    def _clockwise(self) -> None:
        self._add_steps(1)

    def _counter(self) -> None:
        self._add_steps(-1)

    def _add_steps(self, steps: int) -> None:
        with self._guard:
            self._steps += steps

    def take(self) -> tuple[int, int, int]:
        """Return new start presses, stop presses, and encoder steps, then clear them."""

        with self._guard:
            start, stop, steps = self._start_presses, self._stop_presses, self._steps
            self._start_presses = 0
            self._stop_presses = 0
            self._steps = 0
        return start, stop, steps

    def set_leds(self, start_on: bool, stop_on: bool) -> None:
        self._leds.set(start_on, stop_on)

    def close(self) -> None:
        self._leds.close()
        for device in (self._start_button, self._stop_button, self._encoder):
            if device is None:
                continue
            try:
                device.close()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Could not release a panel control")


def panel_key(settings: Settings) -> tuple[int, ...] | None:
    if not settings.setup_complete:
        return None
    gpio = settings.gpio or default_gpio()
    return (
        gpio.button_start,
        gpio.button_stop,
        gpio.led_start,
        gpio.led_stop,
        gpio.encoder_a,
        gpio.encoder_b,
    )


def indicator_leds(
    mode: str,
    *,
    emergency: bool,
) -> tuple[bool, bool]:
    """Start LED, then stop LED. On only while that button will do something."""

    if emergency:
        return False, False
    if mode == "running":
        return False, True
    if mode == "paused":
        return True, True
    return True, False


def open_panel(settings: Settings) -> Panel | None:
    """Open the buttons, both LED relays, and the encoder. None if the pins cannot be claimed."""

    if panel_key(settings) is None:
        return None
    gpio = settings.gpio or default_gpio()
    factory = panel_pin_factory()
    if factory is None:
        logger.warning("Panel left closed because lgpio is unavailable.")
        return None
    try:
        from gpiozero import Button, LED, RotaryEncoder
    except ImportError:
        logger.warning("gpiozero is not installed. Panel left closed.")
        return None
    start = stop = led_start = led_stop = encoder = None
    try:
        start = Button(gpio.button_start, pull_up=True, bounce_time=0.05, pin_factory=factory)
        stop = Button(gpio.button_stop, pull_up=True, bounce_time=0.05, pin_factory=factory)
        led_start = LED(gpio.led_start, active_high=LED_ACTIVE_HIGH, pin_factory=factory)
        led_stop = LED(gpio.led_stop, active_high=LED_ACTIVE_HIGH, pin_factory=factory)
        encoder = RotaryEncoder(
            gpio.encoder_a,
            gpio.encoder_b,
            max_steps=1_000_000,
            wrap=True,
            pin_factory=factory,
        )
        return Panel(start, stop, PromptLeds(led_start, led_stop), encoder)
    except Exception:
        logger.exception("Panel controls could not be opened")
        for device in (start, stop, led_start, led_stop, encoder):
            if device is None:
                continue
            try:
                device.close()
            except Exception:
                pass
        return None
