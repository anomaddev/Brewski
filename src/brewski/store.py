"""SQLite storage for settings, runs, and the sample log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TARGET_MIN_F = 40.0
TARGET_MAX_F = 212.0
MAX_NAME_LENGTH = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heater_count INTEGER NOT NULL,
    sensor_count INTEGER NOT NULL,
    heater_names TEXT NOT NULL,
    sensor_names TEXT NOT NULL,
    target_temp_f REAL NOT NULL
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
    elapsed_seconds REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    recorded_at TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    temps_f TEXT NOT NULL,
    heaters_on TEXT NOT NULL,
    target_temp_f REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS samples_run_id ON samples(run_id, elapsed_seconds);
"""


class SettingsError(ValueError):
    """The settings document is missing a field or has a value the still cannot use."""


@dataclass
class Settings:
    heater_count: int
    sensor_count: int
    heater_names: list[str]
    sensor_names: list[str]
    target_temp_f: float

    def to_dict(self) -> dict:
        return {
            "heater_count": self.heater_count,
            "sensor_count": self.sensor_count,
            "heater_names": list(self.heater_names),
            "sensor_names": list(self.sensor_names),
            "target_temp_f": self.target_temp_f,
        }


def default_settings() -> Settings:
    return Settings(
        heater_count=2,
        sensor_count=2,
        heater_names=["Heater 1", "Heater 2"],
        sensor_names=["Boiler", "Column"],
        target_temp_f=165.0,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def merge_settings(current: Settings, patch: dict) -> Settings:
    if not isinstance(patch, dict):
        raise SettingsError("Settings must be an object")
    allowed = {"heater_count", "sensor_count", "heater_names", "sensor_names", "target_temp_f"}
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise SettingsError(f"Unknown settings: {', '.join(unknown)}")
    merged = current.to_dict()
    merged.update(patch)
    return parse_settings(merged)


def parse_settings(data: dict) -> Settings:
    return Settings(
        heater_count=_count(data.get("heater_count"), "heater_count"),
        sensor_count=_count(data.get("sensor_count"), "sensor_count"),
        heater_names=_names(data.get("heater_names"), "heater_names"),
        sensor_names=_names(data.get("sensor_names"), "sensor_names"),
        target_temp_f=_target(data.get("target_temp_f")),
    )


def shape_changed(before: Settings, after: Settings) -> bool:
    """True when the number or names of heaters or sensors changed."""

    return (
        before.heater_count != after.heater_count
        or before.sensor_count != after.sensor_count
        or before.heater_names != after.heater_names
        or before.sensor_names != after.sensor_names
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
            connection.executescript(SCHEMA)
            row = connection.execute("SELECT id FROM settings WHERE id = 1").fetchone()
            if row is None:
                self._insert_settings(connection, default_settings())

    def get_settings(self) -> Settings:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if row is None:
            return default_settings()
        return Settings(
            heater_count=int(row["heater_count"]),
            sensor_count=int(row["sensor_count"]),
            heater_names=json.loads(row["heater_names"]),
            sensor_names=json.loads(row["sensor_names"]),
            target_temp_f=float(row["target_temp_f"]),
        )

    def save_settings(self, settings: Settings) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE settings
                SET heater_count = ?, sensor_count = ?, heater_names = ?,
                    sensor_names = ?, target_temp_f = ?
                WHERE id = 1
                """,
                (
                    settings.heater_count,
                    settings.sensor_count,
                    json.dumps(settings.heater_names),
                    json.dumps(settings.sensor_names),
                    settings.target_temp_f,
                ),
            )

    def create_run(self, settings: Settings) -> dict:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    started_at, status, target_temp_f, heater_count, sensor_count,
                    heater_names, sensor_names, elapsed_seconds
                ) VALUES (?, 'running', ?, ?, ?, ?, ?, 0)
                """,
                (
                    utc_now(),
                    settings.target_temp_f,
                    settings.heater_count,
                    settings.sensor_count,
                    json.dumps(settings.heater_names),
                    json.dumps(settings.sensor_names),
                ),
            )
            run_id = int(cursor.lastrowid)
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError(f"Run {run_id} was not saved")
        return run

    def update_run(self, run_id: int, **fields: object) -> None:
        allowed = ("status", "elapsed_seconds", "ended_at", "target_temp_f")
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
        target_temp_f: float,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO samples (
                    run_id, recorded_at, elapsed_seconds, temps_f, heaters_on, target_temp_f
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now(),
                    elapsed_seconds,
                    json.dumps(temps_f),
                    json.dumps(heaters_on),
                    target_temp_f,
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
                ORDER BY elapsed_seconds, id
                """,
                (run_id,),
            ).fetchall()
        run = _run_dict(row)
        sample_dicts = [_sample_dict(sample) for sample in samples]
        run["heater_on_seconds"] = heater_on_seconds(sample_dicts, run["elapsed_seconds"])
        run["samples"] = sample_dicts
        return run

    def list_runs(self, live_run: dict | None = None) -> dict:
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
            samples = samples_by_run.get(run["id"], [])
            run["heater_on_seconds"] = heater_on_seconds(samples, run["elapsed_seconds"])
            runs.append(run)

        finished = [run for run in runs if run["status"] == "stopped"]
        average = 0.0
        if finished:
            average = sum(run["elapsed_seconds"] for run in finished) / len(finished)
        heater_totals = [0.0, 0.0]
        for run in runs:
            heater_totals[0] += run["heater_on_seconds"][0]
            heater_totals[1] += run["heater_on_seconds"][1]
        return {
            "stats": {
                "total_runs": len(runs),
                "average_duration_seconds": average,
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
                id, heater_count, sensor_count, heater_names, sensor_names, target_temp_f
            ) VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                settings.heater_count,
                settings.sensor_count,
                json.dumps(settings.heater_names),
                json.dumps(settings.sensor_names),
                settings.target_temp_f,
            ),
        )


def heater_on_seconds(samples: list[dict], elapsed_seconds: float) -> list[float]:
    """Time each heater spent on, estimated from the sample log.

    A sample's heater state is treated as holding until the next sample.
    The gap after the last sample uses that same state, up to the run's
    elapsed time. Paused time is not included because the clock is frozen.
    """

    totals = [0.0, 0.0]
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
        "target_temp_f": float(row["target_temp_f"]),
        "heater_count": int(row["heater_count"]),
        "sensor_count": int(row["sensor_count"]),
        "heater_names": json.loads(row["heater_names"]),
        "sensor_names": json.loads(row["sensor_names"]),
    }


def _sample_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "recorded_at": row["recorded_at"],
        "elapsed_seconds": round(float(row["elapsed_seconds"]), 2),
        "temps_f": json.loads(row["temps_f"]),
        "heaters_on": json.loads(row["heaters_on"]),
        "target_temp_f": float(row["target_temp_f"]),
    }


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):
        raise SettingsError(f"{field} must be 1 or 2")
    return value


def _names(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) != 2:
        raise SettingsError(f"{field} must be a list of two names")
    names = []
    for item in value:
        if not isinstance(item, str):
            raise SettingsError(f"{field} must be text")
        cleaned = item.strip()
        if not cleaned or len(cleaned) > MAX_NAME_LENGTH:
            raise SettingsError(f"{field} entries must be 1 to {MAX_NAME_LENGTH} characters")
        names.append(cleaned)
    return names


def _target(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("target_temp_f must be a number")
    target = float(value)
    if target < TARGET_MIN_F or target > TARGET_MAX_F:
        raise SettingsError(
            f"target_temp_f must be between {TARGET_MIN_F:.0f} and {TARGET_MAX_F:.0f}"
        )
    return target
