"""Run state machine.

The loop is hardware-agnostic. It reads a :class:`HardwareSource`, decides
whether the heaters should be on, and appends samples while a run is going.
The first enabled probe is the one compared with the target. Disabled heaters
stay off. GPIO pins live on the settings for a later Raspberry Pi source;
this loop does not drive pins itself.
"""

from __future__ import annotations

import logging
import threading
import time

from brewski.sources.base import HardwareSource
from brewski.store import (
    SLOT_COUNT,
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
# A cooldown runs from the first time the control probe reaches the target
# until the temperature stops moving. After the run is stopped, that lower
# stop is a 5 minute stretch where the probe stays inside a 2°F band.
COOLDOWN_BAND_F = 2.0
COOLDOWN_STABLE_SECONDS = 5 * 60
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
        self._cooldown_run_id: int | None = None
        self._cooldown_elapsed = 0.0
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor: int | None = None
        # (cooldown elapsed seconds, control temperature) while heaters are off
        # after the run. The lower stop is read from the last 5 minutes of this.
        self._cooldown_window: list[tuple[float, float]] = []
        self._heaters = [False] * SLOT_COUNT
        # A previous process may have stopped mid-run. Close that record so
        # the next start is a new run, not a continuation of a dead one.
        self.store.close_open_runs()
        open_cooldown = self.store.claim_open_cooldown()
        if open_cooldown is not None:
            self._adopt_cooldown(open_cooldown)
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        self.source.set_heaters(self._heaters)

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
            if self._mode == "cooling":
                self._persist_cooldown()
            self.source.set_heaters([False] * SLOT_COUNT)

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
            return self.store.list_runs(self._live_overlay(), self._cooldown_overlay())

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
            cooldown = self._cooldown_overlay()
            if cooldown is not None and cooldown["id"] == run_id:
                record["cooldown_seconds"] = round(self._cooldown_elapsed, 2)
                record["cooldown_status"] = "open"
            return record

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            current = self.store.get_settings()
            try:
                updated = merge_settings(current, patch)
            except SettingsError as exc:
                raise ControllerError(str(exc), 400) from exc
            if self._mode in ("running", "paused") and shape_changed(current, updated):
                raise ControllerError(
                    "Heater and sensor setup is locked while a run is open",
                    409,
                )
            self.store.save_settings(updated)
            self._apply_idle_outputs(updated)
            return self.store.get_settings().to_dict()

    def start(self, patch: dict | None = None) -> dict:
        with self._lock:
            if self._mode == "cooling":
                self._abandon_cooldown()
            elif self._mode != "idle":
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
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
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
            self._heaters = [False] * SLOT_COUNT
            self.source.set_heaters(self._heaters)
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
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
            self._live_temps = self.source.read_temperatures(SLOT_COUNT)
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
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        if self._mode != "running":
            self._apply_idle_outputs(settings)
            if self._mode == "cooling":
                self._tick_cooldown(dt, settings)
            elif self._mode == "paused":
                self._advance_cooldown_clock(dt, settings)
            return
        self._elapsed += dt
        self._heaters = self._decide(settings, self._live_temps)
        self.source.set_heaters(self._heaters)
        self._advance_cooldown_clock(dt, settings)
        self._since_sample += dt
        while self._since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._since_sample -= SAMPLE_INTERVAL_SECONDS
            self._record_sample(settings)
            self.store.update_run(self._run_id, elapsed_seconds=self._elapsed)

    def _finish_run(self) -> None:
        settings = self.store.get_settings()
        self._heaters = [False] * SLOT_COUNT
        self.source.set_heaters(self._heaters)
        self._live_temps = self.source.read_temperatures(SLOT_COUNT)
        if self._run_id is not None:
            self._record_sample(settings)
            self.store.update_run(
                self._run_id,
                status="stopped",
                elapsed_seconds=self._elapsed,
                ended_at=utc_now(),
            )
            logger.info("Run %s stopped at %ss", self._run_id, round(self._elapsed, 1))
            self._begin_cooldown(self._run_id)
        self._run_id = None
        self._elapsed = 0.0
        self._since_sample = 0.0
        if self._mode != "cooling":
            self._mode = "idle"

    def _begin_cooldown(self, run_id: int) -> None:
        settings = self.store.get_settings()
        if self._cooldown_run_id is None:
            self._advance_cooldown_clock(0, settings)
        if self._cooldown_run_id is None:
            return
        self._mode = "cooling"
        self._cooldown_window = []
        current = self._cooldown_temp()
        if current is not None:
            self._push_cooldown_temp(current)
        self._record_cooldown_sample(settings)
        self._persist_cooldown()
        logger.info("Run %s cooling; cooldown clock at %ss", run_id, round(self._cooldown_elapsed, 1))

    def _advance_cooldown_clock(self, dt: float, settings: Settings) -> None:
        """Count time after the control probe first reaches the target.

        The lower stop is not decided here. Holding at the target, or pausing,
        still belongs to the cooldown, but the temperature can only "stop"
        once the run is over and the heaters are off.
        """

        if self._mode == "cooling" or self._run_id is None:
            return
        if self._cooldown_run_id is None:
            found = self._current_control(settings)
            if found is None:
                return
            index, temp = found
            if temp < settings.target_temp_f:
                return
            self._cooldown_run_id = self._run_id
            self._cooldown_sensor = index
            self._cooldown_elapsed = 0.0
            self._cooldown_since_sample = 0.0
            self.store.update_run(
                self._run_id,
                cooldown_status="open",
                cooldown_seconds=0,
                cooldown_baseline_f=settings.target_temp_f,
                cooldown_sensor_index=index,
            )
            logger.info(
                "Run %s reached %s°F; cooldown timing started",
                self._run_id,
                settings.target_temp_f,
            )
        self._cooldown_elapsed += dt
        self._cooldown_since_sample += dt
        while self._cooldown_since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._cooldown_since_sample -= SAMPLE_INTERVAL_SECONDS
            self._persist_cooldown()

    def _adopt_cooldown(self, run: dict) -> None:
        sensor = run.get("cooldown_sensor_index")
        if sensor is None:
            self.store.update_run(int(run["id"]), cooldown_status="abandoned")
            return
        self._mode = "cooling"
        self._cooldown_run_id = int(run["id"])
        self._cooldown_elapsed = float(run.get("cooldown_seconds") or 0)
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor = int(sensor)
        self._cooldown_window = []

    def _tick_cooldown(self, dt: float, settings: Settings) -> None:
        if self._cooldown_run_id is None or self._cooldown_sensor is None:
            self._clear_cooldown()
            return
        self._cooldown_elapsed += dt
        self._cooldown_since_sample += dt
        current = self._cooldown_temp()
        if current is not None:
            self._push_cooldown_temp(current)
            if self._cooldown_settled():
                self._complete_cooldown(settings)
                return
        while self._cooldown_since_sample >= SAMPLE_INTERVAL_SECONDS:
            self._cooldown_since_sample -= SAMPLE_INTERVAL_SECONDS
            self._record_cooldown_sample(settings)
        self._persist_cooldown()

    def _push_cooldown_temp(self, temp: float) -> None:
        self._cooldown_window.append((self._cooldown_elapsed, temp))
        cutoff = self._cooldown_elapsed - COOLDOWN_STABLE_SECONDS
        while len(self._cooldown_window) >= 2 and self._cooldown_window[1][0] <= cutoff:
            self._cooldown_window.pop(0)

    def _cooldown_settled(self) -> bool:
        window = self._cooldown_window
        if len(window) < 2:
            return False
        if window[-1][0] - window[0][0] < COOLDOWN_STABLE_SECONDS:
            return False
        temps = [temp for _, temp in window]
        return max(temps) - min(temps) <= COOLDOWN_BAND_F

    def _complete_cooldown(self, settings: Settings) -> None:
        run_id = self._cooldown_run_id
        elapsed = self._cooldown_elapsed
        self._record_cooldown_sample(settings)
        self.store.update_run(
            run_id,
            cooldown_status="complete",
            cooldown_seconds=round(elapsed, 2),
            cooldown_ended_at=utc_now(),
        )
        logger.info("Run %s cooled in %ss", run_id, round(elapsed, 1))
        self._clear_cooldown()

    def _abandon_cooldown(self) -> None:
        if self._cooldown_run_id is None:
            self._clear_cooldown()
            return
        run_id = self._cooldown_run_id
        self.store.update_run(
            run_id,
            cooldown_status="abandoned",
            cooldown_seconds=round(self._cooldown_elapsed, 2),
        )
        logger.info("Run %s cooldown abandoned at %ss", run_id, round(self._cooldown_elapsed, 1))
        self._clear_cooldown()

    def _persist_cooldown(self) -> None:
        if self._cooldown_run_id is None:
            return
        self.store.update_run(
            self._cooldown_run_id,
            cooldown_seconds=round(self._cooldown_elapsed, 2),
        )

    def _record_cooldown_sample(self, settings: Settings) -> None:
        if self._cooldown_run_id is None:
            return
        temps = [round(float(temp), 2) for temp in self._live_temps[:SLOT_COUNT]]
        self.store.add_sample(
            run_id=self._cooldown_run_id,
            elapsed_seconds=round(self._cooldown_elapsed, 2),
            temps_f=temps,
            heaters_on=[False] * SLOT_COUNT,
            target_temp_f=settings.target_temp_f,
            phase="cooldown",
        )

    def _clear_cooldown(self) -> None:
        self._cooldown_run_id = None
        self._cooldown_elapsed = 0.0
        self._cooldown_since_sample = 0.0
        self._cooldown_sensor = None
        self._cooldown_window = []
        if self._mode == "cooling":
            self._mode = "idle"

    def _cooldown_temp(self) -> float | None:
        index = self._cooldown_sensor
        if index is None or index >= len(self._live_temps):
            return None
        return float(self._live_temps[index])

    def _cooldown_overlay(self) -> dict | None:
        if self._mode != "cooling" or self._cooldown_run_id is None:
            return None
        return {
            "id": self._cooldown_run_id,
            "cooldown_seconds": self._cooldown_elapsed,
            "cooldown_status": "open",
        }

    def _apply_idle_outputs(self, settings: Settings) -> None:
        if self._mode == "running":
            return
        self._heaters = [False] * SLOT_COUNT
        self.source.set_heaters(self._heaters)

    def _decide(self, settings: Settings, temps: list[float]) -> list[bool]:
        """Physical heater mask. Index 0 is heater 1. Disabled slots stay off."""

        off = [False] * SLOT_COUNT
        control_indexes = settings.enabled_sensor_indexes()
        if not control_indexes or control_indexes[0] >= len(temps):
            return off
        primary = temps[control_indexes[0]]
        target = settings.target_temp_f
        if primary >= target:
            return off
        if primary <= target - DEADBAND_F:
            return [channel.enabled for channel in settings.heaters]
        current = list(self._heaters[:SLOT_COUNT])
        while len(current) < SLOT_COUNT:
            current.append(False)
        return [
            bool(current[index]) and settings.heaters[index].enabled for index in range(SLOT_COUNT)
        ]

    def _record_sample(self, settings: Settings) -> None:
        if self._run_id is None:
            return
        # Samples stay aligned to the physical slots. A disabled heater is off.
        temps = [round(float(temp), 2) for temp in self._live_temps[:SLOT_COUNT]]
        heaters = [False] * SLOT_COUNT
        for index, on in enumerate(self._heaters[:SLOT_COUNT]):
            heaters[index] = bool(on) and settings.heaters[index].enabled
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
        sensor_indexes = settings.enabled_sensor_indexes()
        heater_indexes = settings.enabled_heater_indexes()
        temps = [
            round(float(self._live_temps[index]), 1)
            for index in sensor_indexes
            if index < len(self._live_temps)
        ]
        heaters = [
            bool(self._heaters[index])
            for index in heater_indexes
            if index < len(self._heaters)
        ]
        return {
            "mode": self._mode,
            "source": getattr(self.source, "name", "unknown"),
            "elapsed_seconds": round(
                self._cooldown_elapsed if self._mode == "cooling" else self._elapsed,
                1,
            ),
            "target_temp_f": settings.target_temp_f,
            "temps_f": temps,
            "sensor_names": [settings.sensors[index].name for index in sensor_indexes],
            "heaters_on": heaters,
            "heater_names": [settings.heaters[index].name for index in heater_indexes],
            "heater_count": len(heater_indexes),
            "sensor_count": len(sensor_indexes),
            "run_id": self._run_id,
            "cooldown_run_id": self._cooldown_run_id if self._mode == "cooling" else None,
            "settings_locked": self._mode in ("running", "paused"),
        }


    def _current_control(self, settings: Settings) -> tuple[int, float] | None:
        indexes = settings.enabled_sensor_indexes()
        if not indexes or indexes[0] >= len(self._live_temps):
            return None
        index = indexes[0]
        return index, float(self._live_temps[index])
