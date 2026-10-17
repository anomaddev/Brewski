"""SQLite storage for settings, runs, and the sample log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TARGET_MIN_F = 40.0
TARGET_MAX_F = 250.0
MAX_NAME_LENGTH = 40
SLOT_COUNT = 2
# BCM numbers on the 40-pin header. GPIO 0 and 1 are the board ID EEPROM.
GPIO_MIN = 2
GPIO_MAX = 27
# Common relay pins, and the usual 1-wire pin for both probes.
DEFAULT_HEATER_GPIOS = (17, 27)
DEFAULT_SENSOR_GPIOS = (4, 4)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heater_count INTEGER NOT NULL,
    sensor_count INTEGER NOT NULL,
    heater_names TEXT NOT NULL,
    sensor_names TEXT NOT NULL,
    target_temp_f REAL NOT NULL,
    heaters TEXT,
    sensors TEXT
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
    cooldown_status TEXT,
    cooldown_seconds REAL,
    cooldown_baseline_f REAL,
    cooldown_sensor_index INTEGER,
    cooldown_ended_at TEXT
);

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
"""


class SettingsError(ValueError):
    """The settings document is missing a field or has a value the still cannot use."""


@dataclass(frozen=True)
class Channel:
    """One heater or probe on this controller."""

    name: str
    enabled: bool
    gpio: int | None

    def to_dict(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "gpio": self.gpio}


@dataclass
class Settings:
    heaters: list[Channel]
    sensors: list[Channel]
    target_temp_f: int

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
            "heaters": [channel.to_dict() for channel in self.heaters],
            "sensors": [channel.to_dict() for channel in self.sensors],
            "heater_count": self.heater_count,
            "sensor_count": self.sensor_count,
            "heater_names": self.heater_names,
            "sensor_names": self.sensor_names,
        }


def default_settings() -> Settings:
    return Settings(
        heaters=[
            Channel("Heater 1", True, DEFAULT_HEATER_GPIOS[0]),
            Channel("Heater 2", True, DEFAULT_HEATER_GPIOS[1]),
        ],
        sensors=[
            Channel("Boiler", True, DEFAULT_SENSOR_GPIOS[0]),
            Channel("Column", True, DEFAULT_SENSOR_GPIOS[1]),
        ],
        target_temp_f=165,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def merge_settings(current: Settings, patch: dict) -> Settings:
    if not isinstance(patch, dict):
        raise SettingsError("Settings must be an object")
    allowed = {"heaters", "sensors", "target_temp_f"}
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise SettingsError(f"Unknown settings: {', '.join(unknown)}")
    merged = current.to_dict()
    merged.update(patch)
    return parse_settings(merged)


def parse_settings(data: dict) -> Settings:
    heaters = _channels(data.get("heaters"), "heaters")
    sensors = _channels(data.get("sensors"), "sensors")
    _check_pins(heaters, sensors)
    return Settings(
        heaters=heaters,
        sensors=sensors,
        target_temp_f=_target(data.get("target_temp_f")),
    )


def shape_changed(before: Settings, after: Settings) -> bool:
    """True when names, enable flags, or GPIO pins changed."""

    return before.heaters != after.heaters or before.sensors != after.sensors


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
            connection.executescript(SCHEMA)
            _migrate(connection)
            row = connection.execute("SELECT id FROM settings WHERE id = 1").fetchone()
            if row is None:
                self._insert_settings(connection, default_settings())

    def get_settings(self) -> Settings:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if row is None:
            return default_settings()
        return _settings_from_row(row)

    def save_settings(self, settings: Settings) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE settings
                SET heater_count = ?, sensor_count = ?, heater_names = ?,
                    sensor_names = ?, target_temp_f = ?, heaters = ?, sensors = ?
                WHERE id = 1
                """,
                _settings_values(settings),
            )

    def create_run(self, settings: Settings) -> dict:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    started_at, status, target_temp_f, heater_count, sensor_count,
                    heater_names, sensor_names, elapsed_seconds, heaters, sensors
                ) VALUES (?, 'running', ?, ?, ?, ?, ?, 0, ?, ?)
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

    def get_run(self, run_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            samples = connection.execute(
                """
                SELECT * FROM samples
                WHERE run_id = ?
                ORDER BY recorded_at, id
                """,
                (run_id,),
            ).fetchall()
        run = _run_dict(row)
        sample_dicts = [_sample_dict(sample) for sample in samples]
        run["heater_on_seconds"] = heater_on_seconds(sample_dicts, run["elapsed_seconds"])
        run["samples"] = sample_dicts
        return run

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

    def list_runs(
        self,
        live_run: dict | None = None,
        live_cooldown: dict | None = None,
    ) -> dict:
        with self._connect() as connection:
            run_rows = connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, id DESC"
            ).fetchall()
            sample_rows = connection.execute(
                "SELECT * FROM samples ORDER BY run_id, elapsed_seconds, id"
            ).fetchall()

        samples_by_run: dict[int, list[dict]] = {}
        for row in sample_rows:
            samples_by_run.setdefault(int(row["run_id"]), []).append(_sample_dict(row))

        runs = []
        for row in run_rows:
            run = _run_dict(row)
            if live_run is not None and live_run["id"] == run["id"]:
                run["elapsed_seconds"] = round(float(live_run["elapsed_seconds"]), 2)
                run["status"] = live_run["status"]
            if live_cooldown is not None and live_cooldown["id"] == run["id"]:
                run["cooldown_seconds"] = round(float(live_cooldown["cooldown_seconds"]), 2)
                run["cooldown_status"] = live_cooldown["cooldown_status"]
            samples = samples_by_run.get(run["id"], [])
            run["heater_on_seconds"] = heater_on_seconds(samples, run["elapsed_seconds"])
            runs.append(run)

        finished = [run for run in runs if run["status"] == "stopped"]
        average = 0.0
        if finished:
            average = sum(run["elapsed_seconds"] for run in finished) / len(finished)
        completed_cooldowns = [
            float(run["cooldown_seconds"])
            for run in runs
            if run["cooldown_status"] == "complete" and run["cooldown_seconds"] is not None
        ]
        average_cooldown = None
        if completed_cooldowns:
            average_cooldown = sum(completed_cooldowns) / len(completed_cooldowns)
        heater_totals = [0.0, 0.0]
        for run in runs:
            heater_totals[0] += run["heater_on_seconds"][0]
            heater_totals[1] += run["heater_on_seconds"][1]
        return {
            "stats": {
                "total_runs": len(runs),
                "average_duration_seconds": average,
                "average_cooldown_seconds": average_cooldown,
                "heater_on_seconds": heater_totals,
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _insert_settings(self, connection: sqlite3.Connection, settings: Settings) -> None:
        connection.execute(
            """
            INSERT INTO settings (
                id, heater_count, sensor_count, heater_names, sensor_names,
                target_temp_f, heaters, sensors
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            _settings_values(settings),
        )


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
    if _table_exists(connection, "samples"):
        _add_column(connection, "samples", "phase")


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


def _optional_float(row: sqlite3.Row, column: str) -> float | None:
    if column not in row.keys() or row[column] is None:
        return None
    return round(float(row[column]), 2)


def _optional_int(row: sqlite3.Row, column: str) -> int | None:
    if column not in row.keys() or row[column] is None:
        return None
    return int(row[column])


def _settings_values(settings: Settings) -> tuple:
    return (
        settings.heater_count,
        settings.sensor_count,
        json.dumps(settings.heater_names),
        json.dumps(settings.sensor_names),
        settings.target_temp_f,
        json.dumps([channel.to_dict() for channel in settings.heaters]),
        json.dumps([channel.to_dict() for channel in settings.sensors]),
    )


def _settings_from_row(row: sqlite3.Row) -> Settings:
    return Settings(
        heaters=_channels_from_row(row, "heaters", "heater", default_gpios=DEFAULT_HEATER_GPIOS),
        sensors=_channels_from_row(row, "sensors", "sensor", default_gpios=DEFAULT_SENSOR_GPIOS),
        target_temp_f=_stored_target(row["target_temp_f"]),
    )


def _channels_from_row(
    row: sqlite3.Row,
    column: str,
    prefix: str,
    default_gpios: tuple[int, int] | None = None,
) -> list[Channel]:
    """Load a device list, or rebuild it from the older count-and-names columns."""

    raw = row[column] if column in row.keys() else None
    if raw:
        return [_channel_from_stored(item) for item in json.loads(raw)]
    names = json.loads(row[f"{prefix}_names"])
    count = int(row[f"{prefix}_count"])
    channels = []
    for index in range(SLOT_COUNT):
        gpio = None if default_gpios is None else default_gpios[index]
        channels.append(Channel(names[index], index < count, gpio))
    return channels


def _channel_from_stored(item: dict) -> Channel:
    gpio = item.get("gpio")
    return Channel(
        name=item["name"],
        enabled=bool(item["enabled"]),
        gpio=None if gpio is None else int(gpio),
    )


def _channels(value: object, field: str) -> list[Channel]:
    label = "heater" if field == "heaters" else "temperature probe"
    if not isinstance(value, list) or len(value) != SLOT_COUNT:
        raise SettingsError(f"{field} must be a list of {SLOT_COUNT} {label}s")
    channels = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SettingsError(f"{field}[{index}] must be an object")
        unknown = sorted(set(item) - {"name", "enabled", "gpio"})
        if unknown:
            raise SettingsError(f"Unknown {field}[{index}] settings: {', '.join(unknown)}")
        channels.append(
            Channel(
                name=_name(item.get("name"), f"{field}[{index}].name"),
                enabled=_enabled(item.get("enabled"), f"{field}[{index}].enabled"),
                gpio=_gpio(item.get("gpio"), f"{field}[{index}].gpio"),
            )
        )
    if not any(channel.enabled for channel in channels):
        raise SettingsError(f"One {label} must stay enabled")
    return channels


def _check_pins(heaters: list[Channel], sensors: list[Channel]) -> None:
    heater_pins = [channel.gpio for channel in heaters]
    if len(set(heater_pins)) != len(heater_pins):
        raise SettingsError("Each heater needs its own GPIO pin")
    sensor_pins = {channel.gpio for channel in sensors}
    if any(pin in sensor_pins for pin in heater_pins):
        raise SettingsError("A heater and a probe cannot share a GPIO pin")


def _name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_NAME_LENGTH:
        raise SettingsError(f"{field} must be 1 to {MAX_NAME_LENGTH} characters")
    return cleaned


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


def _stored_target(value: object) -> int:
    """Whole degrees already saved. Older rows may be fractional."""

    return int(round(float(value)))


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
