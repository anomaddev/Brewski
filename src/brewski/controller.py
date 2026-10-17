"""Run state machine.

The loop is hardware-agnostic. It reads a :class:`HardwareSource`, decides
whether the heaters should be on, and appends samples while a run is going.
Sensor 1 (index 0) is the probe the target temperature is compared with.
"""

from __future__ import annotations

import logging
import threading
import time

from brewski.sources.base import HardwareSource
from brewski.store import (
    Settings,
    SettingsError,
    Store,
    heater_on_seconds,
    merge_settings,
    shape_changed,
    utc_now,
)

logger = logging.getLogger(__name__)

# Heaters turn on at this many degrees below the target and off at the target.
DEADBAND_F = 2.0
SAMPLE_INTERVAL_SECONDS = 5.0
# Ignore a stalled clock that tries to replay more than an hour in one tick.
MAX_TICK_SECONDS = 3600.0


class ControllerError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Controller:
    def __init__(self, store: Store, source: HardwareSource) -> None:
        self.store = store
        self.source = source
        self._lock = threading.Lock()
        self._stop_loop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shut_down = False
        self._mode = "idle"
        self._run_id: int | None = None
        self._elapsed = 0.0
        self._since_sample = 0.0
        self._heaters = [False, False]
        # A previous process may have stopped mid-run. Close that record so
        # the next start is a new run, not a continuation of a dead one.
        self.store.close_open_runs()
        self._live_temps = self.source.read_temperatures(2)
        self.source.set_heaters([False, False])

    def start_loop(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_loop.clear()
        self._thread = threading.Thread(target=self._loop, name="brewski-controller", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        self._stop_loop.set()
        with self._lock:
            if self._mode in ("running", "paused"):
                self._finish_run()
            else:
                self.source.set_heaters([False, False])

    def tick(self, dt: float) -> None:
        """Advance the simulation and the run clock by ``dt`` seconds."""

        remaining = min(max(0.0, float(dt)), MAX_TICK_SECONDS)
        with self._lock:
            while remaining > 0:
                slice_dt = min(1.0, remaining)
                self._tick(slice_dt)
                remaining -= slice_dt

    def status(self) -> dict:
        with self._lock:
            return self._status()

    def runs(self) -> dict:
        with self._lock:
            return self.store.list_runs(self._live_overlay())

    def run(self, run_id: int) -> dict:
        with self._lock:
            record = self.store.get_run(run_id)
            if record is None:
                raise ControllerError("Run not found", 404)
            overlay = self._live_overlay()
            if overlay is not None and overlay["id"] == run_id:
                record["elapsed_seconds"] = round(self._elapsed, 2)
                record["status"] = self._mode
                # The database elapsed lags the in-memory clock between samples.
                record["heater_on_seconds"] = heater_on_seconds(
                    record["samples"], record["elapsed_seconds"]
                )
            return record

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            current = self.store.get_settings()
            try:
                updated = merge_settings(current, patch)
            except SettingsError as exc:
                raise ControllerError(str(exc), 400) from exc
            if self._mode != "idle" and shape_changed(current, updated):
                raise ControllerError(
                    "Heater and sensor setup is locked while a run is open",
                    409,
                )
            self.store.save_settings(updated)
            self._apply_idle_outputs(updated)
            return self.store.get_settings().to_dict()

    def start(self, patch: dict | None = None) -> dict:
        with self._lock:
            if self._mode != "idle":
                raise ControllerError("A run is already open", 409)
            if patch:
                if set(patch) - {"target_temp_f"}:
                    raise ControllerError("Start only accepts target_temp_f", 400)
                try:
                    updated = merge_settings(self.store.get_settings(), patch)
                except SettingsError as exc:
                    raise ControllerError(str(exc), 400) from exc
                self.store.save_settings(updated)
            settings = self.store.get_settings()
            self._live_temps = self.source.read_temperatures(2)
            self._heaters = self._decide(settings, self._live_temps)
            self.source.set_heaters(self._heaters)
            run = self.store.create_run(settings)
            self._run_id = int(run["id"])
            self._mode = "running"
            self._elapsed = 0.0
            self._since_sample = 0.0
            self._record_sample(settings)
            logger.info("Run %s started at %s°F", self._run_id, settings.target_temp_f)
            return self._status()

    def pause(self) -> dict:
        with self._lock:
            if self._mode != "running":
                raise ControllerError("There is no running run to pause", 409)
            settings = self.store.get_settings()
            self._heaters = [False] * settings.heater_count
            self.source.set_heaters(self._heaters)
            self._live_temps = self.source.read_temperatures(2)
            self._mode = "paused"
            self._record_sample(settings)
            self.store.update_run(self._run_id, status="paused", elapsed_seconds=self._elapsed)
            logger.info("Run %s paused at %ss", self._run_id, round(self._elapsed, 1))
            return self._status()

    def resume(self) -> dict:
        with self._lock:
            if self._mode != "paused":
                raise ControllerError("There is no paused run to resume", 409)
            settings = self.store.get_settings()
            self._live_temps = self.source.read_temperatures(2)
            self._heaters = self._decide(settings, self._live_temps)
            self.source.set_heaters(self._heaters)
            self._mode = "running"
            self._record_sample(settings)
            self.store.update_run(self._run_id, status="running", elapsed_seconds=self._elapsed)
            logger.info("Run %s resumed", self._run_id)
            return self._status()

    def stop(self) -> dict:
        with self._lock:
            if self._mode not in ("running", "paused"):
                raise ControllerError("There is no open run to stop", 409)
            self._finish_run()
            return self._status()

    def _loop(self) -> None:
        last = time.monotonic()
        while not self._stop_loop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            try:
                self.tick(dt)
            except Exception:
                logger.exception("Controller tick failed")
            self._stop_loop.wait(1.0)

    def _tick(self, dt: float) -> None:
        settings = self.store.get_settings()
        stepper = getattr(self.source, "step", None)
        if stepper is not None:
            stepper(dt)
        self._live_temps = self.source.read_temperatures(2)
        if self._mode != "running":
            self._apply_idle_outputs(settings)
            return
        self._elapsed += dt
        self._heaters = self._decide(settings, self._live_temps)
        self.source.set_heaters(self._heaters)
        self._since_sample += dt
        while self._since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._since_sample -= SAMPLE_INTERVAL_SECONDS
            self._record_sample(settings)
            self.store.update_run(self._run_id, elapsed_seconds=self._elapsed)

    def _finish_run(self) -> None:
        settings = self.store.get_settings()
        self._heaters = [False] * settings.heater_count
        self.source.set_heaters(self._heaters)
        self._live_temps = self.source.read_temperatures(2)
        if self._run_id is not None:
            self._record_sample(settings)
            self.store.update_run(
                self._run_id,
                status="stopped",
                elapsed_seconds=self._elapsed,
                ended_at=utc_now(),
            )
            logger.info("Run %s stopped at %ss", self._run_id, round(self._elapsed, 1))
        self._run_id = None
        self._mode = "idle"
        self._elapsed = 0.0
        self._since_sample = 0.0

    def _apply_idle_outputs(self, settings: Settings) -> None:
        if self._mode == "running":
            return
        self._heaters = [False] * settings.heater_count
        self.source.set_heaters(self._heaters)

    def _decide(self, settings: Settings, temps: list[float]) -> list[bool]:
        count = settings.heater_count
        if not temps:
            return [False] * count
        primary = temps[0]
        target = settings.target_temp_f
        if primary >= target:
            return [False] * count
        if primary <= target - DEADBAND_F:
            return [True] * count
        current = list(self._heaters[:count])
        while len(current) < count:
            current.append(False)
        return current

    def _record_sample(self, settings: Settings) -> None:
        if self._run_id is None:
            return
        sensor_count = settings.sensor_count
        heater_count = settings.heater_count
        temps = [round(float(temp), 2) for temp in self._live_temps[:sensor_count]]
        heaters = [bool(on) for on in self._heaters[:heater_count]]
        self.store.add_sample(
            run_id=self._run_id,
            elapsed_seconds=round(self._elapsed, 2),
            temps_f=temps,
            heaters_on=heaters,
            target_temp_f=settings.target_temp_f,
        )

    def _live_overlay(self) -> dict | None:
        if self._run_id is None or self._mode == "idle":
            return None
        return {
            "id": self._run_id,
            "elapsed_seconds": self._elapsed,
            "status": self._mode,
        }

    def _status(self) -> dict:
        settings = self.store.get_settings()
        sensor_count = settings.sensor_count
        heater_count = settings.heater_count
        temps = [round(float(temp), 1) for temp in self._live_temps[:sensor_count]]
        heaters = [bool(on) for on in self._heaters[:heater_count]]
        while len(heaters) < heater_count:
            heaters.append(False)
        return {
            "mode": self._mode,
            "source": getattr(self.source, "name", "unknown"),
            "elapsed_seconds": round(self._elapsed, 1),
            "target_temp_f": settings.target_temp_f,
            "temps_f": temps,
            "sensor_names": settings.sensor_names[:sensor_count],
            "heaters_on": heaters,
            "heater_names": settings.heater_names[:heater_count],
            "heater_count": heater_count,
            "sensor_count": sensor_count,
            "run_id": self._run_id,
            "settings_locked": self._mode != "idle",
        }
