"""MAX31865 conversion. No hardware imports, so tests can run on a Mac.

The reading is the 15-bit RTD code. Resistance is that code times the
reference resistor divided by 32768. Temperature uses the Callendar–Van
Dusen quadratic, the same polynomial the Adafruit driver uses.
"""

from __future__ import annotations

import math

# Hardware SPI0 on the Pi header. Chip select is a separate GPIO.
HARDWARE_CLK = 11
HARDWARE_MISO = 9
HARDWARE_MOSI = 10

RTD_A = 3.9083e-3
RTD_B = -5.775e-7
NOMINAL_OHMS = {"pt100": 100.0, "pt1000": 1000.0}

# VBIAS and automatic conversion. Bit 4 selects 3-wire mode.
CONFIG_BIAS_AUTO = 0xC0
CONFIG_3WIRE = 0x10


def config_byte(wires: int) -> int:
    value = CONFIG_BIAS_AUTO
    if int(wires) == 3:
        value |= CONFIG_3WIRE
    return value


def decode_rtd(msb: int, lsb: int) -> tuple[int, bool]:
    """Return the 15-bit code and whether the fault bit in the LSB is set."""

    raw = ((msb & 0xFF) << 8) | (lsb & 0xFF)
    return raw >> 1, bool(raw & 0x01)


def temperature_f(rtd_raw: int, ref_ohms: float, nominal_ohms: float) -> float:
    """°F from a 15-bit RTD code, the reference resistor, and the RTD nominal."""

    resistance = (int(rtd_raw) / 32768.0) * float(ref_ohms)
    discriminant = RTD_A * RTD_A - (4.0 * RTD_B * (1.0 - (resistance / float(nominal_ohms))))
    temp_c = (-RTD_A + math.sqrt(max(0.0, discriminant))) / (2.0 * RTD_B)
    return temp_c * 9.0 / 5.0 + 32.0


def hardware_spi(clk: int, sdo: int, sdi: int) -> bool:
    """True only for the Pi's SPI0 pins. SDO is MISO and SDI is MOSI."""

    return (int(clk), int(sdo), int(sdi)) == (HARDWARE_CLK, HARDWARE_MISO, HARDWARE_MOSI)
