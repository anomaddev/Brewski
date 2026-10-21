"""Console lines captured while a run is open.

The control log is the spreadsheet of temperatures and calculations. This is
the other half of an export: the same logging the process prints, kept with
the run so a later zip can show what the program was doing at those readings.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable

from brewski.store import Store, utc_now

# A line longer than this is cut off. A traceback is useful; a huge dump is not.
_MAX_MESSAGE = 8000
EMPTY_CONSOLE_LOG = "No console output was captured for this run.\n"


class ConsoleCapture(logging.Handler):
    """Write ``brewski`` log lines onto the run that is open right now.

    ``context`` returns that run and its clock, or ``(None, None)`` when
    nothing is open. A record can name a run itself (``brewski_run_id``) so a
    line written after the run has closed, such as a late analysis, still
    lands on that run.
    """

    def __init__(
        self,
        store: Store,
        context: Callable[[], tuple[int | None, float | None]],
        owner: weakref.ReferenceType,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.store = store
        self._context = context
        self._owner = owner
        self._local = threading.local()

    def alive(self) -> bool:
        return self._owner() is not None

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            explicit = getattr(record, "brewski_run_id", None)
            current, clock = self._context()
            run_id = explicit if explicit is not None else current
            if run_id is None:
                return
            elapsed = getattr(record, "brewski_elapsed", None)
            if elapsed is None and (explicit is None or explicit == current):
                elapsed = clock
            message = record.getMessage()
            if record.exc_info:
                if message:
                    message += "\n"
                message += logging.Formatter().formatException(record.exc_info)
            if len(message) > _MAX_MESSAGE:
                message = message[:_MAX_MESSAGE] + "…"
            self.store.add_console_line(int(run_id), format_console_line(
                None if elapsed is None else float(elapsed),
                utc_now(),
                record.levelname,
                record.name,
                message,
            ))
        except Exception:
            # Logging must not take the controller down, and it must not log
            # again from inside this handler.
            return
        finally:
            self._local.busy = False


def format_console_line(
    elapsed: float | None,
    recorded_at: str,
    level: str,
    logger_name: str,
    message: str,
) -> str:
    """One console line. Elapsed time is included when the run clock is known."""

    clock = "" if elapsed is None else f"{elapsed:.2f}s "
    return f"{recorded_at} {clock}{level} {logger_name}: {message}"


def console_log_text(lines: list[str]) -> str:
    """The export body. An empty capture says so, instead of an empty file."""

    if not lines:
        return EMPTY_CONSOLE_LOG
    return "".join(f"{line}\n" for line in lines)
