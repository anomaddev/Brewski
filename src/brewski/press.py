"""One press on the start button and one press on the stop button.

The two switches share a ground, so a real press on one pin can blip the
other. gpiozero can also deliver a hold after the finger has already
lifted. This tracker turns those edges into one tap, or one hold, and
never both.
"""

from __future__ import annotations

import threading
import time

# Long enough that stopping a run does not open the software menu.
# Change this to retune every long press. The pin numbers live on the
# saved GPIO settings, not here.
STOP_HOLD_SECONDS = 4.0
# A second pin that moves with this one is the shared ground, not a second finger.
CROSSTALK_SECONDS = 0.08
# There is no capacitor on the button. A spike shorter than this is contact
# bounce or cable noise, not a finger. It must not count, and it must not
# block the other button.
PRESS_BOUNCE_SECONDS = 0.05


class Press:
    """The finger on one button. A new generation starts every time it goes down."""

    def __init__(self, button: object, hold_seconds: float | None) -> None:
        self.button = button
        self.hold_seconds = hold_seconds
        self.generation = 0
        self.down_at: float | None = None
        self.held = False
        self.overlapped = False
        self.counted = False
        self.wait_release = _is_pressed(button) is True
        self.ended_at = 0.0
        self.taps = 0
        self.holds = 0
        self._timer: threading.Timer | None = None

    def blocks_other(self, now: float) -> bool:
        """True when a new edge on the other button is this press, not a new one."""

        if self.down_at is not None and now - self.down_at >= CROSSTALK_SECONDS:
            return True
        return self.down_at is None and now - self.ended_at < CROSSTALK_SECONDS

    def arm(self, now: float, schedule) -> None:
        self._cancel_timer()
        self.generation += 1
        self.down_at = now
        self.held = False
        self.overlapped = False
        self.counted = False
        if self.hold_seconds is not None and schedule is not None:
            self._timer = schedule(self.generation)

    def disarm(self, now: float) -> None:
        self._cancel_timer()
        self.generation += 1
        self.down_at = None
        self.held = False
        self.ended_at = now

    def accept_hold(self, generation: int) -> bool:
        """Count one hold if this generation is still down. A late timer loses."""

        if self.generation != generation or self.down_at is None or self.held:
            return False
        if _is_pressed(self.button) is False:
            return False
        self.held = True
        self.holds += 1
        self.counted = True
        self._cancel_timer()
        return True

    def _cancel_timer(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def close(self) -> None:
        self._cancel_timer()


class PressPair:
    """Start and stop together, so a blip on the shared ground is not a second press."""

    def __init__(
        self,
        start: object,
        stop: object,
        *,
        hold_seconds: float | None,
        on_hold=None,
    ) -> None:
        self.start = Press(start, None)
        self.stop = Press(stop, hold_seconds)
        self._on_hold = on_hold
        self._lock = threading.Lock()
        self._closed = False

    def down(self, name: str, now: float) -> str | None:
        """Finger down. ``"down"`` when Stop's lamp should go dark."""

        with self._lock:
            return self._down(name, now)

    def up(self, name: str, now: float) -> str | None:
        """Finger up. One tap, one hold, or nothing if this edge was the other button."""

        with self._lock:
            return self._up(name, now)

    def poll(self, now: float) -> str | None:
        """Finish a press whose callback the debounce swallowed."""

        with self._lock:
            stop_event = self._reconcile("stop", now)
            start_event = self._reconcile("start", now)
            if stop_event:
                return stop_event
            if start_event == "down":
                return "down"
            return None

    def fire(self, generation: int) -> None:
        """The hold timer. A generation that already ended does nothing."""

        with self._lock:
            if self._closed:
                return
            accepted = self.stop.accept_hold(generation)
        if accepted and self._on_hold is not None:
            self._on_hold()

    def consume(self) -> tuple[int, int, int]:
        """Return new start taps, stop taps, and stop holds, then clear them."""

        with self._lock:
            counts = (self.start.taps, self.stop.taps, self.stop.holds)
            self.start.taps = 0
            self.stop.taps = 0
            self.stop.holds = 0
            return counts

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.start.close()
            self.stop.close()

    def _down(self, name: str, now: float, *, force: bool = False) -> str | None:
        press = self._press(name)
        other = self._other(name)
        if press.wait_release or press.down_at is not None:
            return None
        if _is_pressed(press.button) is False:
            return None
        # A blip just let go. The pin that is still down is the real press,
        # so it may arm even though that blip's release is still inside the window.
        if not force and other.blocks_other(now):
            return None
        press.arm(now, self._schedule if press.hold_seconds else None)
        if other.down_at is not None:
            press.overlapped = True
            other.overlapped = True
        if name == "stop":
            return "down"
        return None

    def _up(self, name: str, now: float) -> str | None:
        press = self._press(name)
        other = self._other(name)
        if press.down_at is None:
            if press.wait_release and _is_pressed(press.button) is not True:
                press.wait_release = False
                press.ended_at = now
            return None
        duration = now - press.down_at
        was_held = press.held
        overlapped = press.overlapped
        other_down = other.down_at is not None or _is_pressed(other.button) is True
        other_counted = other.counted
        press.disarm(now)
        if was_held or (press.hold_seconds is not None and duration >= press.hold_seconds):
            if not was_held:
                press.holds += 1
                press.counted = True
            return "hold-release" if name == "stop" else None
        # A spike used to count as Stop, then sit in the crosstalk window and
        # reject Start for the next 80 ms. With the pin chattering, Start
        # never got through.
        if duration < PRESS_BOUNCE_SECONDS:
            press.ended_at = 0.0
            return "bounce" if name == "stop" else None
        # The pin that lets go while the other is still down is the blip.
        # A short tap still counts when it outlasts that blip.
        suppress = other_down or (overlapped and other_counted)
        handed = None
        if suppress and other_down and duration < CROSSTALK_SECONDS and other.down_at is None:
            handed = self._down(self._other_name(name), now, force=True)
        if not suppress:
            press.taps += 1
            press.counted = True
        if handed == "down":
            return "down"
        if name == "stop":
            return "tap"
        return None

    def _reconcile(self, name: str, now: float) -> str | None:
        press = self._press(name)
        level = _is_pressed(press.button)
        if press.wait_release:
            if level is False:
                press.wait_release = False
            return None
        if press.down_at is not None and level is False:
            return self._up(name, now)
        if (
            press.down_at is not None
            and not press.held
            and press.hold_seconds is not None
            and level is True
            and now - press.down_at >= press.hold_seconds
        ):
            if press.accept_hold(press.generation):
                return "hold"
            return None
        if press.down_at is None and level is True:
            return self._down(name, now)
        return None

    def _schedule(self, generation: int) -> threading.Timer:
        hold_seconds = self.stop.hold_seconds
        if hold_seconds is None:
            raise RuntimeError("Stop has no hold time")
        timer = threading.Timer(hold_seconds, self.fire, args=(generation,))
        timer.daemon = True
        timer.start()
        return timer

    def _press(self, name: str) -> Press:
        if name == "stop":
            return self.stop
        return self.start

    def _other(self, name: str) -> Press:
        if name == "stop":
            return self.start
        return self.stop

    def _other_name(self, name: str) -> str:
        if name == "stop":
            return "start"
        return "stop"


def _is_pressed(button: object) -> bool | None:
    pressed = getattr(button, "is_pressed", None)
    if pressed is None:
        return None
    return bool(pressed)
