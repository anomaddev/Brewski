from brewski.api import create_app


def test_status_starts_idle(client):
    status = client.get("/api/status").get_json()
    assert status["mode"] == "idle"
    assert status["source"] == "dummy"
    assert status["heater_count"] == 2
    assert status["sensor_count"] == 2
    assert status["heaters_on"] == [False, False]
    assert status["sensor_names"] == ["Boiler", "Column"]
    assert status["run_id"] is None
    assert status["settings_locked"] is False
    assert status["temps_f"][0] == 70


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

    changed = client.put("/api/settings", json={"target_temp_f": 180})
    assert changed.status_code == 200
    assert changed.get_json()["target_temp_f"] == 180

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
    assert saved.get_json()["heaters"][1]["gpio"] == 27

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


def test_invalid_settings_and_transitions(client):
    assert client.put("/api/settings", json={"heater_count": 3}).status_code == 400
    assert client.put("/api/settings", json={"target_temp_f": 10}).status_code == 400
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
