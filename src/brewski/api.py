"""HTTP API and, when the dashboard has been built, the static UI."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from brewski.controller import Controller, ControllerError
from brewski.paths import web_dist_dir


def create_app(controller: Controller) -> Flask:
    app = Flask(__name__)

    @app.errorhandler(ControllerError)
    def handle_controller_error(error: ControllerError):
        return jsonify({"error": error.message}), error.status

    @app.errorhandler(404)
    def handle_not_found(_error):
        if request.path.startswith("/api"):
            return jsonify({"error": "Not found"}), 404
        return ("Not found", 404)

    @app.get("/api/status")
    def status():
        return jsonify(controller.status())

    @app.get("/api/settings")
    def get_settings():
        return jsonify(controller.store.get_settings().to_dict())

    @app.put("/api/settings")
    def put_settings():
        return jsonify(controller.update_settings(_json_object()))

    @app.post("/api/settings/reset")
    def reset_settings():
        return jsonify(controller.reset())

    @app.get("/api/runs")
    def runs():
        return jsonify(controller.runs())

    @app.get("/api/runs/<int:run_id>")
    def run(run_id: int):
        return jsonify(controller.run(run_id))

    @app.post("/api/runs/start")
    def start_run():
        return jsonify(controller.start(_json_object()))

    @app.post("/api/runs/pause")
    def pause_run():
        return jsonify(controller.pause())

    @app.post("/api/runs/resume")
    def resume_run():
        return jsonify(controller.resume())

    @app.post("/api/runs/stop")
    def stop_run():
        return jsonify(controller.stop())

    @app.post("/api/heaters/<int:slot>/replace")
    def replace_heater(slot: int):
        return jsonify(controller.replace_heater(slot))

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            abort(404)
        if path == "favicon.ico":
            return ("", 204)
        dist = web_dist_dir()
        if dist is None:
            return (
                "The Brewski UI is not built. From the repo run `npm install` and "
                "`npm run build` in web/, or start a dev session with `brewski dev`.",
                503,
                {"Content-Type": "text/plain; charset=utf-8"},
            )
        if path and _is_file_inside(dist, path):
            return send_from_directory(dist, path)
        return send_from_directory(dist, "index.html")

    return app


def _json_object() -> dict:
    if not request.data:
        return {}
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ControllerError("Request body must be a JSON object", 400)
    return data


def _is_file_inside(dist: Path, path: str) -> bool:
    root = dist.resolve()
    candidate = (dist / path).resolve()
    return candidate.is_file() and (candidate == root or root in candidate.parents)
