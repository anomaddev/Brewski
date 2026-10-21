import io
import logging
import zipfile

import pytest

from brewski import __version__
from brewski.api import create_app
from brewski.controller import Controller
from brewski.sources.dummy import DummySource
from brewski.store import Store
from brewski.updates import ReleaseInfo


def test_status_starts_idle(client):
    status = client.get("/api/status").get_json()
    assert status["mode"] == "idle"
    assert status["source"] == "dummy"
    assert status["version"] == __version__
    assert status["update_source"] == "main"
    assert status["about_label"] == "Release"
    assert status["about_value"] == __version__
    assert status["heater_count"] == 2
    assert status["sensor_count"] == 1
    assert status["heaters_on"] == [False, False]
    assert status["sensor_names"] == ["Boiler"]
    assert status["run_id"] is None
    assert status["settings_locked"] is False
    assert status["temps_f"][0] == 70
    assert status["cabinet_enabled"] is True


def test_status_names_the_installed_commit_when_beta_is_selected(client, controller, monkeypatch):
    sha = "abc1234" + "d" * 33
    monkeypatch.setattr("brewski.api.installed_revision", lambda: sha)
    controller.update_settings({"update_source": "beta"})
    status = client.get("/api/status").get_json()
    assert status["update_source"] == "beta"
    assert status["about_label"] == "Commit"
    assert status["about_value"] == "abc1234"
    assert status["version"] == __version__


def test_a_run_log_can_be_read_in_pages_and_exported(client, controller):
    logging.getLogger("brewski.controller").warning("before the run")
    run_id = client.post("/api/runs/start").get_json()["run_id"]
    logging.getLogger("brewski.controller").warning("probe glitch")
    logging.getLogger("other").warning("not brewski")
    controller.tick(1)
    controller.tick(1)
    slim = client.get(f"/api/runs/{run_id}?logs=0")
    assert slim.status_code == 200
    assert "logs" not in slim.get_json()

    page = client.get(f"/api/runs/{run_id}/logs").get_json()["logs"]
    assert [item["elapsed_seconds"] for item in page if item["event"] == "sample"] == [0, 1, 2]
    after = page[0]["id"]
    newer = client.get(f"/api/runs/{run_id}/logs?after={after}").get_json()["logs"]
    assert newer[0]["id"] == page[1]["id"]
    assert client.get(f"/api/runs/{run_id}/logs?after=nope").status_code == 400

    exported = client.get(f"/api/runs/{run_id}/log.csv")
    assert exported.status_code == 200
    assert exported.mimetype == "text/csv"
    assert exported.headers["Content-Disposition"] == f'attachment; filename="brewski-run-{run_id}-log.csv"'
    assert "temp_f" in exported.data.decode("utf-8")
    assert client.get("/api/runs/999/log.csv").status_code == 404

    client.post("/api/runs/stop")
    logging.getLogger("brewski.controller").warning("after the run")
    bundle = client.get(f"/api/runs/{run_id}/export.zip")
    assert bundle.status_code == 200
    assert bundle.mimetype == "application/zip"
    assert bundle.headers["Content-Disposition"] == f'attachment; filename="brewski-run-{run_id}.zip"'
    with zipfile.ZipFile(io.BytesIO(bundle.data)) as archive:
        assert archive.namelist() == [
            f"brewski-run-{run_id}-log.csv",
            f"brewski-run-{run_id}-console.log",
        ]
        csv_text = archive.read(f"brewski-run-{run_id}-log.csv").decode("utf-8-sig")
        console = archive.read(f"brewski-run-{run_id}-console.log").decode("utf-8")
    assert "temp_f" in csv_text
    assert "probe glitch" in console
    assert "Run started" in console
    assert "before the run" not in console
    assert "after the run" not in console
    assert "not brewski" not in console
    assert client.get("/api/runs/999/export.zip").status_code == 404


def test_console_output_stays_on_a_run_after_it_closes_and_while_it_cools(controller):
    run_id = controller.start()["run_id"]
    controller.source.temps = [140.0, 110.0]
    assert controller.stop()["mode"] == "cooling"
    logging.getLogger("brewski.controller").warning("still cooling")
    logging.getLogger("brewski.analysis").info(
        "late note",
        extra={"brewski_run_id": run_id, "brewski_elapsed": 4},
    )
    payload, filename = controller.run_export(run_id)
    assert filename == f"brewski-run-{run_id}.zip"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        console = archive.read(f"brewski-run-{run_id}-console.log").decode("utf-8")
    assert "still cooling" in console
    assert "4.00s " in console
    assert "late note" in console


def test_run_start_pause_resume_stop(client, controller):
    started = client.post("/api/runs/start", json={"target_temp_f": 170})
    assert started.status_code == 200
    body = started.get_json()
    assert body["mode"] == "running"
    assert body["target_temp_f"] == 170
    assert body["heaters_on"] == [True, True]
    run_id = body["run_id"]

    assert client.post("/api/runs/start").status_code == 409

    controller.tick(5)
    running = client.get("/api/status").get_json()
    assert running["elapsed_seconds"] == 5
    assert running["temps_f"][0] > 70

    detail = client.get(f"/api/runs/{run_id}").get_json()
    assert detail["target_temp_f"] == 170
    assert detail["heater_names"] == ["Heater 1", "Heater 2"]
    assert len(detail["samples"]) >= 2
    assert detail["samples"][0]["elapsed_seconds"] == 0
    assert detail["samples"][1]["temps_f"][0] > detail["samples"][0]["temps_f"][0]
    assert detail["samples"][1]["heaters_on"][0] is True

    paused = client.post("/api/runs/pause")
    assert paused.status_code == 200
    assert paused.get_json()["mode"] == "paused"
    assert client.post("/api/runs/resume").status_code == 200
    stopped = client.post("/api/runs/stop")
    assert stopped.status_code == 200
    assert stopped.get_json()["mode"] == "idle"
    assert stopped.get_json()["heaters_on"] == [False, False]

    saved = client.get(f"/api/runs/{run_id}").get_json()
    assert saved["status"] == "stopped"
    assert saved["ended_at"]
    assert saved["elapsed_seconds"] >= 5
    history = client.get("/api/runs").get_json()
    assert history["stats"]["total_runs"] == 1
    assert history["runs"][0]["id"] == run_id
    assert history["stats"]["heater_on_seconds"][0] > 0


def test_replace_heater_zeros_one_element_and_is_locked_during_a_run(client, controller):
    client.post("/api/runs/start")
    controller.tick(4)
    locked = client.post("/api/heaters/0/replace")
    assert locked.status_code == 409
    client.post("/api/runs/stop")
    replaced = client.post("/api/heaters/0/replace")
    assert replaced.status_code == 200
    life = replaced.get_json()["heater_life_seconds"]
    assert life[0] == 0
    assert life[1] == pytest.approx(4)
    assert client.post("/api/heaters/9/replace").status_code == 404


def test_pause_does_not_log_paused_time(client, controller):
    run_id = client.post("/api/runs/start").get_json()["run_id"]
    controller.tick(2)
    client.post("/api/runs/pause")
    count = len(client.get(f"/api/runs/{run_id}").get_json()["samples"])
    controller.tick(8)
    again = client.get(f"/api/runs/{run_id}").get_json()
    assert len(again["samples"]) == count
    assert client.get("/api/status").get_json()["elapsed_seconds"] == 2


def test_shape_is_locked_during_a_run_but_target_can_change(client, controller):
    client.post("/api/runs/start")
    current = client.get("/api/settings").get_json()
    heaters = current["heaters"]
    heaters[1]["enabled"] = False
    locked = client.put("/api/settings", json={"heaters": heaters})
    assert locked.status_code == 409
    gpio = current["gpio"]
    gpio["encoder"]["a"] = 25
    locked_gpio = client.put("/api/settings", json={"gpio": gpio})
    assert locked_gpio.status_code == 409

    changed = client.put("/api/settings", json={"target_temp_f": 180})
    assert changed.status_code == 200
    assert changed.get_json()["target_temp_f"] == 180
    changed_source = client.put("/api/settings", json={"update_source": "beta"})
    assert changed_source.status_code == 200
    assert changed_source.get_json()["update_source"] == "beta"

    controller.tick(5)
    run_id = client.get("/api/status").get_json()["run_id"]
    detail = client.get(f"/api/runs/{run_id}").get_json()
    assert detail["target_temp_f"] == 165
    assert detail["samples"][-1]["target_temp_f"] == 180


def test_one_heater_and_one_sensor(client, controller):
    current = client.get("/api/settings").get_json()
    heaters = current["heaters"]
    sensors = current["sensors"]
    heaters[1]["enabled"] = False
    sensors[1]["enabled"] = False
    saved = client.put("/api/settings", json={"heaters": heaters, "sensors": sensors})
    assert saved.status_code == 200
    assert saved.get_json()["heater_names"] == ["Heater 1", "Heater 2"]
    assert saved.get_json()["heaters"][1]["enabled"] is False
    assert saved.get_json()["heaters"][1]["gpio"] == 16

    status = client.get("/api/status").get_json()
    assert status["heater_count"] == 1
    assert status["sensor_count"] == 1
    assert status["heater_names"] == ["Heater 1"]
    assert status["sensor_names"] == ["Boiler"]
    assert status["heaters_on"] == [False]
    assert len(status["temps_f"]) == 1

    client.post("/api/runs/start")
    controller.tick(3)
    running = client.get("/api/status").get_json()
    assert running["heaters_on"] == [True]
    assert len(running["temps_f"]) == 1


def test_reset_clears_runs_and_restores_defaults(client, controller):
    assert client.put("/api/settings", json={"target_temp_f": 180}).status_code == 200
    assert client.post("/api/runs/start").status_code == 200
    controller.tick(3)
    assert client.get("/api/status").get_json()["temps_f"][0] > 70

    reset = client.post("/api/settings/reset")
    assert reset.status_code == 200
    assert reset.get_json()["target_temp_f"] == 165
    assert reset.get_json()["heater_min_on_seconds"] == 1.0
    assert reset.get_json()["heater_min_off_seconds"] == 1.0
    assert "early_cutoff_f" not in reset.get_json()
    assert reset.get_json()["update_source"] == "main"
    assert reset.get_json()["heater_names"] == ["Heater 1", "Heater 2"]
    assert reset.get_json()["sensor_names"] == ["Boiler", "Column"]
    assert [heater["gpio"] for heater in reset.get_json()["heaters"]] == [12, 16]
    assert [sensor["gpio"] for sensor in reset.get_json()["sensors"]] == [17, 8]
    assert reset.get_json()["sensors"][1]["enabled"] is False
    assert reset.get_json()["gpio"]["encoder"] == {"a": 27, "b": 22}
    assert reset.get_json()["gpio"]["buttons"] == {"start": 5, "stop": 6}

    status = client.get("/api/status").get_json()
    assert status["mode"] == "idle"
    assert status["run_id"] is None
    assert status["cooldown_run_id"] is None
    assert status["heaters_on"] == [False, False]
    assert status["target_temp_f"] == 165
    assert status["temps_f"][0] == 70
    assert status["settings_locked"] is False

    history = client.get("/api/runs").get_json()
    assert history["stats"]["total_runs"] == 0
    assert history["runs"] == []
    assert history["stats"]["average_cooldown_seconds"] is None
    assert history["stats"]["heater_life_seconds"] == pytest.approx([3, 3])
    assert status["heater_life_seconds"] == pytest.approx([3, 3])


def test_a_run_is_refused_until_the_still_is_calibrated(tmp_path):
    store = Store(tmp_path / "brewski.db")
    assert store.get_settings().setup_complete is False
    app = create_app(Controller(store, DummySource(noise=False)))
    client = app.test_client()
    refused = client.post("/api/runs/start", json={"target_temp_f": 170})
    assert refused.status_code == 409
    assert "Calibrate" in refused.get_json()["error"]
    assert client.get("/api/status").get_json()["mode"] == "idle"
    started = client.post("/api/runs/start", json={"kind": "calibration", "charge_gallons": 5})
    assert started.status_code == 200
    assert started.get_json()["run_kind"] == "calibration"


def test_setup_flag_resets_with_the_controller(client):
    assert client.get("/api/settings").get_json()["setup_complete"] is True
    reset = client.post("/api/settings/reset")
    assert reset.status_code == 200
    assert reset.get_json()["setup_complete"] is False
    assert reset.get_json()["learned_coast_f"] is None
    assert client.post("/api/runs/start").status_code == 409


def test_calibration_uses_the_still_size_from_setup(client):
    assert client.put("/api/settings", json={"charge_gallons": 7.5}).status_code == 200
    started = client.post("/api/runs/start", json={"kind": "calibration"})
    assert started.status_code == 200
    assert started.get_json()["charge_gallons"] == 7.5
    assert client.post("/api/runs/stop").status_code == 200
    assert client.get("/api/runs").get_json()["runs"][0]["charge_gallons"] == 7.5


def test_calibration_is_left_out_of_run_stats(client):
    started = client.post(
        "/api/runs/start",
        json={"kind": "calibration", "charge_gallons": 3, "target_temp_f": 160},
    )
    assert started.status_code == 200
    assert started.get_json()["run_kind"] == "calibration"
    assert started.get_json()["charge_gallons"] == 3
    assert client.post("/api/runs/stop").status_code == 200
    history = client.get("/api/runs").get_json()
    assert history["stats"]["total_runs"] == 0
    assert history["stats"]["calibration_runs"] == 1
    assert history["runs"][0]["kind"] == "calibration"
    assert history["runs"][0]["charge_gallons"] == 3


def test_bad_calibration_is_rejected(client):
    assert client.post("/api/runs/start", json={"kind": "experiment"}).status_code == 400
    assert client.post("/api/runs/start", json={"charge_gallons": 5}).status_code == 400
    assert client.post(
        "/api/runs/start",
        json={"kind": "calibration", "charge_gallons": 0},
    ).status_code == 400


def test_invalid_settings_and_transitions(client):
    assert client.put("/api/settings", json={"heater_count": 3}).status_code == 400
    assert client.put("/api/settings", json={"target_temp_f": 10}).status_code == 400
    assert client.put("/api/settings", json={"heater_min_on_seconds": 0.4}).status_code == 400
    assert client.put("/api/settings", json={"heater_min_off_seconds": 0.4}).status_code == 400
    assert client.put("/api/settings", json={"early_cutoff_f": 2.5}).status_code == 400
    saved = client.put("/api/settings", json={"heater_min_on_seconds": 0.5})
    assert saved.status_code == 200
    assert saved.get_json()["heater_min_on_seconds"] == 0.5
    assert client.put("/api/settings", json={"sensors": [{"name": "Only"}]}).status_code == 400
    assert client.put(
        "/api/settings", data="{", content_type="application/json"
    ).status_code == 400
    assert client.put("/api/settings", json=["nope"]).status_code == 400
    assert client.post("/api/runs/pause").status_code == 409
    assert client.post("/api/runs/resume").status_code == 409
    assert client.post("/api/runs/stop").status_code == 409
    assert client.get("/api/runs/99").status_code == 404
    assert client.get("/api/missing").status_code == 404


def test_missing_ui_still_serves_the_api(client, monkeypatch):
    monkeypatch.setattr("brewski.api.web_dist_dir", lambda: None)
    page = client.get("/")
    assert page.status_code == 503
    assert b"brewski dev" in page.data
    assert client.get("/api/status").status_code == 200


def test_built_ui_is_served_for_client_routes(tmp_path, monkeypatch, controller):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Brewski</title>")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('brewski')")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    monkeypatch.setattr("brewski.api.web_dist_dir", lambda: dist)

    app = create_app(controller)
    client = app.test_client()
    assert b"Brewski" in client.get("/").data
    assert b"Brewski" in client.get("/history").data
    assert b"Brewski" in client.get("/history/4").data
    assert b"console.log" in client.get("/assets/app.js").data
    assert b"nope" not in client.get("/../secret.txt").data


def test_update_status_reports_a_newer_release(client, monkeypatch):
    monkeypatch.setattr("brewski.updates.apply_supported", lambda: False)
    monkeypatch.setattr(
        "brewski.updates.check_release",
        lambda **_kwargs: ReleaseInfo(
            current="0.2.0",
            latest="v0.3.0",
            available=True,
            reachable=True,
        ),
    )
    body = client.get("/api/update").get_json()
    assert body["available"] is True
    assert body["latest"] == "v0.3.0"
    assert body["current"] == "0.2.0"
    assert body["apply_supported"] is False
    assert body["phase"] == "idle"


def test_apply_is_refused_when_this_machine_cannot_install(client, monkeypatch):
    monkeypatch.setattr("brewski.updates.apply_supported", lambda: False)
    monkeypatch.setattr(
        "brewski.updates.check_release",
        lambda **_kwargs: ReleaseInfo(
            current="0.2.0",
            latest="v0.3.0",
            available=True,
            reachable=True,
        ),
    )
    refused = client.post("/api/update")
    assert refused.status_code == 400
    assert "cannot install" in refused.get_json()["error"]


def test_apply_is_refused_when_nothing_is_newer(client, monkeypatch):
    monkeypatch.setattr("brewski.updates.apply_supported", lambda: True)
    monkeypatch.setattr(
        "brewski.updates.check_release",
        lambda **_kwargs: ReleaseInfo(
            current="0.2.0",
            latest="v0.2.0",
            available=False,
            reachable=True,
        ),
    )
    refused = client.post("/api/update")
    assert refused.status_code == 409


def test_apply_is_refused_while_a_run_is_open(client, monkeypatch):
    monkeypatch.setattr("brewski.updates.apply_supported", lambda: True)
    monkeypatch.setattr(
        "brewski.updates.check_release",
        lambda **_kwargs: ReleaseInfo(
            current="0.2.0",
            latest="v0.3.0",
            available=True,
            reachable=True,
        ),
    )
    assert client.post("/api/runs/start", json={}).status_code == 200
    refused = client.post("/api/update")
    assert refused.status_code == 409
    assert "Stop the run" in refused.get_json()["error"]


def test_reboot_asks_the_pi_to_restart(client, monkeypatch):
    called = []
    monkeypatch.setattr("brewski.controller.request_reboot", lambda: called.append(True))
    response = client.post("/api/reboot")
    assert response.status_code == 202
    assert response.get_json()["rebooting"] is True
    assert called == [True]


def test_reboot_is_refused_while_a_run_is_open(client, monkeypatch):
    called = []
    monkeypatch.setattr("brewski.controller.request_reboot", lambda: called.append(True))
    assert client.post("/api/runs/start", json={}).status_code == 200
    refused = client.post("/api/reboot")
    assert refused.status_code == 409
    assert called == []


def test_reboot_reports_when_the_machine_cannot(client, monkeypatch):
    def refuse():
        raise OSError("nope")

    monkeypatch.setattr("brewski.controller.request_reboot", refuse)
    response = client.post("/api/reboot")
    assert response.status_code == 500
    assert "reboot" in response.get_json()["error"].lower()
