"""The contract a still's heaters and temperature sensors must satisfy.

A future Raspberry Pi source can implement this without the dashboard, the
run log, or the control loop changing. Nothing in this package imports GPIO.
"""

from __future__ import annotations

from typing import Protocol


class HardwareSource(Protocol):
    """Live heaters and temperature sensors, in degrees Fahrenheit."""

    name: str

    def read_temperatures(self, count: int) -> list[float]:
        """Return ``count`` probe temperatures. Index 0 is the control probe."""

    def set_heaters(self, states: list[bool]) -> None:
        """Turn each heater on or off. Index 0 is heater 1."""
