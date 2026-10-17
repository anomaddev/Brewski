"""A simulated still used until real sensors and heaters are connected.

Both heaters together lift the boiler from room temperature to a typical
165°F target in a few minutes. That is much faster than a pot still, so the
chart moves while the dashboard is being designed. The second probe trails
the boiler and sits a little cooler, standing in for a column or head probe.
"""

from __future__ import annotations

import random

AMBIENT_F = 70.0
# Degrees per second added by each heater that is on.
HEAT_PER_HEATER_F_PER_SEC = 0.30
# Pull toward room temperature. At a 95°F rise this is about 0.19°F/s.
COOLING_COEFFICIENT = 0.002
SENSOR2_OFFSET_F = 8.0
# Fraction of the gap to the lagged target closed each second.
SENSOR2_LAG = 0.12
NOISE_F = 0.15


class DummySource:
    name = "dummy"

    def __init__(
        self,
        noise: bool = True,
        initial_temps: tuple[float, float] = (AMBIENT_F, AMBIENT_F - SENSOR2_OFFSET_F),
    ) -> None:
        self.noise = noise
        self.temps = [float(initial_temps[0]), float(initial_temps[1])]
        self.heaters = [False, False]

    def read_temperatures(self, count: int) -> list[float]:
        count = max(1, min(2, int(count)))
        return list(self.temps[:count])

    def set_heaters(self, states: list[bool]) -> None:
        self.heaters = [False, False]
        for index, on in enumerate(states[:2]):
            self.heaters[index] = bool(on)

    def step(self, dt: float) -> None:
        remaining = max(0.0, float(dt))
        while remaining > 0:
            slice_dt = min(1.0, remaining)
            self._step_once(slice_dt)
            remaining -= slice_dt

    def _step_once(self, dt: float) -> None:
        heat = HEAT_PER_HEATER_F_PER_SEC * sum(1 for on in self.heaters if on)
        boiler = self.temps[0]
        cool = COOLING_COEFFICIENT * (boiler - AMBIENT_F)
        boiler += (heat - cool) * dt
        if self.noise:
            boiler += random.uniform(-NOISE_F, NOISE_F)
        self.temps[0] = _clamp(boiler)

        lagged_target = self.temps[0] - SENSOR2_OFFSET_F
        column = self.temps[1] + SENSOR2_LAG * (lagged_target - self.temps[1]) * dt
        if self.noise:
            column += random.uniform(-NOISE_F, NOISE_F)
        self.temps[1] = _clamp(column)


def _clamp(temp: float) -> float:
    return min(250.0, max(32.0, temp))
