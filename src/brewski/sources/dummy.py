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
# Measurement jitter. Applied to each reading, not stored, so it cannot walk
# the pot out of the 2°F cooldown band while the still is sitting still.
NOISE_F = 0.15
# Heat left in the element after the SSR opens. A real sheath keeps warming
# the liquid for 10–60 seconds; this simulation is sped up, so the tail is
# shorter, but the temperature still climbs with the heaters already off.
# That coast is why the controller cuts power before the setpoint.
ELEMENT_TAU_SEC = 8.0
# °F/s one fully soaked element still delivers the moment the SSR opens.
RESIDUAL_PEAK_F_PER_SEC = 0.25


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
        # 0 is a cold element, 1 is fully soaked. Opening the SSR does not
        # clear this; the stored heat drains into the boiler on later steps.
        self.residual = [0.0, 0.0]
        self._initial_temps = (float(initial_temps[0]), float(initial_temps[1]))

    def reset(self) -> None:
        """Return the simulated pot to the temperature it had at startup."""

        self.temps = [self._initial_temps[0], self._initial_temps[1]]
        self.heaters = [False, False]
        self.residual = [0.0, 0.0]

    def read_temperatures(self, count: int) -> list[float]:
        count = max(1, min(2, int(count)))
        temps = [float(temp) for temp in self.temps[:count]]
        if not self.noise:
            return temps
        return [_clamp(temp + random.uniform(-NOISE_F, NOISE_F)) for temp in temps]

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
        for index, on in enumerate(self.heaters):
            soak = self.residual[index]
            if on:
                soak += (1.0 - soak) * (dt / ELEMENT_TAU_SEC)
            else:
                # The SSR is open. The sheath is still hot, and that heat
                # keeps entering the liquid until the element cools.
                heat += RESIDUAL_PEAK_F_PER_SEC * soak
                soak -= soak * (dt / ELEMENT_TAU_SEC)
            self.residual[index] = min(1.0, max(0.0, soak))
        boiler = self.temps[0]
        cool = COOLING_COEFFICIENT * (boiler - AMBIENT_F)
        boiler += (heat - cool) * dt
        self.temps[0] = _clamp(boiler)

        lagged_target = self.temps[0] - SENSOR2_OFFSET_F
        column = self.temps[1] + SENSOR2_LAG * (lagged_target - self.temps[1]) * dt
        self.temps[1] = _clamp(column)


def _clamp(temp: float) -> float:
    return min(250.0, max(32.0, temp))
