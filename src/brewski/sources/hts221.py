"""HTS221 humidity and temperature conversion. No hardware imports.

Calibration bytes are the 16 registers at 0x30. Humidity and temperature
are linear interpolations between the two factory points, then °C becomes °F.
"""

from __future__ import annotations

from dataclasses import dataclass

ADDRESS = 0x5F
WHO_AM_I_REGISTER = 0x0F
WHO_AM_I = 0xBC
CTRL_REG1 = 0x20
# Power on, block data update, 1 Hz.
CTRL_POWER_ON = 0x84
HUMIDITY_OUT = 0x28
TEMP_OUT = 0x2A
CALIBRATION_START = 0x30

# Hardware I2C on the Pi header.
HARDWARE_SDA = 2
HARDWARE_SCL = 3


@dataclass(frozen=True)
class Calibration:
    h0: float
    h1: float
    h0_out: int
    h1_out: int
    t0: float
    t1: float
    t0_out: int
    t1_out: int


def _signed(lo: int, hi: int) -> int:
    value = (lo & 0xFF) | ((hi & 0xFF) << 8)
    if value & 0x8000:
        value -= 0x10000
    return value


def calibration_from_bytes(block: bytes) -> Calibration:
    """Decode the 16 calibration registers starting at 0x30."""

    if len(block) < 16:
        raise ValueError("HTS221 calibration is 16 bytes")
    raw = list(block[:16])
    t_msb = raw[5]
    t0 = (((t_msb & 0x03) << 8) | raw[2]) / 8.0
    t1 = (((t_msb & 0x0C) << 6) | raw[3]) / 8.0
    return Calibration(
        h0=raw[0] / 2.0,
        h1=raw[1] / 2.0,
        h0_out=_signed(raw[6], raw[7]),
        h1_out=_signed(raw[10], raw[11]),
        t0=t0,
        t1=t1,
        t0_out=_signed(raw[12], raw[13]),
        t1_out=_signed(raw[14], raw[15]),
    )


def compensate(calibration: Calibration, raw_humidity: int, raw_temp: int) -> tuple[float, float]:
    """Return °F and relative humidity from the raw output registers."""

    humidity = _interpolate(
        raw_humidity,
        calibration.h0_out,
        calibration.h1_out,
        calibration.h0,
        calibration.h1,
    )
    temp_c = _interpolate(
        raw_temp,
        calibration.t0_out,
        calibration.t1_out,
        calibration.t0,
        calibration.t1,
    )
    humidity = min(100.0, max(0.0, humidity))
    return temp_c * 9.0 / 5.0 + 32.0, humidity


def _interpolate(raw: int, raw0: int, raw1: int, value0: float, value1: float) -> float:
    span = raw1 - raw0
    if span == 0:
        return value0
    return value0 + (int(raw) - raw0) * (value1 - value0) / span
