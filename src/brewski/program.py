"""A saved program: the mode Start will run, and when that run should end.

Hold is the first mode. It keeps a target until Stop, or for a set time
once the pot is holding. A max duration in settings ends every open run.
Later modes add a branch in :func:`program_end`.
"""

from __future__ import annotations

from dataclasses import dataclass

PROGRAM_MODES = ("hold",)
HOLD_MODE = "hold"
MIN_PROGRAM_HOURS = 6
MAX_PROGRAM_HOURS = 72
DEFAULT_MAX_PROGRAM_HOURS = 24
DEFAULT_DURATION_SECONDS = 3600
MAX_DURATION_SECONDS = MAX_PROGRAM_HOURS * 3600


@dataclass(frozen=True)
class Program:
    """The program copied onto a run when Start is pressed."""

    mode: str = HOLD_MODE
    continuous: bool = True
    duration_seconds: int = DEFAULT_DURATION_SECONDS

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "continuous": self.continuous,
            "duration_seconds": self.duration_seconds,
        }


def program_end(
    program: Program,
    *,
    elapsed_seconds: float,
    hold_seconds: float | None,
    max_program_hours: int,
) -> str | None:
    """Why the open run should close, or None.

    ``max`` is the emergency cap on elapsed run time. ``duration`` is a timed
    Hold that has been holding long enough. The cap wins when both are due.
    ``hold_seconds`` stays None until the run has held once, so the heat-up
    does not consume the hold timer.
    """

    if float(elapsed_seconds) >= int(max_program_hours) * 3600:
        return "max"
    if program.mode != HOLD_MODE or program.continuous or hold_seconds is None:
        return None
    if float(hold_seconds) >= int(program.duration_seconds):
        return "duration"
    return None


def parse_program_mode(value: object) -> str:
    if not isinstance(value, str) or value not in PROGRAM_MODES:
        raise ValueError("program_mode must be hold")
    return value


def parse_program_continuous(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("program_continuous must be true or false")
    return value


def parse_program_duration(value: object) -> int:
    seconds = _whole_number(value, "program_duration_seconds")
    if seconds < 1 or seconds > MAX_DURATION_SECONDS:
        raise ValueError(
            "program_duration_seconds must be a whole number from 1 to "
            f"{MAX_DURATION_SECONDS}"
        )
    return seconds


def parse_max_program_hours(value: object) -> int:
    hours = _whole_number(value, "max_program_hours")
    if hours < MIN_PROGRAM_HOURS or hours > MAX_PROGRAM_HOURS:
        raise ValueError(
            f"max_program_hours must be a whole number from {MIN_PROGRAM_HOURS} "
            f"to {MAX_PROGRAM_HOURS}"
        )
    return hours


def _whole_number(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a whole number")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be a whole number")
    return int(value)
