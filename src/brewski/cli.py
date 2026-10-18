"""Command line entry points for the installed controller and local UI development."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from brewski import __version__
from brewski.api import create_app
from brewski.controller import Controller
from brewski.paths import data_directory, host_and_port, web_project_dir
from brewski.sources.dummy import DummySource
from brewski.store import Store

logger = logging.getLogger("brewski")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="brewski",
        description="Web controller for a small still. With no command, start the server.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["dev", "boot-update", "apply-update"],
        help="dev starts the API and the Vite dashboard together. "
        "boot-update asks on the LCD before replacing a release. "
        "apply-update installs the release the user already accepted.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.command == "dev":
        run_dev()
    elif args.command == "boot-update":
        from brewski.updates import run_boot_update

        raise SystemExit(run_boot_update())
    elif args.command == "apply-update":
        from brewski.updates import run_apply_update

        raise SystemExit(run_apply_update())
    else:
        run_serve()


def run_serve() -> None:
    controller, app = _build_runtime(dev=False)
    host, port = host_and_port(dev=False)
    _install_interrupt()
    controller.start_loop()
    logger.info("Brewski listening on http://%s:%s", host, port)
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        controller.shutdown()


def run_dev() -> None:
    web_dir = web_project_dir()
    if web_dir is None:
        raise SystemExit("Could not find web/. Run `brewski dev` from a Brewski checkout.")
    if not (web_dir / "node_modules" / "vite" / "package.json").is_file():
        raise SystemExit("Dashboard dependencies are missing. Run `npm install` in web/.")

    host, port = host_and_port(dev=True)
    dev_port = int(os.environ.get("BREWSKI_DEV_PORT", "5173"))
    controller, app = _build_runtime(dev=True)
    _install_interrupt()
    controller.start_loop()

    server_error: dict[str, BaseException] = {}

    def serve() -> None:
        try:
            app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        except Exception as exc:
            server_error["error"] = exc

    thread = threading.Thread(target=serve, name="brewski-http", daemon=True)
    thread.start()
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if not _wait_for_port(connect_host, port, server_error):
        detail = server_error.get("error")
        controller.shutdown()
        raise SystemExit(f"API did not start on {host}:{port}{': ' + str(detail) if detail else ''}")

    env = os.environ.copy()
    env["BREWSKI_HOST"] = host
    env["BREWSKI_PORT"] = str(port)
    logger.info("Brewski API  http://%s:%s", connect_host, port)
    logger.info("Brewski UI   http://127.0.0.1:%s", dev_port)
    process = subprocess.Popen(
        [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(dev_port),
            "--strictPort",
        ],
        cwd=web_dir,
        env=env,
        start_new_session=True,
    )
    try:
        return_code = process.wait()
    except KeyboardInterrupt:
        return_code = 0
    finally:
        controller.shutdown()
        _terminate_process_group(process)
    if return_code:
        raise SystemExit(return_code)


def _build_runtime(dev: bool) -> tuple[Controller, object]:
    store = Store(data_directory(dev))
    if dev:
        source = DummySource()
    else:
        from brewski.sources.pi import PiSource

        source = PiSource(store)
    controller = Controller(store, source)
    return controller, create_app(controller)


def _install_interrupt() -> None:
    def handle(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def _wait_for_port(host: str, port: int, server_error: dict, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_error:
            return False
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _terminate_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


if __name__ == "__main__":
    main()
    sys.exit(0)
