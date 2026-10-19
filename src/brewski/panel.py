"""Front-panel buttons, LED relays, and the target encoder.

The running controller keeps these open for the life of the process. A
button already held when the pins open does not count. LED relays stay
solid on only while that button will do something, except while Stop is
being held: that lamp comes on when the hold is long enough and goes out
when the button does.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import NamedTuple

from brewski.lcd import LED_ACTIVE_HIGH, PromptLeds, panel_pin_factory
from brewski.press import STOP_HOLD_SECONDS, PressPair
from brewski.store import Settings, default_gpio

logger = logging.getLogger(__name__)

# After the finger lifts from a hold, keep that lamp off long enough to see.
HOLD_RELEASE_OFF_SECONDS = 0.25


class PanelEvents(NamedTuple):
    starts: int
    stops: int
    stop_holds: int
    steps: int


class Panel:
    """Presses and encoder steps collected outside the controller lock.

    Start has no long-press command, so it counts once on the way up.
    Stop counts once on the way up, unless the finger stayed down for
    ``STOP_HOLD_SECONDS``. That hold is not also a short press. The two
    buttons share a ground, so an edge on one pin is ignored while the
    other press is already real.

    While Stop is held, the Start lamp stays dark. The Stop lamp stays
    dark too until the hold timer fires, then comes on, and goes out
    again when the button is released.
    ``take`` still watches the pins, so a release the debounce dropped
    still finishes.
    """

    def __init__(self, start: object, stop: object, leds: PromptLeds, encoder: object | None) -> None:
        self._start_button = start
        self._stop_button = stop
        self._leds = leds
        self._encoder = encoder
        self._guard = threading.Lock()
        self._steps = 0
        self._desired_leds = (False, False)
        self._painted: tuple[bool, bool] | None = None
        self._stop_forced: str | None = None
        self._release_dark_until = 0.0
        self._gestures = PressPair(
            start,
            stop,
            hold_seconds=STOP_HOLD_SECONDS,
            on_hold=self._held_lamp,
        )
        self._bind_button(start, self._start_down, self._start_up)
        self._bind_button(stop, self._stop_down, self._stop_up)
        if encoder is not None:
            encoder.when_rotated_clockwise = self._clockwise  # type: ignore[attr-defined]
            encoder.when_rotated_counter_clockwise = self._counter  # type: ignore[attr-defined]
        self.set_leds(False, False)

    def _bind_button(self, button: object, on_down: object, on_up: object) -> None:
        # gpiozero's bounce timer drops edges. With no capacitor on the
        # switch, that sticks the pin after the first spike. PressPair does
        # the debounce. hold_time stays at gpiozero's default: the library
        # rejects None, and that exception was closing the whole panel.
        if hasattr(button, "bounce_time"):
            try:
                button.bounce_time = None  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Button timing could not be configured")
        button.when_pressed = on_down  # type: ignore[attr-defined]
        button.when_released = on_up  # type: ignore[attr-defined]

    def _start_down(self) -> None:
        self._gestures.down("start", time.monotonic())

    def _start_up(self) -> None:
        event = self._gestures.up("start", time.monotonic())
        self._apply_lamp(event)

    def _stop_down(self) -> None:
        event = self._gestures.down("stop", time.monotonic())
        self._apply_lamp(event)

    def _stop_up(self) -> None:
        event = self._gestures.up("stop", time.monotonic())
        self._apply_lamp(event)

    def _held_lamp(self) -> None:
        """The hold timer calls this once the finger has been down long enough."""

        self._apply_lamp("hold")

    def _clockwise(self) -> None:
        self._add_steps(1)

    def _counter(self) -> None:
        self._add_steps(-1)

    def _add_steps(self, steps: int) -> None:
        with self._guard:
            self._steps += steps

    def take(self) -> PanelEvents:
        """Return new presses, the stop hold, and encoder steps, then clear them."""

        now = time.monotonic()
        event = self._gestures.poll(now)
        starts, stops, holds = self._gestures.consume()
        with self._guard:
            if event:
                self._note_lamp(event, now)
            self._paint_leds(now)
            steps = self._steps
            self._steps = 0
        return PanelEvents(starts, stops, holds, steps)

    def set_leds(self, start_on: bool, stop_on: bool) -> None:
        with self._guard:
            self._desired_leds = (bool(start_on), bool(stop_on))
            self._paint_leds(time.monotonic())

    def _apply_lamp(self, event: str | None) -> None:
        if not event:
            return
        with self._guard:
            self._note_lamp(event, time.monotonic())
            self._paint_leds(time.monotonic())

    def _note_lamp(self, event: str, now: float) -> None:
        """Caller holds ``self._guard``."""

        if event == "down":
            self._stop_forced = "off"
        elif event == "hold":
            self._stop_forced = "on"
            self._release_dark_until = 0.0
        elif event == "hold-release":
            self._stop_forced = None
            self._release_dark_until = now + HOLD_RELEASE_OFF_SECONDS
        elif event in ("tap", "release", "bounce"):
            self._stop_forced = None

    def _paint_leds(self, now: float) -> None:
        """Caller holds ``self._guard``. Only the lamp that changed is written."""

        start, stop = self._desired_leds
        # The finger is still down. Leave Start dark for the whole press.
        # Driving that pin while Stop changes was lighting the Start lamp.
        if self._stop_forced is not None:
            start = False
        if self._stop_forced == "off":
            stop = False
        elif self._stop_forced == "on":
            stop = True
        elif now < self._release_dark_until:
            stop = False
        shown = (start, stop)
        if shown == self._painted:
            return
        self._painted = shown
        self._leds.set(start, stop)

    def close(self) -> None:
        self._gestures.close()
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
        # Start stays refused. Stop still stops an open run, and once the
        # still is idle or cooling a hold opens the software menu.
        if mode in ("idle", "cooling", "running", "paused"):
            return False, True
        return False, False
    if mode == "running":
        return False, True
    # Idle and cooling: Start runs the still, and holding Stop opens the menu.
    # Paused: Start resumes and Stop stops.
    return True, True


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
        # gpiozero still runs its own hold thread. when_held is left unset,
        # so that thread cannot turn a tap into a second gesture.
        start = Button(
            gpio.button_start,
            pull_up=True,
            bounce_time=None,
            pin_factory=factory,
        )
        stop = Button(
            gpio.button_stop,
            pull_up=True,
            bounce_time=None,
            pin_factory=factory,
        )
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
