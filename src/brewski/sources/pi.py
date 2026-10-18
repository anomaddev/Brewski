"""Live still: SSR outputs, MAX31865 probes, and the HTS221 cabinet sensor.

The installed service always uses this source. A board that does not open
leaves that reading empty and keeps every SSR off. It does not fall back
to the dummy source.
"""

from __future__ import annotations

import logging
import time

from brewski.lcd import panel_pin_factory
from brewski.sources.hts221 import (
    ADDRESS,
    CALIBRATION_START,
    CTRL_POWER_ON,
    CTRL_REG1,
    HARDWARE_SCL,
    HARDWARE_SDA,
    HUMIDITY_OUT,
    TEMP_OUT,
    WHO_AM_I,
    WHO_AM_I_REGISTER,
    calibration_from_bytes,
    compensate,
)
from brewski.sources.max31865 import NOMINAL_OHMS, config_byte, decode_rtd, hardware_spi, temperature_f
from brewski.store import SLOT_COUNT, Settings, Store, default_gpio

logger = logging.getLogger(__name__)

# One conversion in automatic mode is about 60 ms. The first read waits.
_CONVERT_SECONDS = 0.07


class PiSource:
    name = "pi"

    def __init__(self, store: Store) -> None:
        self.store = store
        self.spi_fault = False
        self.probe_faults = [False] * SLOT_COUNT
        self.climate_fault = False
        self._factory: object | None = None
        self._outputs: list[object | None] = []
        self._spi: object | None = None
        self._cs: dict[int, object] = {}
        self._configured: set[int] = set()
        self._bus: object | None = None
        self._calibration = None
        self._open()

    def close(self) -> None:
        self.set_heaters([False] * SLOT_COUNT)
        for device in (*self._outputs, *self._cs.values()):
            _close(device)
        self._outputs = []
        self._cs = {}
        if self._spi is not None:
            try:
                self._spi.close()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Could not close SPI")
            self._spi = None
        if self._bus is not None:
            try:
                self._bus.close()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Could not close I2C")
            self._bus = None

    def read_temperatures(self, count: int) -> list[float | None]:
        count = max(1, min(SLOT_COUNT, int(count)))
        settings = self.store.get_settings()
        if self.spi_fault or self._spi is None:
            self.probe_faults = [True] * count
            return [None] * count
        temps: list[float | None] = []
        faults: list[bool] = []
        for index in range(count):
            channel = settings.sensors[index]
            if not channel.enabled:
                temps.append(None)
                faults.append(False)
                continue
            reading = self._read_probe(channel.gpio, channel.wires or 3, channel.rtd or "pt100", channel.ref_ohms or 430.0)
            temps.append(reading)
            faults.append(reading is None)
        self.probe_faults = faults
        return temps

    def read_climate(self) -> tuple[float | None, float | None]:
        settings = self.store.get_settings()
        if not _hts_enabled(settings) or self._bus is None or self._calibration is None:
            if _hts_enabled(settings):
                self.climate_fault = True
            return None, None
        try:
            humidity_raw = _read_word(self._bus, HUMIDITY_OUT)
            temp_raw = _read_word(self._bus, TEMP_OUT)
            temp_f, humidity = compensate(self._calibration, humidity_raw, temp_raw)
        except Exception:
            logger.exception("HTS221 read failed")
            self.climate_fault = True
            return None, None
        self.climate_fault = False
        return temp_f, humidity

    def set_heaters(self, states: list[bool]) -> None:
        for index, on in enumerate(states[:SLOT_COUNT]):
            device = self._outputs[index] if index < len(self._outputs) else None
            if device is None:
                continue
            try:
                if on:
                    device.on()  # type: ignore[attr-defined]
                else:
                    device.off()  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Could not drive heater %s", index + 1)
                try:
                    device.off()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _open(self) -> None:
        self._factory = panel_pin_factory()
        if self._factory is None:
            self.spi_fault = True
            self.probe_faults = [True] * SLOT_COUNT
            self.climate_fault = True
            logger.warning("GPIO did not open. Heaters stay off.")
            return
        settings = self.store.get_settings()
        self._open_heaters(settings)
        self._open_spi(settings)
        self._open_climate(settings)

    def _open_heaters(self, settings: Settings) -> None:
        try:
            from gpiozero import DigitalOutputDevice
        except ImportError:
            logger.warning("gpiozero is not installed. Heaters stay off.")
            self._outputs = [None] * SLOT_COUNT
            return
        outputs: list[object | None] = []
        for channel in settings.heaters:
            if channel.gpio is None:
                outputs.append(None)
                continue
            try:
                outputs.append(
                    DigitalOutputDevice(
                        channel.gpio,
                        active_high=True,
                        initial_value=False,
                        pin_factory=self._factory,
                    )
                )
            except Exception:
                logger.exception("Heater BCM %s could not be opened", channel.gpio)
                outputs.append(None)
        self._outputs = outputs

    def _open_spi(self, settings: Settings) -> None:
        gpio = settings.gpio or default_gpio()
        if not hardware_spi(gpio.spi_clk, gpio.spi_sdo, gpio.spi_sdi):
            self.spi_fault = True
            self.probe_faults = [True] * SLOT_COUNT
            logger.warning(
                "SPI pins BCM %s %s %s are not the hardware bus. Probes left unread.",
                gpio.spi_clk,
                gpio.spi_sdo,
                gpio.spi_sdi,
            )
            return
        try:
            import spidev
            from gpiozero import DigitalOutputDevice
        except ImportError:
            self.spi_fault = True
            self.probe_faults = [True] * SLOT_COUNT
            logger.warning("spidev or gpiozero is not installed. Probes left unread.")
            return
        try:
            spi = spidev.SpiDev()
            spi.open(0, 0)
            spi.max_speed_hz = 500_000
            spi.mode = 1
            try:
                spi.no_cs = True
            except Exception:
                logger.warning("SPI hardware chip select could not be disabled")
            self._spi = spi
        except Exception:
            logger.exception("SPI did not open")
            self.spi_fault = True
            self.probe_faults = [True] * SLOT_COUNT
            return
        for channel in settings.sensors:
            if channel.gpio is None or channel.gpio in self._cs:
                continue
            try:
                # Active-low chip select. Off leaves the pin high.
                self._cs[channel.gpio] = DigitalOutputDevice(
                    channel.gpio,
                    active_high=False,
                    initial_value=False,
                    pin_factory=self._factory,
                )
            except Exception:
                logger.exception("Chip select BCM %s could not be opened", channel.gpio)

    def _open_climate(self, settings: Settings) -> None:
        if not _hts_enabled(settings):
            self.climate_fault = False
            return
        gpio = settings.gpio or default_gpio()
        if (gpio.i2c_sda, gpio.i2c_scl) != (HARDWARE_SDA, HARDWARE_SCL):
            self.climate_fault = True
            logger.warning("HTS221 I2C pins are not BCM 2 and 3. Cabinet sensor left unread.")
            return
        try:
            import smbus2
        except ImportError:
            self.climate_fault = True
            logger.warning("smbus2 is not installed. Cabinet sensor left unread.")
            return
        try:
            bus = smbus2.SMBus(1)
            who = bus.read_byte_data(ADDRESS, WHO_AM_I_REGISTER)
            if who != WHO_AM_I:
                raise OSError(f"HTS221 who-am-i was {who:#x}")
            bus.write_byte_data(ADDRESS, CTRL_REG1, CTRL_POWER_ON)
            block = bytes(bus.read_i2c_block_data(ADDRESS, CALIBRATION_START | 0x80, 16))
            self._calibration = calibration_from_bytes(block)
            self._bus = bus
            self.climate_fault = False
        except Exception:
            logger.exception("HTS221 did not answer")
            self.climate_fault = True
            self._calibration = None

    def _read_probe(self, cs_pin: int | None, wires: int, rtd: str, ref_ohms: float) -> float | None:
        if cs_pin is None or cs_pin not in self._cs or self._spi is None:
            return None
        cs = self._cs[cs_pin]
        try:
            if cs_pin not in self._configured:
                _transfer(self._spi, cs, [0x80, config_byte(wires)])
                self._configured.add(cs_pin)
                time.sleep(_CONVERT_SECONDS)
            data = _transfer(self._spi, cs, [0x01, 0x00, 0x00])
            fault = _transfer(self._spi, cs, [0x07, 0x00])
        except Exception:
            logger.exception("MAX31865 read failed on BCM %s", cs_pin)
            return None
        if len(data) < 3 or len(fault) < 2:
            return None
        raw, fault_bit = decode_rtd(data[1], data[2])
        if fault_bit or (fault[1] & 0xFC):
            return None
        nominal = NOMINAL_OHMS.get(rtd, NOMINAL_OHMS["pt100"])
        try:
            return temperature_f(raw, ref_ohms, nominal)
        except (ValueError, ZeroDivisionError):
            return None


def _hts_enabled(settings: Settings) -> bool:
    gpio = settings.gpio or default_gpio()
    return any(device.id == "hts221" and device.enabled for device in gpio.i2c_devices)


def _transfer(spi: object, cs: object, payload: list[int]) -> list[int]:
    cs.on()  # type: ignore[attr-defined]
    try:
        return list(spi.xfer2(payload))  # type: ignore[attr-defined]
    finally:
        cs.off()  # type: ignore[attr-defined]


def _read_word(bus: object, register: int) -> int:
    # Auto-increment bit so the low and high bytes come from one transaction.
    data = bus.read_i2c_block_data(ADDRESS, register | 0x80, 2)  # type: ignore[attr-defined]
    value = (data[0] & 0xFF) | ((data[1] & 0xFF) << 8)
    if value & 0x8000:
        value -= 0x10000
    return value


def _close(device: object | None) -> None:
    if device is None:
        return
    try:
        device.off()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        device.close()  # type: ignore[attr-defined]
    except Exception:
        logger.exception("Could not close a GPIO output")
