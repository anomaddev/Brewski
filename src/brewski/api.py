"""HTTP API and, when the dashboard has been built, the static UI."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from brewski import __version__
from brewski.controller import Controller, ControllerError
from brewski.paths import web_dist_dir
from brewski.updates import about_identity, installed_revision


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
        payload = controller.status()
        label, value = about_identity(
            str(payload.get("update_source") or "main"),
            __version__,
            installed_revision(),
        )
        payload["version"] = __version__
        payload["about_label"] = label
        payload["about_value"] = value
        return jsonify(payload)

    @app.get("/api/settings")
    def get_settings():
        return jsonify(controller.store.get_settings().to_dict())

    @app.put("/api/settings")
    def put_settings():
        return jsonify(controller.update_settings(_json_object()))

    @app.post("/api/settings/reset")
    def reset_settings():
        return jsonify(controller.reset())

    @app.get("/api/update")
    def get_update():
        fresh = request.args.get("fresh", "").lower() in {"1", "true", "yes"}
        return jsonify(controller.update_status(fresh=fresh))

    @app.post("/api/update")
    def post_update():
        return jsonify(controller.start_update()), 202

    @app.post("/api/reboot")
    def post_reboot():
        return jsonify(controller.reboot()), 202

    @app.get("/api/timeline")
    def timeline():
        return jsonify(
            controller.timeline(_timeline_ms("from"), _timeline_ms("to"), _timeline_max())
        )

    @app.get("/api/runs")
    def runs():
        return jsonify(controller.runs())

    @app.get("/api/runs/<int:run_id>")
    def run(run_id: int):
        include_logs = request.args.get("logs", "1").lower() not in {"0", "false", "no"}
        return jsonify(controller.run(run_id, include_logs=include_logs))

    @app.get("/api/runs/<int:run_id>/logs")
    def run_logs(run_id: int):
        return jsonify(controller.run_logs(run_id, _logs_after()))

    @app.get("/api/runs/<int:run_id>/log.csv")
    def run_log_csv(run_id: int):
        text, filename = controller.control_log_export(run_id)
        return Response(
            text,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/calibration/clean")
    def clean_calibration():
        return jsonify(controller.clear_calibration())

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

    @app.post("/api/tests/end")
    def end_test():
        return jsonify(controller.end_test())

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


def _timeline_ms(name: str) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        raise ControllerError(f"Missing {name}", 400)
    try:
        return int(raw)
    except ValueError as exc:
        raise ControllerError(f"{name} must be a timestamp in milliseconds", 400) from exc


def _timeline_max() -> int:
    raw = request.args.get("max")
    if raw is None or raw == "":
        return 1600
    try:
        return int(raw)
    except ValueError as exc:
        raise ControllerError("max must be an integer", 400) from exc


def _logs_after() -> int:
    raw = request.args.get("after", "0")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ControllerError("after must be a log id", 400) from exc
    if value < 0:
        raise ControllerError("after must be a log id", 400)
    return value


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
