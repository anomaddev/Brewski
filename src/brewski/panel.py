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
from brewski.store import Settings, default_gpio

logger = logging.getLogger(__name__)

# Long enough that stopping a run does not open the software menu.
STOP_HOLD_SECONDS = 4.0
# gpiozero's longer debounce was swallowing the release of a quick tap, so
# the press never became a click. Contact bounce is over in a few milliseconds.
BUTTON_BOUNCE_SECONDS = 0.01
# Ignore a second edge this soon after the last one. A real tap is longer.
EDGE_GAP_SECONDS = 0.012
# After the finger lifts, keep the acknowledgement lamp off long enough to see.
HOLD_RELEASE_OFF_SECONDS = 0.25


class PanelEvents(NamedTuple):
    starts: int
    stops: int
    stop_holds: int
    steps: int


class Panel:
    """Presses and encoder steps collected outside the controller lock.

    Start counts on the way down, so a quick tap is not waiting on a release
    the debounce might drop. Stop counts on the way up, unless it was still
    down at the hold time. ``take`` also watches the pin, so a release that
    never produces a callback still completes the press.
    """

    def __init__(self, start: object, stop: object, leds: PromptLeds, encoder: object | None) -> None:
        self._start_button = start
        self._stop_button = stop
        self._leds = leds
        self._encoder = encoder
        self._guard = threading.Lock()
        self._start_presses = 0
        self._stop_presses = 0
        self._stop_holds = 0
        self._steps = 0
        self._start_down_at: float | None = None
        self._stop_down_at: float | None = None
        self._stop_hold_sent = False
        self._start_wait_release = _is_pressed(start) is True
        self._stop_wait_release = _is_pressed(stop) is True
        self._last_start_edge = 0.0
        self._last_stop_edge = 0.0
        self._desired_leds = (False, False)
        self._painted: tuple[bool, bool] | None = None
        self._release_dark_until = 0.0
        self._bind_button(start, self._start_down, self._start_up)
        self._bind_button(stop, self._stop_down, self._stop_up)
        if encoder is not None:
            encoder.when_rotated_clockwise = self._clockwise  # type: ignore[attr-defined]
            encoder.when_rotated_counter_clockwise = self._counter  # type: ignore[attr-defined]
        self.set_leds(False, False)

    def _bind_button(self, button: object, on_down: object, on_up: object) -> None:
        try:
            button.bounce_time = BUTTON_BOUNCE_SECONDS  # type: ignore[attr-defined]
            button.when_held = None  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Button debounce was left at the device default", exc_info=True)
        button.when_pressed = on_down  # type: ignore[attr-defined]
        button.when_released = on_up  # type: ignore[attr-defined]

    def _start_down(self) -> None:
        with self._guard:
            self._begin_start(time.monotonic())

    def _start_up(self) -> None:
        with self._guard:
            self._end_start(time.monotonic())

    def _stop_down(self) -> None:
        with self._guard:
            self._begin_stop(time.monotonic())
            self._paint_leds(time.monotonic())

    def _stop_up(self) -> None:
        with self._guard:
            self._end_stop(time.monotonic())
            self._paint_leds(time.monotonic())

    def _begin_start(self, now: float) -> None:
        """Caller holds ``self._guard``."""

        if self._start_wait_release:
            return
        if self._start_down_at is not None:
            return
        if now - self._last_start_edge < EDGE_GAP_SECONDS:
            return
        self._last_start_edge = now
        self._start_down_at = now
        self._start_presses += 1

    def _end_start(self, now: float) -> None:
        """Caller holds ``self._guard``."""

        self._last_start_edge = now
        self._start_down_at = None
        self._start_wait_release = False

    def _begin_stop(self, now: float) -> None:
        """Caller holds ``self._guard``."""

        if self._stop_wait_release:
            return
        if self._stop_down_at is not None:
            return
        if now - self._last_stop_edge < EDGE_GAP_SECONDS:
            return
        self._last_stop_edge = now
        self._stop_down_at = now
        self._stop_hold_sent = False
        self._release_dark_until = 0.0

    def _end_stop(self, now: float) -> None:
        """Caller holds ``self._guard``. One Stop press becomes one event."""

        self._last_stop_edge = now
        down_at = self._stop_down_at
        sent = self._stop_hold_sent
        waiting = self._stop_wait_release
        self._stop_down_at = None
        self._stop_hold_sent = False
        self._stop_wait_release = False
        if waiting or down_at is None:
            return
        if sent or now - down_at >= STOP_HOLD_SECONDS:
            if not sent:
                self._stop_holds += 1
            self._release_dark_until = now + HOLD_RELEASE_OFF_SECONDS
            return
        self._stop_presses += 1

    def _note_hold(self, now: float) -> None:
        """Caller holds ``self._guard``."""

        down_at = self._stop_down_at
        if down_at is None or self._stop_hold_sent or self._stop_wait_release:
            return
        if _is_pressed(self._stop_button) is not True:
            return
        if now - down_at < STOP_HOLD_SECONDS:
            return
        self._stop_hold_sent = True
        self._stop_holds += 1
        self._release_dark_until = 0.0

    def _recover_pins(self, now: float) -> None:
        """Finish a press whose release callback never arrived."""

        stop_down = _is_pressed(self._stop_button)
        start_down = _is_pressed(self._start_button)
        if self._stop_wait_release:
            if stop_down is False:
                self._stop_wait_release = False
        elif (
            self._stop_down_at is not None
            and stop_down is False
            and now - self._stop_down_at >= EDGE_GAP_SECONDS
        ):
            self._end_stop(now)
        if self._start_wait_release:
            if start_down is False:
                self._start_wait_release = False
        elif self._start_down_at is not None and start_down is False:
            self._end_start(now)
        elif self._start_down_at is None and start_down is True:
            self._begin_start(now)

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
        with self._guard:
            self._recover_pins(now)
            self._note_hold(now)
            events = PanelEvents(
                self._start_presses,
                self._stop_presses,
                self._stop_holds,
                self._steps,
            )
            self._start_presses = 0
            self._stop_presses = 0
            self._stop_holds = 0
            self._steps = 0
            self._paint_leds(now)
        return events

    def set_leds(self, start_on: bool, stop_on: bool) -> None:
        with self._guard:
            self._desired_leds = (bool(start_on), bool(stop_on))
            self._paint_leds(time.monotonic())

    def _paint_leds(self, now: float) -> None:
        """Caller holds ``self._guard``."""

        start, stop = self._desired_leds
        if self._stop_down_at is not None and not self._stop_hold_sent:
            stop = False
        elif self._stop_hold_sent and self._stop_down_at is not None:
            stop = True
        elif now < self._release_dark_until:
            stop = False
        shown = (start, stop)
        if shown == self._painted:
            return
        self._painted = shown
        self._leds.set(start, stop)

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


def _is_pressed(button: object) -> bool | None:
    pressed = getattr(button, "is_pressed", None)
    if pressed is None:
        return None
    return bool(pressed)


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
        start = Button(gpio.button_start, pull_up=True, bounce_time=BUTTON_BOUNCE_SECONDS, pin_factory=factory)
        stop = Button(gpio.button_stop, pull_up=True, bounce_time=BUTTON_BOUNCE_SECONDS, pin_factory=factory)
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
