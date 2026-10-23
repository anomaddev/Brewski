"""SQLite storage for settings, runs, the sample log, and the timeline."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from brewski.program import (
    DEFAULT_DURATION_SECONDS,
    DEFAULT_MAX_PROGRAM_HOURS,
    HOLD_MODE,
    parse_max_program_hours,
    parse_program_continuous,
    parse_program_duration,
    parse_program_mode,
)
from brewski.timeline import GAP_AFTER_MS, iso_to_ms

TARGET_MIN_F = 90.0
TARGET_MAX_F = 240.0
MAX_NAME_LENGTH = 40
# A probe name, a colon, a space, and a 7-character reading ("168.5 F") fill
# the 20-column LCD. 11 is the longest name that still leaves that room.
PROBE_NAME_MAX = 11
CABINET_LIMIT_DEFAULT_F = 170
CABINET_LIMIT_MIN_F = 100
CABINET_LIMIT_MAX_F = 200
SLOT_COUNT = 2
# BCM numbers on the 40-pin header. GPIO 0 and 1 are the board ID EEPROM.
GPIO_MIN = 2
GPIO_MAX = 27
# How long a heater stays on, and then off, before the controller may switch
# it again. A zero-crossing SSR only changes state as the mains voltage
# passes through zero, and cycling faster than about half a second chatters
# the element. One second sits in the useful 0.5–2 second range.
HEATER_MIN_ON_DEFAULT_SECONDS = 1.0
HEATER_MIN_OFF_DEFAULT_SECONDS = 1.0
HEATER_DWELL_FLOOR_SECONDS = 0.5
HEATER_DWELL_CEILING_SECONDS = 60.0
# Clamp on a measured or volume-scaled coast. Older databases still have an
# early_cutoff_f column. It is left in place and is not read or written.
EARLY_CUTOFF_MIN_F = 0.0
EARLY_CUTOFF_MAX_F = 20.0
# Gallons of liquid in the pot for a calibration. The coast scales with this.
CHARGE_MIN_GALLONS = 0.25
CHARGE_MAX_GALLONS = 100.0
# Where a Pi looks for the next Brewski update. Main is the latest published
# release. Beta is the newest commit on the beta branch.
UPDATE_SOURCES = ("main", "beta")
DEFAULT_UPDATE_SOURCE = "main"
# BCM defaults from the still wiring. Heaters drive the AC SSRs. Each probe is
# one MAX31865: they share SPI, and each one takes its own chip select. Probe 2
# is present and disabled until that second board is wired. The remaining pins
# are the panel and the I2C bus. Every one of these must stay unique.
DEFAULT_HEATER_GPIOS = (12, 16)
DEFAULT_SENSOR_GPIOS = (17, 8)
DEFAULT_SENSOR_ENABLED = (True, False)
I2C_DEVICE_IDS = ("hts221", "lcd")
DEFAULT_WIRES = 2
DEFAULT_RTD = "pt100"
DEFAULT_REF_OHMS = {"pt100": 430.0, "pt1000": 4300.0}
REF_OHMS_MIN = 100.0
REF_OHMS_MAX = 10000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heater_count INTEGER NOT NULL,
    sensor_count INTEGER NOT NULL,
    heater_names TEXT NOT NULL,
    sensor_names TEXT NOT NULL,
    target_temp_f REAL NOT NULL,
    heater_min_on_seconds REAL NOT NULL DEFAULT 1,
    heater_min_off_seconds REAL NOT NULL DEFAULT 1,
    early_cutoff_f REAL NOT NULL DEFAULT 3, -- legacy, unused
    cabinet_limit_f REAL NOT NULL DEFAULT 170,
    learned_coast_f REAL,
    learned_coast_seconds REAL,
    learned_cooldown_f_per_min REAL,
    charge_gallons REAL,
    heaters TEXT,
    sensors TEXT,
    setup_complete INTEGER NOT NULL DEFAULT 0,
    gpio TEXT,
    update_source TEXT NOT NULL DEFAULT 'main',
    thermal_model TEXT,
    max_program_hours INTEGER NOT NULL DEFAULT 24,
    program_mode TEXT NOT NULL DEFAULT 'hold',
    program_continuous INTEGER NOT NULL DEFAULT 1,
    program_duration_seconds INTEGER NOT NULL DEFAULT 3600
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    target_temp_f REAL NOT NULL,
    heater_count INTEGER NOT NULL,
    sensor_count INTEGER NOT NULL,
    heater_names TEXT NOT NULL,
    sensor_names TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    heaters TEXT,
    sensors TEXT,
    kind TEXT NOT NULL DEFAULT 'run',
    charge_gallons REAL,
    coast_f REAL,
    coast_seconds REAL,
    cooldown_f_per_min REAL,
    calibration_settled INTEGER NOT NULL DEFAULT 0,
    cooldown_status TEXT,
    cooldown_seconds REAL,
    cooldown_baseline_f REAL,
    cooldown_sensor_index INTEGER,
    cooldown_ended_at TEXT,
    thermal_model TEXT,
    analysis TEXT,
    program_mode TEXT,
    program_continuous INTEGER,
    program_duration_seconds INTEGER,
    program_end TEXT
);

CREATE TABLE IF NOT EXISTS run_marks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS run_marks_run_id ON run_marks(run_id, id);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    recorded_at TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS run_logs_run_id ON run_logs(run_id, id);
CREATE INDEX IF NOT EXISTS run_logs_run_elapsed ON run_logs(run_id, elapsed_seconds, id);

CREATE TABLE IF NOT EXISTS console_logs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    recorded_at TEXT NOT NULL,
    line TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS console_logs_run_id ON console_logs(run_id, id);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    recorded_at TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    temps_f TEXT NOT NULL,
    heaters_on TEXT NOT NULL,
    target_temp_f REAL NOT NULL,
    phase TEXT NOT NULL DEFAULT 'run'
);

CREATE INDEX IF NOT EXISTS samples_run_id ON samples(run_id, elapsed_seconds);

CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY,
    recorded_ms INTEGER NOT NULL,
    temps_f TEXT NOT NULL,
    target_temp_f REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS timeline_recorded_ms ON timeline(recorded_ms);

CREATE TABLE IF NOT EXISTS timeline_gaps (
    id INTEGER PRIMARY KEY,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS timeline_gaps_started ON timeline_gaps(started_ms);

CREATE TABLE IF NOT EXISTS heater_meters (
    slot INTEGER PRIMARY KEY,
    on_seconds REAL NOT NULL DEFAULT 0
);
"""


class SettingsError(ValueError):
    """The settings document is missing a field or has a value the still cannot use."""


@dataclass(frozen=True)
class I2cDevice:
    """One board on the shared I2C bus.

    The LCD driver probes the usual backpack addresses. They are not stored.
    """

    id: str
    name: str
    enabled: bool

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "enabled": self.enabled}


@dataclass(frozen=True)
class GpioConfig:
    """BCM pins for the buses and the front panel.

    Heaters and probes keep their own pins. This block is everything else that
    has to stay off those pins: SPI, I2C, the encoder, the start and stop
    buttons, and the LED relays.
    """

    spi_clk: int
    spi_sdo: int
    spi_sdi: int
    i2c_sda: int
    i2c_scl: int
    encoder_a: int
    encoder_b: int
    led_start: int
    led_stop: int
    button_start: int
    button_stop: int
    i2c_devices: tuple[I2cDevice, ...]

    def to_dict(self) -> dict:
        return {
            "spi": {"clk": self.spi_clk, "sdo": self.spi_sdo, "sdi": self.spi_sdi},
            "i2c": {"sda": self.i2c_sda, "scl": self.i2c_scl},
            "i2c_devices": [device.to_dict() for device in self.i2c_devices],
            "encoder": {"a": self.encoder_a, "b": self.encoder_b},
            "leds": {"start": self.led_start, "stop": self.led_stop},
            "buttons": {"start": self.button_start, "stop": self.button_stop},
        }

    def pins(self) -> list[tuple[str, int]]:
        """Labeled BCM pins, in a stable order, for collision checks."""

        return [
            ("SPI CLK", self.spi_clk),
            ("SPI SDO", self.spi_sdo),
            ("SPI SDI", self.spi_sdi),
            ("I2C SDA", self.i2c_sda),
            ("I2C SCL", self.i2c_scl),
            ("encoder A", self.encoder_a),
            ("encoder B", self.encoder_b),
            ("start LED relay", self.led_start),
            ("stop LED relay", self.led_stop),
            ("start button", self.button_start),
            ("stop button", self.button_stop),
        ]

    def spi_pins(self) -> frozenset[int]:
        return frozenset({self.spi_clk, self.spi_sdo, self.spi_sdi})

    def lcd_enabled(self) -> bool:
        return any(device.id == "lcd" and device.enabled for device in self.i2c_devices)


def default_gpio() -> GpioConfig:
    return GpioConfig(
        spi_clk=11,
        spi_sdo=9,
        spi_sdi=10,
        i2c_sda=2,
        i2c_scl=3,
        encoder_a=27,
        encoder_b=22,
        # The first written map had these two relays backwards.
        led_start=24,
        led_stop=23,
        button_start=5,
        button_stop=6,
        i2c_devices=(
            I2cDevice("hts221", "HTS221", True),
            I2cDevice("lcd", "LCD", True),
        ),
    )


@dataclass(frozen=True)
class Channel:
    """One heater or probe on this controller.

    A probe's ``gpio`` is the chip select of its MAX31865. ``wires``, ``rtd``,
    and ``ref_ohms`` describe that converter and are left unset on heaters.
    """

    name: str
    enabled: bool
    gpio: int | None
    wires: int | None = None
    rtd: str | None = None
    ref_ohms: float | None = None

    def to_dict(self) -> dict:
        data = {"name": self.name, "enabled": self.enabled, "gpio": self.gpio}
        if self.rtd is not None:
            data["wires"] = self.wires
            data["rtd"] = self.rtd
            data["ref_ohms"] = self.ref_ohms
        return data


@dataclass
class Settings:
    heaters: list[Channel]
    sensors: list[Channel]
    target_temp_f: int
    heater_min_on_seconds: float = HEATER_MIN_ON_DEFAULT_SECONDS
    heater_min_off_seconds: float = HEATER_MIN_OFF_DEFAULT_SECONDS
    cabinet_limit_f: int = CABINET_LIMIT_DEFAULT_F
    learned_coast_f: float | None = None
    learned_coast_seconds: float | None = None
    learned_cooldown_f_per_min: float | None = None
    charge_gallons: float | None = None
    setup_complete: bool = False
    gpio: GpioConfig | None = None
    update_source: str = DEFAULT_UPDATE_SOURCE
    thermal_model: dict | None = None
    max_program_hours: int = DEFAULT_MAX_PROGRAM_HOURS
    program_mode: str = HOLD_MODE
    program_continuous: bool = True
    program_duration_seconds: int = DEFAULT_DURATION_SECONDS

    @property
    def heater_count(self) -> int:
        return sum(channel.enabled for channel in self.heaters)

    @property
    def sensor_count(self) -> int:
        return sum(channel.enabled for channel in self.sensors)

    @property
    def heater_names(self) -> list[str]:
        return [channel.name for channel in self.heaters]

    @property
    def sensor_names(self) -> list[str]:
        return [channel.name for channel in self.sensors]

    def enabled_heater_indexes(self) -> list[int]:
        return [index for index, channel in enumerate(self.heaters) if channel.enabled]

    def enabled_sensor_indexes(self) -> list[int]:
        """Enabled probes in slot order. The first one is the control probe."""

        return [index for index, channel in enumerate(self.sensors) if channel.enabled]

    def to_dict(self) -> dict:
        return {
            "target_temp_f": self.target_temp_f,
            "heater_min_on_seconds": self.heater_min_on_seconds,
            "heater_min_off_seconds": self.heater_min_off_seconds,
            "cabinet_limit_f": self.cabinet_limit_f,
            "learned_coast_f": self.learned_coast_f,
            "learned_coast_seconds": self.learned_coast_seconds,
            "learned_cooldown_f_per_min": self.learned_cooldown_f_per_min,
            "charge_gallons": self.charge_gallons,
            "setup_complete": self.setup_complete,
            "update_source": self.update_source,
            "gpio": (self.gpio or default_gpio()).to_dict(),
            "heaters": [channel.to_dict() for channel in self.heaters],
            "sensors": [channel.to_dict() for channel in self.sensors],
            "heater_count": self.heater_count,
            "sensor_count": self.sensor_count,
            "heater_names": self.heater_names,
            "sensor_names": self.sensor_names,
            "thermal_model": self.thermal_model,
            "max_program_hours": self.max_program_hours,
            "program_mode": self.program_mode,
            "program_continuous": self.program_continuous,
            "program_duration_seconds": self.program_duration_seconds,
        }


def default_settings() -> Settings:
    return Settings(
        heaters=[
            Channel("Heater 1", True, DEFAULT_HEATER_GPIOS[0]),
            Channel("Heater 2", True, DEFAULT_HEATER_GPIOS[1]),
        ],
        sensors=[
            _probe("Boiler", DEFAULT_SENSOR_ENABLED[0], DEFAULT_SENSOR_GPIOS[0]),
            _probe("Column", DEFAULT_SENSOR_ENABLED[1], DEFAULT_SENSOR_GPIOS[1]),
        ],
        gpio=default_gpio(),
        target_temp_f=165,
        heater_min_on_seconds=HEATER_MIN_ON_DEFAULT_SECONDS,
        heater_min_off_seconds=HEATER_MIN_OFF_DEFAULT_SECONDS,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def merge_settings(current: Settings, patch: dict) -> Settings:
    if not isinstance(patch, dict):
        raise SettingsError("Settings must be an object")
    allowed = {
        "heaters",
        "sensors",
        "target_temp_f",
        "heater_min_on_seconds",
        "heater_min_off_seconds",
        "cabinet_limit_f",
        "learned_coast_f",
        "learned_coast_seconds",
        "learned_cooldown_f_per_min",
        "charge_gallons",
        "setup_complete",
        "gpio",
        "update_source",
        "max_program_hours",
        "program_mode",
        "program_continuous",
        "program_duration_seconds",
    }
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise SettingsError(f"Unknown settings: {', '.join(unknown)}")
    merged = current.to_dict()
    merged.update(patch)
    return parse_settings(merged)


def parse_settings(data: dict) -> Settings:
    heaters = _channels(data.get("heaters"), "heaters")
    sensors = _channels(data.get("sensors"), "sensors")
    gpio = _gpio_config(data.get("gpio"))
    _check_pins(heaters, sensors, gpio)
    return Settings(
        heaters=heaters,
        sensors=sensors,
        target_temp_f=_target(data.get("target_temp_f")),
        heater_min_on_seconds=_dwell_seconds(
            data.get("heater_min_on_seconds"),
            "heater_min_on_seconds",
            HEATER_MIN_ON_DEFAULT_SECONDS,
        ),
        heater_min_off_seconds=_dwell_seconds(
            data.get("heater_min_off_seconds"),
            "heater_min_off_seconds",
            HEATER_MIN_OFF_DEFAULT_SECONDS,
        ),
        cabinet_limit_f=_cabinet_limit(data.get("cabinet_limit_f")),
        learned_coast_f=_optional_setting(
            data.get("learned_coast_f"), "learned_coast_f", EARLY_CUTOFF_MIN_F, EARLY_CUTOFF_MAX_F
        ),
        learned_coast_seconds=_optional_setting(
            data.get("learned_coast_seconds"), "learned_coast_seconds", 0.0, 3600.0
        ),
        learned_cooldown_f_per_min=_optional_setting(
            data.get("learned_cooldown_f_per_min"),
            "learned_cooldown_f_per_min",
            0.0,
            120.0,
        ),
        charge_gallons=_optional_charge(data.get("charge_gallons")),
        setup_complete=_flag(data.get("setup_complete")),
        gpio=gpio,
        update_source=_update_source(data.get("update_source")),
        thermal_model=_thermal_document(data.get("thermal_model")),
        max_program_hours=_max_program_hours(data.get("max_program_hours")),
        program_mode=_program_mode(data.get("program_mode")),
        program_continuous=_program_continuous(data.get("program_continuous")),
        program_duration_seconds=_program_duration(data.get("program_duration_seconds")),
    )


def shape_changed(before: Settings, after: Settings) -> bool:
    """True when heater, probe, or other GPIO wiring changed."""

    return (
        before.heaters != after.heaters
        or before.sensors != after.sensors
        or before.gpio != after.gpio
    )


class Store:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        if path.suffix != ".db":
            path.mkdir(parents=True, exist_ok=True)
            path = path / "brewski.db"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            had_meters = _table_exists(connection, "heater_meters")
            connection.executescript(SCHEMA)
            _migrate(connection)
            if not had_meters:
                _seed_heater_meters(connection)
            row = connection.execute("SELECT id FROM settings WHERE id = 1").fetchone()
            if row is None:
                self._insert_settings(connection, default_settings())
        self._backfill_timeline()
        self.compact_timeline()

    def get_settings(self) -> Settings:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if row is None:
            return default_settings()
        return _settings_from_row(row)

    def reset(self) -> None:
        """Erase runs and samples, and restore the factory settings.

        Element hours stay. They belong to the heating element, not to the
        run log, so clearing history does not make an old element look new.
        """

        with self._connect() as connection:
            connection.execute("DELETE FROM run_logs")
            connection.execute("DELETE FROM console_logs")
            connection.execute("DELETE FROM run_marks")
            connection.execute("DELETE FROM samples")
            connection.execute("DELETE FROM runs")
            connection.execute("DELETE FROM timeline_gaps")
            connection.execute("DELETE FROM timeline")
            connection.execute("DELETE FROM settings")
            self._insert_settings(connection, default_settings())

    def heater_life_seconds(self) -> list[float]:
        """Seconds each heater output has been on, by slot."""

        with self._connect() as connection:
            rows = connection.execute("SELECT slot, on_seconds FROM heater_meters").fetchall()
        by_slot = {int(row["slot"]): float(row["on_seconds"]) for row in rows}
        return [by_slot.get(index, 0.0) for index in range(SLOT_COUNT)]

    def add_heater_life(self, seconds: list[float]) -> None:
        """Add powered time to each heater. Negative deltas are ignored."""

        deltas = [
            max(0.0, float(seconds[index])) if index < len(seconds) else 0.0
            for index in range(SLOT_COUNT)
        ]
        if not any(deltas):
            return
        with self._connect() as connection:
            for index, delta in enumerate(deltas):
                if delta <= 0:
                    continue
                connection.execute(
                    """
                    INSERT INTO heater_meters (slot, on_seconds) VALUES (?, ?)
                    ON CONFLICT(slot) DO UPDATE SET on_seconds = on_seconds + excluded.on_seconds
                    """,
                    (index, delta),
                )

    def reset_heater_life(self, slot: int) -> None:
        """Zero one heater after its element is replaced."""

        slot = int(slot)
        if slot < 0 or slot >= SLOT_COUNT:
            raise SettingsError(f"Heater slot must be 0–{SLOT_COUNT - 1}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO heater_meters (slot, on_seconds) VALUES (?, 0)
                ON CONFLICT(slot) DO UPDATE SET on_seconds = 0
                """,
                (slot,),
            )

    def save_settings(self, settings: Settings) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE settings
                SET heater_count = ?, sensor_count = ?, heater_names = ?,
                    sensor_names = ?, target_temp_f = ?, heater_min_on_seconds = ?,
                    heater_min_off_seconds = ?, cabinet_limit_f = ?,
                    learned_coast_f = ?, learned_coast_seconds = ?, learned_cooldown_f_per_min = ?,
                    charge_gallons = ?,
                    heaters = ?, sensors = ?, setup_complete = ?, gpio = ?,
                    update_source = ?, thermal_model = ?,
                    max_program_hours = ?, program_mode = ?,
                    program_continuous = ?, program_duration_seconds = ?
                WHERE id = 1
                """,
                _settings_values(settings),
            )

    def create_run(
        self,
        settings: Settings,
        *,
        kind: str = "run",
        charge_gallons: float | None = None,
        program: object | None = None,
    ) -> dict:
        mode = None if program is None else program.mode
        continuous = None if program is None else (1 if program.continuous else 0)
        duration = None if program is None else program.duration_seconds
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    started_at, status, target_temp_f, heater_count, sensor_count,
                    heater_names, sensor_names, elapsed_seconds, heaters, sensors,
                    kind, charge_gallons, program_mode, program_continuous,
                    program_duration_seconds
                ) VALUES (?, 'running', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    settings.target_temp_f,
                    settings.heater_count,
                    settings.sensor_count,
                    json.dumps(settings.heater_names),
                    json.dumps(settings.sensor_names),
                    json.dumps([channel.to_dict() for channel in settings.heaters]),
                    json.dumps([channel.to_dict() for channel in settings.sensors]),
                    kind,
                    charge_gallons,
                    mode,
                    continuous,
                    duration,
                ),
            )
            run_id = int(cursor.lastrowid)
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError(f"Run {run_id} was not saved")
        return run

    def update_run(self, run_id: int, **fields: object) -> None:
        allowed = (
            "status",
            "elapsed_seconds",
            "ended_at",
            "target_temp_f",
            "cooldown_status",
            "cooldown_seconds",
            "cooldown_baseline_f",
            "cooldown_sensor_index",
            "cooldown_ended_at",
            "coast_f",
            "coast_seconds",
            "cooldown_f_per_min",
            "calibration_settled",
            "thermal_model",
            "analysis",
            "program_end",
        )
        keys = [key for key in allowed if key in fields]
        if not keys:
            return
        assignments = ", ".join(f"{key} = ?" for key in keys)
        values = [fields[key] for key in keys]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                [*values, run_id],
            )

    def add_sample(
        self,
        run_id: int,
        elapsed_seconds: float,
        temps_f: list[float],
        heaters_on: list[bool],
        target_temp_f: int,
        phase: str = "run",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO samples (
                    run_id, recorded_at, elapsed_seconds, temps_f, heaters_on, target_temp_f, phase
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now(),
                    elapsed_seconds,
                    json.dumps(temps_f),
                    json.dumps(heaters_on),
                    target_temp_f,
                    phase,
                ),
            )

    def add_timeline_point(
        self,
        temps_f: list[float | None],
        target_temp_f: float,
        recorded_ms: int | None = None,
    ) -> None:
        """Append one controller reading. A hole longer than 30 seconds is kept.

        The chart collapses holes longer than 30 minutes. Those are the
        stretches where this process was not running.
        """

        recorded_ms = int(time.time() * 1000) if recorded_ms is None else int(recorded_ms)
        payload = json.dumps(
            [None if value is None else round(float(value), 2) for value in temps_f]
        )
        with self._connect() as connection:
            last = connection.execute("SELECT MAX(recorded_ms) AS at FROM timeline").fetchone()
            previous = None if last is None or last["at"] is None else int(last["at"])
            if previous is not None and recorded_ms - previous > GAP_AFTER_MS:
                connection.execute(
                    "INSERT INTO timeline_gaps (started_ms, ended_ms) VALUES (?, ?)",
                    (previous, recorded_ms),
                )
            connection.execute(
                """
                INSERT INTO timeline (recorded_ms, temps_f, target_temp_f)
                VALUES (?, ?, ?)
                """,
                (recorded_ms, payload, round(float(target_temp_f), 2)),
            )

    def timeline(self, start_ms: int, end_ms: int, max_points: int) -> dict:
        """Readings in a real-time range, thinned to ``max_points`` when dense.

        Gaps are every recorded hole, not only those inside the range. The
        chart needs the whole list to place a window that crosses an outage.
        """

        max_points = max(1, int(max_points))
        start_ms = int(start_ms)
        end_ms = int(end_ms)
        with self._connect() as connection:
            bounds = connection.execute(
                "SELECT MIN(recorded_ms) AS earliest, MAX(recorded_ms) AS latest FROM timeline"
            ).fetchone()
            gap_rows = connection.execute(
                "SELECT started_ms, ended_ms FROM timeline_gaps ORDER BY started_ms, id"
            ).fetchall()
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS n FROM timeline
                    WHERE recorded_ms >= ? AND recorded_ms <= ?
                    """,
                    (start_ms, end_ms),
                ).fetchone()["n"]
            )
            if count <= max_points:
                rows = connection.execute(
                    """
                    SELECT id, recorded_ms, temps_f, target_temp_f
                    FROM timeline
                    WHERE recorded_ms >= ? AND recorded_ms <= ?
                    ORDER BY recorded_ms, id
                    """,
                    (start_ms, end_ms),
                ).fetchall()
                decimated = False
            else:
                stride = max(1, (count + max_points - 1) // max_points)
                rows = list(
                    connection.execute(
                        """
                        WITH numbered AS (
                            SELECT id, recorded_ms, temps_f, target_temp_f,
                                   ROW_NUMBER() OVER (ORDER BY recorded_ms, id) AS n
                            FROM timeline
                            WHERE recorded_ms >= ? AND recorded_ms <= ?
                        )
                        SELECT id, recorded_ms, temps_f, target_temp_f
                        FROM numbered
                        WHERE n = 1 OR n = ? OR (n % ?) = 1
                        ORDER BY recorded_ms, id
                        """,
                        (start_ms, end_ms, count, stride),
                    ).fetchall()
                )
                decimated = True
                have = {int(row["recorded_ms"]) for row in rows}
                for gap in gap_rows:
                    for edge in (int(gap["started_ms"]), int(gap["ended_ms"])):
                        if edge < start_ms or edge > end_ms or edge in have:
                            continue
                        nearest = _nearest_timeline_row(connection, edge, start_ms, end_ms)
                        if nearest is None or int(nearest["recorded_ms"]) in have:
                            continue
                        rows.append(nearest)
                        have.add(int(nearest["recorded_ms"]))
                rows.sort(key=lambda row: (int(row["recorded_ms"]), int(row["id"])))
        settings = self.get_settings()
        indexes = settings.enabled_sensor_indexes()
        gaps = [
            {"from": int(row["started_ms"]), "to": int(row["ended_ms"])} for row in gap_rows
        ]
        points = []
        for row in rows:
            stored = json.loads(row["temps_f"])
            temps = []
            for index in indexes:
                value = stored[index] if index < len(stored) else None
                temps.append(None if value is None else round(float(value), 2))
            points.append(
                {
                    "at": int(row["recorded_ms"]),
                    "temps_f": temps,
                    "target_temp_f": row["target_temp_f"],
                }
            )
        return {
            "earliest": None if bounds["earliest"] is None else int(bounds["earliest"]),
            "latest": None if bounds["latest"] is None else int(bounds["latest"]),
            "gaps": gaps,
            "points": points,
            "decimated": decimated,
        }

    def compact_timeline(self, now_ms: int | None = None) -> None:
        """Thin old readings. The last two days stay at the recorded rate.

        A point that opens or closes a hole is kept, and so are the first and
        last readings. Compaction does not invent holes.
        """

        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        day = 24 * 60 * 60 * 1000
        bands = (
            (2 * day, 14 * day, 60 * 1000),
            (14 * day, 180 * day, 5 * 60 * 1000),
            (180 * day, None, 30 * 60 * 1000),
        )
        with self._connect() as connection:
            for min_age, max_age, bucket in bands:
                clauses = ["recorded_ms < ?"]
                params: list[int] = [now_ms - min_age]
                if max_age is not None:
                    clauses.append("recorded_ms >= ?")
                    params.append(now_ms - max_age)
                where = " AND ".join(clauses)
                connection.execute(
                    f"""
                    DELETE FROM timeline
                    WHERE {where}
                    AND recorded_ms != (SELECT MIN(recorded_ms) FROM timeline)
                    AND recorded_ms != (SELECT MAX(recorded_ms) FROM timeline)
                    AND recorded_ms NOT IN (
                        SELECT started_ms FROM timeline_gaps
                        UNION
                        SELECT ended_ms FROM timeline_gaps
                    )
                    AND id NOT IN (
                        SELECT MIN(id) FROM timeline
                        WHERE {where}
                        GROUP BY recorded_ms / ?
                    )
                    """,
                    (*params, *params, bucket),
                )

    def _backfill_timeline(self) -> None:
        """Copy an existing sample log once, so older runs still scroll."""

        with self._connect() as connection:
            if not _table_exists(connection, "timeline") or not _table_exists(connection, "samples"):
                return
            count = connection.execute("SELECT COUNT(*) AS n FROM timeline").fetchone()
            if count is not None and int(count["n"]):
                return
            rows = connection.execute(
                """
                SELECT recorded_at, temps_f, target_temp_f
                FROM samples
                ORDER BY recorded_at, id
                """
            ).fetchall()
            previous: int | None = None
            for row in rows:
                recorded_ms = iso_to_ms(row["recorded_at"])
                if previous is not None and recorded_ms - previous > GAP_AFTER_MS:
                    connection.execute(
                        "INSERT INTO timeline_gaps (started_ms, ended_ms) VALUES (?, ?)",
                        (previous, recorded_ms),
                    )
                connection.execute(
                    """
                    INSERT INTO timeline (recorded_ms, temps_f, target_temp_f)
                    VALUES (?, ?, ?)
                    """,
                    (recorded_ms, row["temps_f"], row["target_temp_f"]),
                )
                previous = recorded_ms

    def get_run(
        self,
        run_id: int,
        *,
        include_logs: bool = True,
        include_samples: bool = True,
    ) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            samples = None
            if include_samples:
                samples = connection.execute(
                    """
                    SELECT * FROM samples
                    WHERE run_id = ?
                    ORDER BY recorded_at, id
                    """,
                    (run_id,),
                ).fetchall()
            else:
                on_seconds = _heater_on_seconds(connection, run_id, float(row["elapsed_seconds"]))
        run = _run_dict(row)
        if include_samples:
            sample_dicts = [_sample_dict(sample) for sample in samples or []]
            run["heater_on_seconds"] = heater_on_seconds(sample_dicts, run["elapsed_seconds"])
            run["samples"] = sample_dicts
        else:
            run["heater_on_seconds"] = on_seconds
        run["marks"] = self._marks_for(run_id)
        if include_logs:
            run["logs"] = self._logs_for(run_id)
        return run

    def heater_on_seconds_for(self, run_id: int, elapsed_seconds: float) -> list[float]:
        """Heater time for one run without copying its sample log into Python."""

        with self._connect() as connection:
            return _heater_on_seconds(connection, run_id, elapsed_seconds)

    def chart_samples(self, run_id: int, after_id: int = 0) -> list[dict] | None:
        """Readings for the run chart, newer than ``after_id``.

        Cooldown uses its own clock, so those rows stay off this series.
        None when the run does not exist.
        """

        with self._connect() as connection:
            found = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if found is None:
                return None
            rows = connection.execute(
                """
                SELECT id, elapsed_seconds, temps_f, target_temp_f, phase
                FROM samples
                WHERE run_id = ? AND id > ? AND phase != 'cooldown'
                ORDER BY id
                """,
                (run_id, max(0, int(after_id))),
            ).fetchall()
        return [_chart_sample(row) for row in rows]

    def review_window(
        self,
        run_id: int,
        *,
        limit: int,
        log_offset: int = 0,
        sample_offset: int = 0,
        from_elapsed: float | None = None,
        to_elapsed: float | None = None,
        clock: str = "run",
        events: list[str] | None = None,
        phases: list[str] | None = None,
        tail: bool = False,
    ) -> dict | None:
        """One page of the control log and the samples in the same stretch.

        With no ``from_elapsed``, the page of control-log rows chooses the
        stretch. With both ends set, that range is the stretch and both logs
        are paged inside it. None when the run does not exist.
        """

        custom = from_elapsed is not None and to_elapsed is not None
        with self._connect() as connection:
            found = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if found is None:
                return None
            bounds = _review_bounds(connection, run_id)
            where, params = _log_where(
                run_id,
                events or [],
                phases or [],
                from_elapsed if custom else None,
                to_elapsed if custom else None,
                clock if custom else None,
            )
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM run_logs WHERE {where}",
                    params,
                ).fetchone()["n"]
            )
            offset = _page_offset(total, limit, log_offset, tail)
            rows = connection.execute(
                f"""
                SELECT id, recorded_at, elapsed_seconds, event, message, detail
                FROM run_logs
                WHERE {where}
                ORDER BY id
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            logs = [_log_dict(row) for row in rows]
            if custom:
                window = _custom_window(float(from_elapsed), float(to_elapsed), clock)
            else:
                window = _window_from_logs(logs)
            sample_where, sample_params = _sample_where(run_id, window)
            sample_total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM samples WHERE {sample_where}",
                    sample_params,
                ).fetchone()["n"]
            )
            samples_offset = _page_offset(sample_total, limit, sample_offset, False)
            sample_rows = connection.execute(
                f"""
                SELECT *
                FROM samples
                WHERE {sample_where}
                ORDER BY CASE WHEN phase = 'cooldown' THEN 1 ELSE 0 END, elapsed_seconds, id
                LIMIT ? OFFSET ?
                """,
                (*sample_params, limit, samples_offset),
            ).fetchall()
        return {
            "mode": "custom" if custom else "page",
            "limit": int(limit),
            "bounds": bounds,
            "window": window,
            "logs": {"rows": logs, "total": total, "offset": offset},
            "samples": {
                "rows": [_sample_dict(row) for row in sample_rows],
                "total": sample_total,
                "offset": samples_offset,
            },
        }

    def claim_open_cooldown(self) -> dict | None:
        """Return the cooldown still in progress, and drop any older ones."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM runs
                WHERE cooldown_status = 'open'
                ORDER BY ended_at DESC, id DESC
                """
            ).fetchall()
            if not rows:
                return None
            keep_id = int(rows[0]["id"])
            for row in rows[1:]:
                connection.execute(
                    "UPDATE runs SET cooldown_status = 'abandoned' WHERE id = ?",
                    (int(row["id"]),),
                )
        return self.get_run(keep_id)

    def add_mark(self, run_id: int, name: str, elapsed_seconds: float) -> None:
        """Record one named chart line. A second write of the same name is ignored."""

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM run_marks WHERE run_id = ? AND name = ?",
                (run_id, name),
            ).fetchone()
            if existing is not None:
                return
            connection.execute(
                """
                INSERT INTO run_marks (run_id, name, elapsed_seconds, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, name, round(float(elapsed_seconds), 2), utc_now()),
            )

    def add_run_log(
        self,
        run_id: int,
        elapsed_seconds: float | None,
        event: str,
        message: str,
        detail: dict | None = None,
    ) -> None:
        """Attach one diagnostic line to a run.

        ``elapsed_seconds`` is the run clock, or the cooldown clock once the
        pot is cooling. A line written after the run has closed, such as a
        late analysis, uses the elapsed time already stored on the run.
        """

        with self._connect() as connection:
            if elapsed_seconds is None:
                row = connection.execute(
                    "SELECT elapsed_seconds FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    return
                elapsed_seconds = float(row["elapsed_seconds"])
            connection.execute(
                """
                INSERT INTO run_logs (
                    run_id, recorded_at, elapsed_seconds, event, message, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now(),
                    round(float(elapsed_seconds), 2),
                    event,
                    message,
                    json.dumps(detail or {}),
                ),
            )

    def run_kind(self, run_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT kind FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return row["kind"] or "run"

    def run_logs(self, run_id: int, after_id: int = 0) -> list[dict] | None:
        """Control-log lines with an id above ``after_id``. None if the run is missing."""

        with self._connect() as connection:
            found = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if found is None:
                return None
            rows = connection.execute(
                """
                SELECT id, recorded_at, elapsed_seconds, event, message, detail
                FROM run_logs
                WHERE run_id = ? AND id > ?
                ORDER BY id
                """,
                (run_id, max(0, int(after_id))),
            ).fetchall()
        return [_log_dict(row) for row in rows]

    def add_console_line(self, run_id: int, line: str) -> None:
        """Attach one console line to a run. A missing run is ignored."""

        with self._connect() as connection:
            found = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if found is None:
                return
            connection.execute(
                """
                INSERT INTO console_logs (run_id, recorded_at, line)
                VALUES (?, ?, ?)
                """,
                (run_id, utc_now(), line),
            )

    def console_lines(self, run_id: int) -> list[str] | None:
        """Console lines for a run, in the order they were written. None if it is missing."""

        with self._connect() as connection:
            found = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if found is None:
                return None
            rows = connection.execute(
                """
                SELECT line FROM console_logs
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return [row["line"] for row in rows]

    def _logs_for(self, run_id: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, recorded_at, elapsed_seconds, event, message, detail
                FROM run_logs
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return [_log_dict(row) for row in rows]

    def append_analysis(self, run_id: int, entry: dict) -> None:
        """Append one Grok comparison. The local fit is kept when Grok is absent."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT analysis FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return
            items = _json_list(row["analysis"])
            items.append(entry)
            connection.execute(
                "UPDATE runs SET analysis = ? WHERE id = ?",
                (json.dumps(items), run_id),
            )

    def _marks_for(self, run_id: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT name, elapsed_seconds, recorded_at
                FROM run_marks
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return [_mark_dict(row) for row in rows]

    def list_runs(
        self,
        live_run: dict | None = None,
        live_cooldown: dict | None = None,
    ) -> dict:
        with self._connect() as connection:
            run_rows = connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, id DESC"
            ).fetchall()
            # Heater time is the gap between samples. Summing it here keeps the
            # dashboard from copying every reading into Python once a second.
            on_rows = connection.execute(
                """
                WITH ordered AS (
                    SELECT run_id, elapsed_seconds, heaters_on,
                           LEAD(elapsed_seconds) OVER (
                               PARTITION BY run_id ORDER BY elapsed_seconds, id
                           ) AS next_elapsed
                    FROM samples
                    WHERE phase != 'cooldown'
                )
                SELECT run_id,
                       SUM(
                           CASE
                               WHEN json_extract(heaters_on, '$[0]') = 1 AND next_elapsed IS NOT NULL
                               THEN max(0, next_elapsed - elapsed_seconds)
                               ELSE 0
                           END
                       ) AS on0,
                       SUM(
                           CASE
                               WHEN json_extract(heaters_on, '$[1]') = 1 AND next_elapsed IS NOT NULL
                               THEN max(0, next_elapsed - elapsed_seconds)
                               ELSE 0
                           END
                       ) AS on1
                FROM ordered
                GROUP BY run_id
                """
            ).fetchall()
            tail_rows = connection.execute(
                """
                SELECT run_id, elapsed_seconds, heaters_on
                FROM (
                    SELECT run_id, elapsed_seconds, heaters_on,
                           ROW_NUMBER() OVER (
                               PARTITION BY run_id ORDER BY elapsed_seconds DESC, id DESC
                           ) AS n
                    FROM samples
                    WHERE phase != 'cooldown'
                )
                WHERE n = 1
                """
            ).fetchall()
            mark_rows = connection.execute(
                """
                SELECT run_id, name, elapsed_seconds, recorded_at
                FROM run_marks
                ORDER BY run_id, id
                """
            ).fetchall()

        on_by_run = {
            int(row["run_id"]): [float(row["on0"] or 0.0), float(row["on1"] or 0.0)]
            for row in on_rows
        }
        tail_by_run = {int(row["run_id"]): row for row in tail_rows}
        marks_by_run: dict[int, list[dict]] = {}
        for row in mark_rows:
            marks_by_run.setdefault(int(row["run_id"]), []).append(_mark_dict(row))

        runs = []
        for row in run_rows:
            run = _run_dict(row)
            run["marks"] = marks_by_run.get(run["id"], [])
            if live_run is not None and live_run["id"] == run["id"]:
                run["elapsed_seconds"] = round(float(live_run["elapsed_seconds"]), 2)
                run["status"] = live_run["status"]
            if live_cooldown is not None and live_cooldown["id"] == run["id"]:
                run["cooldown_seconds"] = round(float(live_cooldown["cooldown_seconds"]), 2)
                run["cooldown_status"] = live_cooldown["cooldown_status"]
            totals = list(on_by_run.get(run["id"], [0.0, 0.0]))
            tail = tail_by_run.get(run["id"])
            if tail is not None:
                dt = max(0.0, float(run["elapsed_seconds"]) - float(tail["elapsed_seconds"]))
                _add_on_time(totals, json.loads(tail["heaters_on"]), dt)
            run["heater_on_seconds"] = totals
            runs.append(run)

        production = [run for run in runs if run["kind"] == "run"]
        calibration_count = sum(1 for run in runs if run["kind"] == "calibration")
        finished = [run for run in production if run["status"] == "stopped"]
        average = 0.0
        if finished:
            average = sum(run["elapsed_seconds"] for run in finished) / len(finished)
        # Every finished cooldown, including a calibration.
        completed_cooldowns = [
            float(run["cooldown_seconds"])
            for run in runs
            if run["cooldown_status"] == "complete" and run["cooldown_seconds"] is not None
        ]
        average_cooldown = None
        if completed_cooldowns:
            average_cooldown = sum(completed_cooldowns) / len(completed_cooldowns)
        heater_totals = [0.0, 0.0]
        for run in production:
            heater_totals[0] += run["heater_on_seconds"][0]
            heater_totals[1] += run["heater_on_seconds"][1]
        return {
            "stats": {
                "total_runs": len(production),
                "calibration_runs": calibration_count,
                "average_duration_seconds": average,
                "average_cooldown_seconds": average_cooldown,
                "heater_on_seconds": heater_totals,
                "heater_life_seconds": self.heater_life_seconds(),
            },
            "runs": runs,
        }

    def close_open_runs(self) -> None:
        """Mark runs left open by a crash or shutdown as stopped."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = 'stopped', ended_at = COALESCE(ended_at, ?)
                WHERE status IN ('running', 'paused')
                """,
                (utc_now(),),
            )

    @contextmanager
    def _connect(self):
        """One short transaction. The file stays in WAL so a chart read does not block a sample write."""

        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_settings(self, connection: sqlite3.Connection, settings: Settings) -> None:
        connection.execute(
            """
            INSERT INTO settings (
                id, heater_count, sensor_count, heater_names, sensor_names,
                target_temp_f, heater_min_on_seconds, heater_min_off_seconds,
                cabinet_limit_f, learned_coast_f, learned_coast_seconds,
                learned_cooldown_f_per_min, charge_gallons, heaters, sensors, setup_complete, gpio,
                update_source, thermal_model, max_program_hours, program_mode,
                program_continuous, program_duration_seconds
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _settings_values(settings),
        )


def _nearest_timeline_row(
    connection: sqlite3.Connection,
    moment: int,
    start_ms: int,
    end_ms: int,
) -> sqlite3.Row | None:
    after = connection.execute(
        """
        SELECT id, recorded_ms, temps_f, target_temp_f
        FROM timeline
        WHERE recorded_ms >= ? AND recorded_ms <= ?
        ORDER BY recorded_ms, id
        LIMIT 1
        """,
        (moment, end_ms),
    ).fetchone()
    before = connection.execute(
        """
        SELECT id, recorded_ms, temps_f, target_temp_f
        FROM timeline
        WHERE recorded_ms <= ? AND recorded_ms >= ?
        ORDER BY recorded_ms DESC, id DESC
        LIMIT 1
        """,
        (moment, start_ms),
    ).fetchone()
    if after is None:
        return before
    if before is None:
        return after
    if abs(int(after["recorded_ms"]) - moment) < abs(int(before["recorded_ms"]) - moment):
        return after
    return before


def heater_on_seconds(samples: list[dict], elapsed_seconds: float) -> list[float]:
    """Time each heater spent on, estimated from the sample log.

    A sample's heater state is treated as holding until the next sample.
    The gap after the last sample uses that same state, up to the run's
    elapsed time. Paused time is not included because the clock is frozen.
    """

    totals = [0.0, 0.0]
    samples = [sample for sample in samples if sample.get("phase", "run") != "cooldown"]
    if not samples:
        return totals
    for previous, nxt in zip(samples, samples[1:]):
        dt = max(0.0, float(nxt["elapsed_seconds"]) - float(previous["elapsed_seconds"]))
        _add_on_time(totals, previous["heaters_on"], dt)
    tail = max(0.0, float(elapsed_seconds) - float(samples[-1]["elapsed_seconds"]))
    _add_on_time(totals, samples[-1]["heaters_on"], tail)
    return totals


def _add_on_time(totals: list[float], heaters_on: list[bool], dt: float) -> None:
    for index, on in enumerate(heaters_on[:2]):
        if on:
            totals[index] += dt


def _run_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "status": row["status"],
        "elapsed_seconds": round(float(row["elapsed_seconds"]), 2),
        "target_temp_f": _stored_target(row["target_temp_f"]),
        "cooldown_status": row["cooldown_status"] if "cooldown_status" in row.keys() else None,
        "cooldown_seconds": _optional_float(row, "cooldown_seconds"),
        "cooldown_baseline_f": _optional_float(row, "cooldown_baseline_f"),
        "cooldown_sensor_index": _optional_int(row, "cooldown_sensor_index"),
        "cooldown_ended_at": row["cooldown_ended_at"] if "cooldown_ended_at" in row.keys() else None,
        "heater_count": int(row["heater_count"]),
        "sensor_count": int(row["sensor_count"]),
        "heater_names": json.loads(row["heater_names"]),
        "sensor_names": json.loads(row["sensor_names"]),
        "heaters": [channel.to_dict() for channel in _channels_from_row(row, "heaters", "heater")],
        "sensors": [channel.to_dict() for channel in _channels_from_row(row, "sensors", "sensor")],
        "kind": row["kind"] if "kind" in row.keys() and row["kind"] else "run",
        "charge_gallons": _optional_float(row, "charge_gallons"),
        "coast_f": _optional_float(row, "coast_f"),
        "coast_seconds": _optional_float(row, "coast_seconds"),
        "cooldown_f_per_min": _optional_float(row, "cooldown_f_per_min"),
        "calibration_settled": bool(row["calibration_settled"])
        if "calibration_settled" in row.keys() and row["calibration_settled"]
        else False,
        "thermal_model": _json_object(row["thermal_model"]) if "thermal_model" in row.keys() else None,
        "analysis": _json_list(row["analysis"]) if "analysis" in row.keys() else [],
        "program_mode": row["program_mode"]
        if "program_mode" in row.keys() and row["program_mode"]
        else None,
        "program_continuous": _optional_bool(row, "program_continuous"),
        "program_duration_seconds": _optional_int(row, "program_duration_seconds"),
        "program_end": row["program_end"] if "program_end" in row.keys() and row["program_end"] else None,
        "marks": [],
    }


def _heater_on_seconds(connection: sqlite3.Connection, run_id: int, elapsed_seconds: float) -> list[float]:
    """Same estimate as :func:`heater_on_seconds`, summed in SQLite."""

    row = connection.execute(
        """
        WITH ordered AS (
            SELECT elapsed_seconds, heaters_on,
                   LEAD(elapsed_seconds) OVER (
                       ORDER BY elapsed_seconds, id
                   ) AS next_elapsed
            FROM samples
            WHERE run_id = ? AND phase != 'cooldown'
        )
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN json_extract(heaters_on, '$[0]') = 1 AND next_elapsed IS NOT NULL
                    THEN max(0, next_elapsed - elapsed_seconds)
                    ELSE 0
                END
            ), 0) AS on0,
            COALESCE(SUM(
                CASE
                    WHEN json_extract(heaters_on, '$[1]') = 1 AND next_elapsed IS NOT NULL
                    THEN max(0, next_elapsed - elapsed_seconds)
                    ELSE 0
                END
            ), 0) AS on1
        FROM ordered
        """,
        (run_id,),
    ).fetchone()
    totals = [float(row["on0"] or 0), float(row["on1"] or 0)]
    tail = connection.execute(
        """
        SELECT elapsed_seconds, heaters_on
        FROM samples
        WHERE run_id = ? AND phase != 'cooldown'
        ORDER BY elapsed_seconds DESC, id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if tail is None:
        return [0.0, 0.0]
    gap = max(0.0, float(elapsed_seconds) - float(tail["elapsed_seconds"]))
    _add_on_time(totals, json.loads(tail["heaters_on"]), gap)
    return totals


def _chart_sample(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "elapsed_seconds": round(float(row["elapsed_seconds"]), 2),
        "temps_f": json.loads(row["temps_f"]),
        "target_temp_f": _stored_target(row["target_temp_f"]),
        "phase": row["phase"] if row["phase"] else "run",
    }


def _review_bounds(connection: sqlite3.Connection, run_id: int) -> dict:
    row = connection.execute(
        """
        SELECT
            MIN(CASE WHEN phase != 'cooldown' THEN elapsed_seconds END) AS run_from,
            MAX(CASE WHEN phase != 'cooldown' THEN elapsed_seconds END) AS run_to,
            MIN(CASE WHEN phase = 'cooldown' THEN elapsed_seconds END) AS cooldown_from,
            MAX(CASE WHEN phase = 'cooldown' THEN elapsed_seconds END) AS cooldown_to
        FROM samples
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    bounds = {
        "from": _optional_round(row["run_from"]),
        "to": _optional_round(row["run_to"]),
        "cooldown_from": _optional_round(row["cooldown_from"]),
        "cooldown_to": _optional_round(row["cooldown_to"]),
    }
    if bounds["from"] is None and bounds["cooldown_from"] is None:
        logs = connection.execute(
            """
            SELECT MIN(elapsed_seconds) AS start_at, MAX(elapsed_seconds) AS end_at
            FROM run_logs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        bounds["from"] = _optional_round(logs["start_at"])
        bounds["to"] = _optional_round(logs["end_at"])
    return bounds


def _optional_round(value: object) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _page_offset(total: int, limit: int, offset: int, tail: bool) -> int:
    if total <= 0 or limit <= 0:
        return 0
    last = ((int(total) - 1) // int(limit)) * int(limit)
    if tail:
        return last
    if offset < 0:
        return 0
    if offset > last:
        return last
    return int(offset)


_COOLDOWN_LOG = (
    "(json_extract(detail, '$.path') = 'cooldown' "
    "OR json_extract(detail, '$.phase') = 'cooldown')"
)


def _log_where(
    run_id: int,
    events: list[str],
    phases: list[str],
    from_elapsed: float | None,
    to_elapsed: float | None,
    clock: str | None,
) -> tuple[str, list[object]]:
    clauses = ["run_id = ?"]
    params: list[object] = [run_id]
    if events:
        marks = ", ".join("?" for _ in events)
        clauses.append(f"event IN ({marks})")
        params.extend(events)
    if phases:
        marks = ", ".join("?" for _ in phases)
        clauses.append(
            "COALESCE(json_extract(detail, '$.path'), json_extract(detail, '$.phase'), '') "
            f"IN ({marks})"
        )
        params.extend(phases)
    if from_elapsed is not None and to_elapsed is not None:
        clauses.append("elapsed_seconds >= ? AND elapsed_seconds <= ?")
        params.extend([from_elapsed, to_elapsed])
    if clock == "run":
        clauses.append(f"NOT {_COOLDOWN_LOG}")
    elif clock == "cooldown":
        clauses.append(_COOLDOWN_LOG)
    return " AND ".join(clauses), params


def _is_cooldown_log(detail: dict) -> bool:
    return detail.get("path") == "cooldown" or detail.get("phase") == "cooldown"


def _window_from_logs(logs: list[dict]) -> dict:
    run_times = []
    cool_times = []
    for item in logs:
        elapsed = round(float(item["elapsed_seconds"]), 2)
        if _is_cooldown_log(item.get("detail") or {}):
            cool_times.append(elapsed)
        else:
            run_times.append(elapsed)
    return {
        "from": min(run_times) if run_times else None,
        "to": max(run_times) if run_times else None,
        "cooldown_from": min(cool_times) if cool_times else None,
        "cooldown_to": max(cool_times) if cool_times else None,
    }


def _custom_window(start: float, end: float, clock: str) -> dict:
    window = {"from": None, "to": None, "cooldown_from": None, "cooldown_to": None}
    span = (round(float(start), 2), round(float(end), 2))
    if clock in {"run", "both"}:
        window["from"], window["to"] = span
    if clock in {"cooldown", "both"}:
        window["cooldown_from"], window["cooldown_to"] = span
    return window


def _sample_where(run_id: int, window: dict) -> tuple[str, list[object]]:
    parts = []
    params: list[object] = [run_id]
    if window["from"] is not None and window["to"] is not None:
        parts.append("(phase != 'cooldown' AND elapsed_seconds >= ? AND elapsed_seconds <= ?)")
        params.extend([window["from"], window["to"]])
    if window["cooldown_from"] is not None and window["cooldown_to"] is not None:
        parts.append("(phase = 'cooldown' AND elapsed_seconds >= ? AND elapsed_seconds <= ?)")
        params.extend([window["cooldown_from"], window["cooldown_to"]])
    if not parts:
        return "run_id = ? AND 0", params
    return f"run_id = ? AND ({' OR '.join(parts)})", params


def _log_dict(row: sqlite3.Row) -> dict:
    detail: dict = {}
    raw = row["detail"]
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            detail = parsed
    return {
        "id": int(row["id"]),
        "recorded_at": row["recorded_at"],
        "elapsed_seconds": round(float(row["elapsed_seconds"]), 2),
        "event": row["event"],
        "message": row["message"],
        "detail": detail,
    }


def _sample_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "recorded_at": row["recorded_at"],
        "elapsed_seconds": round(float(row["elapsed_seconds"]), 2),
        "temps_f": json.loads(row["temps_f"]),
        "heaters_on": json.loads(row["heaters_on"]),
        "target_temp_f": _stored_target(row["target_temp_f"]),
        "phase": row["phase"] if "phase" in row.keys() and row["phase"] else "run",
    }


def _migrate(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the first database layout."""

    for table in ("settings", "runs"):
        for column in ("heaters", "sensors"):
            _add_column(connection, table, column)
    _add_column(connection, "runs", "cooldown_status")
    _add_column(connection, "runs", "cooldown_seconds", "REAL")
    _add_column(connection, "runs", "cooldown_baseline_f", "REAL")
    _add_column(connection, "runs", "cooldown_sensor_index", "INTEGER")
    _add_column(connection, "runs", "cooldown_ended_at")
    _add_column(connection, "settings", "heater_min_on_seconds", "REAL")
    _add_column(connection, "settings", "heater_min_off_seconds", "REAL")
    _add_column(connection, "settings", "early_cutoff_f", "REAL")
    _add_column(connection, "settings", "cabinet_limit_f", "REAL")
    _add_column(connection, "settings", "learned_coast_f", "REAL")
    _add_column(connection, "settings", "learned_coast_seconds", "REAL")
    _add_column(connection, "settings", "learned_cooldown_f_per_min", "REAL")
    _add_column(connection, "settings", "charge_gallons", "REAL")
    _add_column(connection, "runs", "kind", "TEXT")
    _add_column(connection, "runs", "charge_gallons", "REAL")
    _add_column(connection, "runs", "coast_f", "REAL")
    _add_column(connection, "runs", "coast_seconds", "REAL")
    _add_column(connection, "runs", "cooldown_f_per_min", "REAL")
    _add_column(connection, "runs", "calibration_settled", "INTEGER")
    _add_column(connection, "settings", "thermal_model")
    _add_column(connection, "runs", "thermal_model")
    _add_column(connection, "runs", "analysis")
    if _table_exists(connection, "samples"):
        _add_column(connection, "samples", "phase")
    settings_columns = {row[1] for row in connection.execute("PRAGMA table_info(settings)")}
    adding_setup = "setup_complete" not in settings_columns
    _add_column(connection, "settings", "setup_complete", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "settings", "gpio")
    _add_column(connection, "settings", "update_source", "TEXT NOT NULL DEFAULT 'main'")
    _add_column(connection, "settings", "max_program_hours", "INTEGER NOT NULL DEFAULT 24")
    _add_column(connection, "settings", "program_mode", "TEXT NOT NULL DEFAULT 'hold'")
    _add_column(connection, "settings", "program_continuous", "INTEGER NOT NULL DEFAULT 1")
    _add_column(
        connection,
        "settings",
        "program_duration_seconds",
        "INTEGER NOT NULL DEFAULT 3600",
    )
    _add_column(connection, "runs", "program_mode")
    _add_column(connection, "runs", "program_continuous", "INTEGER")
    _add_column(connection, "runs", "program_duration_seconds", "INTEGER")
    _add_column(connection, "runs", "program_end")
    _swap_documented_led_pins(connection)
    if not adding_setup or not _table_exists(connection, "runs"):
        return
    has_runs = connection.execute("SELECT 1 FROM runs LIMIT 1").fetchone()
    has_coast = connection.execute(
        "SELECT 1 FROM settings WHERE learned_coast_f IS NOT NULL LIMIT 1"
    ).fetchone()
    if has_runs or has_coast:
        connection.execute("UPDATE settings SET setup_complete = 1")


def _swap_documented_led_pins(connection: sqlite3.Connection) -> None:
    """Correct a saved map that still has the lamp relays reversed.

    Start is BCM 24 and Stop is BCM 23. Only that old pair is rewritten, and
    only once: after the swap the row no longer matches.
    """

    if not _table_exists(connection, "settings"):
        return
    columns = {row[1] for row in connection.execute("PRAGMA table_info(settings)")}
    if "gpio" not in columns:
        return
    row = connection.execute("SELECT gpio FROM settings WHERE id = 1").fetchone()
    if row is None or row[0] is None:
        return
    try:
        gpio = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return
    leds = gpio.get("leds") if isinstance(gpio, dict) else None
    if not isinstance(leds, dict) or leds.get("start") != 23 or leds.get("stop") != 24:
        return
    leds["start"] = 24
    leds["stop"] = 23
    connection.execute(
        "UPDATE settings SET gpio = ? WHERE id = 1",
        (json.dumps(gpio),),
    )


def _seed_heater_meters(connection: sqlite3.Connection) -> None:
    """Fill element hours from the sample log the first time the meter exists.

    Later time is added by the controller as the output is actually on.
    Seeding again would count those same runs twice, so an existing row wins.
    Cooldown samples are skipped: the heaters are off while the pot cools.
    Calibration is included, because the element was powered.
    """

    existing = connection.execute("SELECT 1 FROM heater_meters LIMIT 1").fetchone()
    if existing is not None:
        return
    totals = [0.0, 0.0]
    if _table_exists(connection, "runs") and _table_exists(connection, "samples"):
        run_rows = connection.execute("SELECT id, elapsed_seconds FROM runs").fetchall()
        sample_rows = connection.execute(
            "SELECT * FROM samples ORDER BY run_id, elapsed_seconds, id"
        ).fetchall()
        samples_by_run: dict[int, list[dict]] = {}
        for row in sample_rows:
            samples_by_run.setdefault(int(row["run_id"]), []).append(_sample_dict(row))
        for row in run_rows:
            seconds = heater_on_seconds(
                samples_by_run.get(int(row["id"]), []),
                float(row["elapsed_seconds"] or 0),
            )
            totals[0] += seconds[0]
            totals[1] += seconds[1]
    for slot, seconds in enumerate(totals):
        connection.execute(
            "INSERT INTO heater_meters (slot, on_seconds) VALUES (?, ?)",
            (slot, seconds),
        )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str = "TEXT",
) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _mark_dict(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "elapsed_seconds": round(float(row["elapsed_seconds"]), 2),
        "recorded_at": row["recorded_at"],
    }


def _json_object(value: object) -> dict | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return parsed


def _thermal_document(value: object) -> dict | None:
    return _json_object(value)


def _optional_float(row: sqlite3.Row, column: str) -> float | None:
    if column not in row.keys() or row[column] is None:
        return None
    return round(float(row[column]), 2)


def _optional_int(row: sqlite3.Row, column: str) -> int | None:
    if column not in row.keys() or row[column] is None:
        return None
    return int(row[column])


def _optional_bool(row: sqlite3.Row, column: str) -> bool | None:
    if column not in row.keys() or row[column] is None:
        return None
    return bool(row[column])


def _settings_values(settings: Settings) -> tuple:
    return (
        settings.heater_count,
        settings.sensor_count,
        json.dumps(settings.heater_names),
        json.dumps(settings.sensor_names),
        settings.target_temp_f,
        settings.heater_min_on_seconds,
        settings.heater_min_off_seconds,
        settings.cabinet_limit_f,
        settings.learned_coast_f,
        settings.learned_coast_seconds,
        settings.learned_cooldown_f_per_min,
        settings.charge_gallons,
        json.dumps([channel.to_dict() for channel in settings.heaters]),
        json.dumps([channel.to_dict() for channel in settings.sensors]),
        1 if settings.setup_complete else 0,
        json.dumps((settings.gpio or default_gpio()).to_dict()),
        settings.update_source,
        None if settings.thermal_model is None else json.dumps(settings.thermal_model),
        settings.max_program_hours,
        settings.program_mode,
        1 if settings.program_continuous else 0,
        settings.program_duration_seconds,
    )


def _settings_from_row(row: sqlite3.Row) -> Settings:
    heaters = _channels_from_row(row, "heaters", "heater", default_gpios=DEFAULT_HEATER_GPIOS)
    gpio = _gpio_from_row(row)
    sensors = _align_probe_pins(
        _channels_from_row(row, "sensors", "sensor", default_gpios=DEFAULT_SENSOR_GPIOS, probe=True),
        heaters,
        gpio,
    )
    return Settings(
        heaters=heaters,
        sensors=sensors,
        target_temp_f=_clamped_target(row["target_temp_f"]),
        heater_min_on_seconds=_stored_float(
            row, "heater_min_on_seconds", HEATER_MIN_ON_DEFAULT_SECONDS
        ),
        heater_min_off_seconds=_stored_float(
            row, "heater_min_off_seconds", HEATER_MIN_OFF_DEFAULT_SECONDS
        ),
        cabinet_limit_f=_stored_cabinet_limit(row),
        learned_coast_f=_optional_float(row, "learned_coast_f"),
        learned_coast_seconds=_optional_float(row, "learned_coast_seconds"),
        learned_cooldown_f_per_min=_optional_float(row, "learned_cooldown_f_per_min"),
        charge_gallons=_optional_float(row, "charge_gallons"),
        setup_complete=_stored_flag(row, "setup_complete"),
        gpio=gpio,
        update_source=_stored_update_source(row),
        thermal_model=_json_object(row["thermal_model"]) if "thermal_model" in row.keys() else None,
        max_program_hours=_stored_max_program_hours(row),
        program_mode=_stored_program_mode(row),
        program_continuous=_stored_program_continuous(row),
        program_duration_seconds=_stored_program_duration(row),
    )


def load_settings(path: Path) -> Settings | None:
    """Read settings without creating or migrating the database.

    A missing file, or a file that is not a Brewski database yet, means setup
    has not saved the I2C pins. Callers leave the LCD closed in that case.
    """

    path = Path(path)
    if path.suffix != ".db":
        path = path / "brewski.db"
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    return _settings_from_row(row)


def _gpio_from_row(row: sqlite3.Row) -> GpioConfig:
    """Load the pin map. A database from before the column existed uses the defaults."""

    if "gpio" not in row.keys() or row["gpio"] is None:
        return default_gpio()
    return _gpio_config(json.loads(row["gpio"]))


def _probe(
    name: str,
    enabled: bool,
    gpio: int | None,
    wires: int = DEFAULT_WIRES,
    rtd: str = DEFAULT_RTD,
    ref_ohms: float | None = None,
) -> Channel:
    return Channel(
        name,
        enabled,
        gpio,
        wires,
        rtd,
        DEFAULT_REF_OHMS[rtd] if ref_ohms is None else ref_ohms,
    )


def _channels_from_row(
    row: sqlite3.Row,
    column: str,
    prefix: str,
    default_gpios: tuple[int, int] | None = None,
    probe: bool = False,
) -> list[Channel]:
    """Load a device list, or rebuild it from the older count-and-names columns."""

    raw = row[column] if column in row.keys() else None
    if raw:
        return [_channel_from_stored(item, probe=probe) for item in json.loads(raw)]
    names = json.loads(row[f"{prefix}_names"])
    count = int(row[f"{prefix}_count"])
    channels = []
    for index in range(SLOT_COUNT):
        gpio = None if default_gpios is None else default_gpios[index]
        if probe:
            channels.append(_probe(_fit_probe_name(names[index]), index < count, gpio))
        else:
            channels.append(Channel(names[index], index < count, gpio))
    return channels


def _channel_from_stored(item: dict, *, probe: bool = False) -> Channel:
    gpio = item.get("gpio")
    pin = None if gpio is None else int(gpio)
    if not probe:
        return Channel(name=item["name"], enabled=bool(item["enabled"]), gpio=pin)
    item = {**item, "name": _fit_probe_name(item["name"])}
    if any(key in item for key in ("wires", "rtd", "ref_ohms")):
        wires, rtd, ref_ohms = _probe_config(item, "sensors")
    else:
        wires, rtd, ref_ohms = DEFAULT_WIRES, DEFAULT_RTD, DEFAULT_REF_OHMS[DEFAULT_RTD]
    return Channel(
        name=item["name"],
        enabled=bool(item["enabled"]),
        gpio=pin,
        wires=wires,
        rtd=rtd,
        ref_ohms=ref_ohms,
    )


def _channels(value: object, field: str) -> list[Channel]:
    label = "heater" if field == "heaters" else "temperature probe"
    probe = field == "sensors"
    allowed = {"name", "enabled", "gpio"}
    if probe:
        allowed |= {"wires", "rtd", "ref_ohms"}
    if not isinstance(value, list) or len(value) != SLOT_COUNT:
        raise SettingsError(f"{field} must be a list of {SLOT_COUNT} {label}s")
    channels = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SettingsError(f"{field}[{index}] must be an object")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise SettingsError(f"Unknown {field}[{index}] settings: {', '.join(unknown)}")
        wires = rtd = ref_ohms = None
        if probe:
            wires, rtd, ref_ohms = _probe_config(item, f"{field}[{index}]")
        channels.append(
            Channel(
                name=_name(
                    item.get("name"),
                    f"{field}[{index}].name",
                    PROBE_NAME_MAX if probe else MAX_NAME_LENGTH,
                ),
                enabled=_enabled(item.get("enabled"), f"{field}[{index}].enabled"),
                gpio=_gpio(item.get("gpio"), f"{field}[{index}].gpio"),
                wires=wires,
                rtd=rtd,
                ref_ohms=ref_ohms,
            )
        )
    if not any(channel.enabled for channel in channels):
        raise SettingsError(f"One {label} must stay enabled")
    return channels


def _probe_config(item: dict, field: str) -> tuple[int, str, float]:
    rtd = item.get("rtd", DEFAULT_RTD)
    if rtd not in DEFAULT_REF_OHMS:
        raise SettingsError(f"{field}.rtd must be pt100 or pt1000")
    wires = item.get("wires", DEFAULT_WIRES)
    if isinstance(wires, bool) or not isinstance(wires, int) or wires not in (2, 3, 4):
        raise SettingsError(f"{field}.wires must be 2, 3, or 4")
    ref_ohms = item.get("ref_ohms", DEFAULT_REF_OHMS[rtd])
    if isinstance(ref_ohms, bool) or not isinstance(ref_ohms, (int, float)):
        raise SettingsError(f"{field}.ref_ohms must be a number of ohms")
    ohms = round(float(ref_ohms), 2)
    if ohms < REF_OHMS_MIN or ohms > REF_OHMS_MAX:
        raise SettingsError(
            f"{field}.ref_ohms must be between {REF_OHMS_MIN:.0f} and {REF_OHMS_MAX:.0f}"
        )
    return wires, rtd, ohms


def _align_probe_pins(
    sensors: list[Channel], heaters: list[Channel], gpio: GpioConfig
) -> list[Channel]:
    """Give each probe a chip select a MAX31865 can use.

    Older settings put both probes on the 1-wire pin. A shared pin, or one
    already taken by a heater or by the rest of the GPIO map, is replaced
    with a free chip select. The next save stores that assignment. A pin that
    is already valid is left alone.
    """

    occupied = _occupied_pins(heaters, gpio)
    pins = [sensor.gpio for sensor in sensors]
    shared = {pin for pin in pins if pins.count(pin) > 1}
    used: set[int] = set()
    aligned = []
    for index, sensor in enumerate(sensors):
        pin = sensor.gpio
        if pin in shared or not _chip_select_free(pin, used, occupied):
            pin = _free_chip_select(used, occupied, DEFAULT_SENSOR_GPIOS[index])
        used.add(pin)
        aligned.append(replace(sensor, gpio=pin))
    return aligned


def _occupied_pins(heaters: list[Channel], gpio: GpioConfig) -> set[int]:
    pins = {channel.gpio for channel in heaters if isinstance(channel.gpio, int)}
    pins.update(pin for _, pin in gpio.pins())
    return pins


def _chip_select_free(pin: int | None, used: set[int], occupied: set[int]) -> bool:
    return isinstance(pin, int) and GPIO_MIN <= pin <= GPIO_MAX and pin not in occupied and pin not in used


def _free_chip_select(used: set[int], occupied: set[int], preferred: int) -> int:
    candidates = (preferred, *DEFAULT_SENSOR_GPIOS, *range(GPIO_MIN, GPIO_MAX + 1))
    for pin in candidates:
        if _chip_select_free(pin, used, occupied):
            return pin
    raise SettingsError("No chip select is free for a temperature probe")


def _check_pins(heaters: list[Channel], sensors: list[Channel], gpio: GpioConfig) -> None:
    claimed: dict[int, str] = {}
    for label, pin in gpio.pins():
        if pin in claimed:
            raise SettingsError(f"BCM {pin} is shared by {claimed[pin]} and {label}")
        claimed[pin] = label
    spi = gpio.spi_pins()
    for channel in (*heaters, *sensors):
        if channel.gpio in spi:
            listed = _bcm_list(sorted(spi))
            verb = "is" if len(spi) == 1 else "are"
            raise SettingsError(
                f"BCM {listed} {verb} the SPI bus shared by the temperature probes"
            )
    heater_pins = [channel.gpio for channel in heaters]
    if len(set(heater_pins)) != len(heater_pins):
        raise SettingsError("Each heater needs its own GPIO pin")
    sensor_pins = [channel.gpio for channel in sensors]
    if len(set(sensor_pins)) != len(sensor_pins):
        raise SettingsError("Each temperature probe needs its own chip select")
    if any(pin in sensor_pins for pin in heater_pins):
        raise SettingsError("A heater and a probe cannot share a GPIO pin")
    roles = [(f"heater {index + 1}", channel.gpio) for index, channel in enumerate(heaters)]
    roles += [(f"probe {index + 1}", channel.gpio) for index, channel in enumerate(sensors)]
    for label, pin in roles:
        if pin in claimed and pin not in spi:
            raise SettingsError(f"BCM {pin} is shared by {claimed[pin]} and {label}")
        claimed[pin] = label


def _bcm_list(pins: list[int]) -> str:
    labels = [str(pin) for pin in pins]
    if len(labels) <= 1:
        return labels[0] if labels else ""
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


_GPIO_GROUPS = {
    "spi": ("clk", "sdo", "sdi"),
    "i2c": ("sda", "scl"),
    "encoder": ("a", "b"),
    "leds": ("start", "stop"),
    "buttons": ("start", "stop"),
}


def _gpio_config(value: object) -> GpioConfig:
    if value is None:
        return default_gpio()
    if not isinstance(value, dict):
        raise SettingsError("gpio must be an object")
    allowed = set(_GPIO_GROUPS) | {"i2c_devices"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SettingsError(f"Unknown gpio settings: {', '.join(unknown)}")
    groups = {name: _pin_group(value.get(name), f"gpio.{name}", keys) for name, keys in _GPIO_GROUPS.items()}
    spi = groups["spi"]
    i2c = groups["i2c"]
    encoder = groups["encoder"]
    leds = groups["leds"]
    buttons = groups["buttons"]
    return GpioConfig(
        spi_clk=spi["clk"],
        spi_sdo=spi["sdo"],
        spi_sdi=spi["sdi"],
        i2c_sda=i2c["sda"],
        i2c_scl=i2c["scl"],
        encoder_a=encoder["a"],
        encoder_b=encoder["b"],
        led_start=leds["start"],
        led_stop=leds["stop"],
        button_start=buttons["start"],
        button_stop=buttons["stop"],
        i2c_devices=_i2c_devices(value.get("i2c_devices")),
    )


def _pin_group(value: object, field: str, keys: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict):
        raise SettingsError(f"{field} must be an object")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise SettingsError(f"Unknown {field} settings: {', '.join(unknown)}")
    missing = [key for key in keys if key not in value]
    if missing:
        raise SettingsError(f"{field} needs {', '.join(missing)}")
    return {key: _gpio(value[key], f"{field}.{key}") for key in keys}


def _i2c_devices(value: object) -> tuple[I2cDevice, ...]:
    if not isinstance(value, list) or len(value) != len(I2C_DEVICE_IDS):
        raise SettingsError("gpio.i2c_devices must list the HTS221 and the LCD")
    devices = []
    for index, expected in enumerate(I2C_DEVICE_IDS):
        item = value[index]
        field = f"gpio.i2c_devices[{index}]"
        if not isinstance(item, dict):
            raise SettingsError(f"{field} must be an object")
        unknown = sorted(set(item) - {"id", "name", "enabled"})
        if unknown:
            raise SettingsError(f"Unknown {field} settings: {', '.join(unknown)}")
        if item.get("id") != expected:
            raise SettingsError(f"{field}.id must be {expected}")
        devices.append(
            I2cDevice(
                expected,
                _name(item.get("name"), f"{field}.name"),
                _enabled(item.get("enabled"), f"{field}.enabled"),
            )
        )
    return tuple(devices)


def _update_source(value: object) -> str:
    if value is None or value == "":
        return DEFAULT_UPDATE_SOURCE
    if not isinstance(value, str):
        raise SettingsError("Update source must be main or beta")
    source = value.strip().lower()
    if source not in UPDATE_SOURCES:
        raise SettingsError("Update source must be main or beta")
    return source


def _stored_update_source(row: sqlite3.Row) -> str:
    if "update_source" not in row.keys() or not row["update_source"]:
        return DEFAULT_UPDATE_SOURCE
    source = str(row["update_source"]).strip().lower()
    if source not in UPDATE_SOURCES:
        return DEFAULT_UPDATE_SOURCE
    return source


def _program_value(parse, value: object, default: object) -> object:
    if value is None:
        return default
    try:
        return parse(value)
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc


def _max_program_hours(value: object) -> int:
    return _program_value(parse_max_program_hours, value, DEFAULT_MAX_PROGRAM_HOURS)


def _program_mode(value: object) -> str:
    return _program_value(parse_program_mode, value, HOLD_MODE)


def _program_continuous(value: object) -> bool:
    return _program_value(parse_program_continuous, value, True)


def _program_duration(value: object) -> int:
    return _program_value(parse_program_duration, value, DEFAULT_DURATION_SECONDS)


def _stored_max_program_hours(row: sqlite3.Row) -> int:
    if "max_program_hours" not in row.keys() or row["max_program_hours"] is None:
        return DEFAULT_MAX_PROGRAM_HOURS
    try:
        return parse_max_program_hours(int(row["max_program_hours"]))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PROGRAM_HOURS


def _stored_program_mode(row: sqlite3.Row) -> str:
    if "program_mode" not in row.keys() or not row["program_mode"]:
        return HOLD_MODE
    try:
        return parse_program_mode(str(row["program_mode"]))
    except ValueError:
        return HOLD_MODE


def _stored_program_continuous(row: sqlite3.Row) -> bool:
    if "program_continuous" not in row.keys() or row["program_continuous"] is None:
        return True
    return bool(row["program_continuous"])


def _stored_program_duration(row: sqlite3.Row) -> int:
    if "program_duration_seconds" not in row.keys() or row["program_duration_seconds"] is None:
        return DEFAULT_DURATION_SECONDS
    try:
        return parse_program_duration(int(row["program_duration_seconds"]))
    except (TypeError, ValueError):
        return DEFAULT_DURATION_SECONDS


def _flag(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise SettingsError("setup_complete must be true or false")


def _stored_flag(row: sqlite3.Row, column: str) -> bool:
    if column not in row.keys() or row[column] is None:
        return False
    return bool(row[column])


def _name(value: object, field: str, limit: int = MAX_NAME_LENGTH) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        raise SettingsError(f"{field} must be 1 to {limit} characters")
    return cleaned


def _fit_probe_name(name: str) -> str:
    """Cut a stored probe name so the LCD can still show ``: `` and the reading."""

    cleaned = str(name).strip()[:PROBE_NAME_MAX]
    return cleaned or "Probe"


def _enabled(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise SettingsError(f"{field} must be true or false")
    return value


def _gpio(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError(f"{field} must be a BCM pin from {GPIO_MIN} to {GPIO_MAX}")
    if value < GPIO_MIN or value > GPIO_MAX:
        raise SettingsError(f"{field} must be a BCM pin from {GPIO_MIN} to {GPIO_MAX}")
    return value


def _stored_float(row: sqlite3.Row, column: str, default: float) -> float:
    """A saved number. Databases from before the column existed use the default."""

    if column not in row.keys() or row[column] is None:
        return default
    return float(row[column])


def _dwell_seconds(value: object, field: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{field} must be a number of seconds")
    seconds = round(float(value), 2)
    if seconds < HEATER_DWELL_FLOOR_SECONDS or seconds > HEATER_DWELL_CEILING_SECONDS:
        raise SettingsError(
            f"{field} must be between {HEATER_DWELL_FLOOR_SECONDS:g} and {HEATER_DWELL_CEILING_SECONDS:g}"
        )
    return seconds


def charge_gallons(value: object) -> float:
    """Gallons of liquid in the pot for a calibration."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("charge_gallons must be a number of gallons")
    gallons = round(float(value), 2)
    if gallons < CHARGE_MIN_GALLONS or gallons > CHARGE_MAX_GALLONS:
        raise SettingsError(
            "charge_gallons must be between "
            f"{CHARGE_MIN_GALLONS:g} and {CHARGE_MAX_GALLONS:g}"
        )
    return gallons


def _optional_charge(value: object) -> float | None:
    if value is None:
        return None
    return charge_gallons(value)


def _optional_setting(value: object, field: str, low: float, high: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{field} must be a number")
    number = round(float(value), 2)
    if number < low or number > high:
        raise SettingsError(f"{field} must be between {low:g} and {high:g}")
    return number


def _stored_target(value: object) -> int:
    """Whole degrees already saved. Older rows may be fractional."""

    return int(round(float(value)))


def _clamped_target(value: object) -> int:
    """A saved target brought inside the current dial range."""

    target = _stored_target(value)
    return min(int(TARGET_MAX_F), max(int(TARGET_MIN_F), target))


def _cabinet_limit(value: object) -> int:
    if value is None:
        return CABINET_LIMIT_DEFAULT_F
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("cabinet_limit_f must be a whole number")
    if isinstance(value, float) and not value.is_integer():
        raise SettingsError("cabinet_limit_f must be a whole number")
    limit = int(value)
    if limit < CABINET_LIMIT_MIN_F or limit > CABINET_LIMIT_MAX_F:
        raise SettingsError(
            "cabinet_limit_f must be a whole number between "
            f"{CABINET_LIMIT_MIN_F} and {CABINET_LIMIT_MAX_F}"
        )
    return limit


def _stored_cabinet_limit(row: sqlite3.Row) -> int:
    if "cabinet_limit_f" not in row.keys() or row["cabinet_limit_f"] is None:
        return CABINET_LIMIT_DEFAULT_F
    limit = int(round(float(row["cabinet_limit_f"])))
    return min(CABINET_LIMIT_MAX_F, max(CABINET_LIMIT_MIN_F, limit))


def _target(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("target_temp_f must be a whole number")
    if isinstance(value, float) and not value.is_integer():
        raise SettingsError("target_temp_f must be a whole number")
    target = int(value)
    if target < TARGET_MIN_F or target > TARGET_MAX_F:
        raise SettingsError(
            f"target_temp_f must be a whole number between {TARGET_MIN_F:.0f} and {TARGET_MAX_F:.0f}"
        )
    return target
