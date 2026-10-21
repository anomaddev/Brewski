"""Hold programs, the emergency cap, and the LCD editor that saves them."""

import pytest

from brewski.controller import ControllerError
from brewski.program import Program, program_end
from brewski.store import SettingsError, merge_settings


class _Panel:
    def __init__(self, events):
        self.events = events

    def take(self):
        events = self.events
        self.events = (0, 0, 0, 0)
        return events

    def set_leds(self, *_args):
        return None

    def close(self):
        return None


def _press(controller, events):
    controller._panel = _Panel(events)
    controller._apply_panel()


def test_program_end_rules():
    timed = Program(continuous=False, duration_seconds=60)
    assert program_end(timed, elapsed_seconds=120, hold_seconds=None, max_program_hours=24) is None
    assert program_end(timed, elapsed_seconds=30, hold_seconds=59, max_program_hours=24) is None
    assert program_end(timed, elapsed_seconds=90, hold_seconds=60, max_program_hours=24) == "duration"
    assert (
        program_end(timed, elapsed_seconds=24 * 3600, hold_seconds=60, max_program_hours=24) == "max"
    )
    continuous = Program(continuous=True, duration_seconds=60)
    assert program_end(continuous, elapsed_seconds=3600, hold_seconds=3600, max_program_hours=24) is None
    assert program_end(continuous, elapsed_seconds=24 * 3600, hold_seconds=None, max_program_hours=24) == "max"


def test_program_settings_reject_values_outside_the_range(store):
    current = store.get_settings()
    assert current.max_program_hours == 24
    assert current.program_mode == "hold"
    assert current.program_continuous is True
    assert current.program_duration_seconds == 3600
    assert merge_settings(current, {"max_program_hours": 6}).max_program_hours == 6
    assert merge_settings(current, {"max_program_hours": 72}).max_program_hours == 72
    assert merge_settings(current, {"program_duration_seconds": 1}).program_duration_seconds == 1
    assert (
        merge_settings(current, {"program_duration_seconds": 72 * 3600}).program_duration_seconds
        == 72 * 3600
    )
    with pytest.raises(SettingsError):
        merge_settings(current, {"max_program_hours": 5})
    with pytest.raises(SettingsError):
        merge_settings(current, {"max_program_hours": 73})
    with pytest.raises(SettingsError):
        merge_settings(current, {"max_program_hours": 24.5})
    with pytest.raises(SettingsError):
        merge_settings(current, {"program_mode": "boil"})
    with pytest.raises(SettingsError):
        merge_settings(current, {"program_continuous": "yes"})
    with pytest.raises(SettingsError):
        merge_settings(current, {"program_duration_seconds": 0})
    saved = merge_settings(
        current,
        {"program_continuous": False, "program_duration_seconds": 90, "max_program_hours": 12},
    )
    store.save_settings(saved)
    loaded = store.get_settings()
    assert loaded.program_continuous is False
    assert loaded.program_duration_seconds == 90
    assert loaded.max_program_hours == 12


def test_start_saves_the_hold_program_onto_the_run(controller, store):
    started = controller.start(
        {"program_continuous": False, "program_duration_seconds": 90, "target_temp_f": 170}
    )
    assert started["program"]["mode"] == "hold"
    assert started["program"]["continuous"] is False
    assert started["program"]["duration_seconds"] == 90
    assert started["program"]["state"] == "heating"
    assert started["max_program_hours"] == 24
    assert started["max_program_remaining_seconds"] == pytest.approx(24 * 3600, abs=1)
    settings = store.get_settings()
    assert settings.program_continuous is False
    assert settings.program_duration_seconds == 90
    assert settings.target_temp_f == 170
    run = store.get_run(started["run_id"])
    assert run["program_mode"] == "hold"
    assert run["program_continuous"] is False
    assert run["program_duration_seconds"] == 90
    assert run["program_end"] is None


def test_a_program_is_refused_on_a_test(controller):
    with pytest.raises(ControllerError, match="only for a run"):
        controller.start({"kind": "test", "test": "initial_heating", "program_continuous": False})


def test_a_timed_hold_completes_when_the_hold_clock_is_up(controller, store):
    started = controller.start({"program_continuous": False, "program_duration_seconds": 60})
    run_id = started["run_id"]
    controller._hold_started_at = 0.0
    controller._elapsed = 59
    controller.tick(1)
    assert controller.status()["mode"] == "idle"
    saved = store.get_run(run_id)
    assert saved["status"] == "complete"
    assert saved["program_end"] == "duration"


def test_a_continuous_hold_ignores_the_hold_timer(controller):
    controller.start()
    controller._hold_started_at = 0.0
    controller._elapsed = 23 * 3600
    controller.tick(1)
    status = controller.status()
    assert status["mode"] == "running"
    assert status["program"]["continuous"] is True
    assert status["program"]["state"] == "holding"


def test_the_cap_completes_a_run_before_it_has_held(controller, store):
    started = controller.start()
    run_id = started["run_id"]
    controller._elapsed = 24 * 3600 - 1
    controller.tick(1)
    assert controller._hold_started_at is None
    saved = store.get_run(run_id)
    assert saved["status"] == "complete"
    assert saved["program_end"] == "max"


def test_pause_does_not_advance_the_hold_clock_and_a_lower_cap_completes_it(controller, store):
    started = controller.start({"program_continuous": False, "program_duration_seconds": 120})
    run_id = started["run_id"]
    controller._hold_started_at = 0.0
    controller._elapsed = 30
    controller.pause()
    controller.tick(50)
    status = controller.status()
    assert status["mode"] == "paused"
    assert status["elapsed_seconds"] == 30
    assert status["program"]["hold_seconds"] == 30
    assert status["program"]["state"] == "paused"
    controller.update_settings({"max_program_hours": 6})
    controller._elapsed = 6 * 3600
    controller.tick(1)
    saved = store.get_run(run_id)
    assert saved["status"] == "complete"
    assert saved["program_end"] == "max"


def test_the_cap_completes_a_calibration(controller, store):
    started = controller.start({"kind": "calibration"})
    run_id = started["run_id"]
    controller.update_settings({"max_program_hours": 6})
    controller._elapsed = 6 * 3600 - 1
    controller.tick(1)
    saved = store.get_run(run_id)
    assert saved["status"] == "complete"
    assert saved["program_end"] == "max"
    assert saved["program_mode"] is None


def test_the_lcd_program_menu_saves_a_timed_hold(controller, store):
    _press(controller, (0, 0, 1, 0))
    _press(controller, (1, 0, 0, 0))
    assert controller._display.lines[1] == "> Hold"
    _press(controller, (1, 0, 0, 0))
    assert controller._display.lines[1].startswith("> Target")
    _press(controller, (0, 0, 0, 4))
    assert "Continuous" in controller._display.lines[3]
    _press(controller, (1, 0, 0, 0))
    assert store.get_settings().program_continuous is False
    _press(controller, (0, 0, 0, -3))
    assert controller._display.lines[1].startswith("> Hours")
    _press(controller, (1, 0, 0, 0))
    assert controller._display.lines[0].startswith("Editing")
    _press(controller, (0, 0, 0, 1))
    _press(controller, (1, 0, 0, 0))
    settings = store.get_settings()
    assert settings.program_continuous is False
    assert settings.program_duration_seconds == 2 * 3600
    _press(controller, (1, 0, 0, 0))
    assert controller._menu.screen == "hold"
    _press(controller, (0, 0, 0, 0))
    controller._menu.hold_index = 1
    controller._menu.editing = True
    controller._menu.draft = 0
    controller._menu.hours = 0
    controller._menu.minutes = 0
    controller._menu.seconds = 0
    controller._menu._commit_draft()
    controller._menu.editing = False
    controller._save_menu_program()
    controller._refresh_display()
    assert store.get_settings().program_duration_seconds == 2 * 3600
    assert controller._display.lines[0].startswith("Need 1 second")
