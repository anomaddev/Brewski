"""Emergency codes the controller can raise.

This module is the only list. A new fault gets the next number here and a
line in the README. Numbers are not reused or renumbered.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmergencyCode:
    code: str
    summary: str


CODES: tuple[EmergencyCode, ...] = (
    EmergencyCode("E01", "Cabinet temperature is above the cabinet limit."),
    EmergencyCode("E02", "The HTS221 is enabled and does not answer."),
    EmergencyCode("E03", "The control probe faulted or did not answer."),
    EmergencyCode("E04", "The SPI bus did not open, or the saved SPI pins are not the hardware bus."),
)

_BY_CODE = {item.code: item for item in CODES}


def summary(code: str) -> str:
    item = _BY_CODE.get(code)
    if item is None:
        return "Unknown emergency."
    return item.summary


def check_code_list(codes: list[str]) -> None:
    """Reject a duplicate or a list that is not E01, E02, ... in order."""

    expected = [f"E{number:02d}" for number in range(1, len(codes) + 1)]
    if codes != expected or len(set(codes)) != len(codes):
        raise ValueError("Emergency codes must be E01, E02, ... with no duplicates or gaps")


check_code_list([item.code for item in CODES])
