"""GitHub release checks and the prompted installer.

A fresh boot does not contact GitHub. The I2C pins are whatever setup saved,
and the panel prompt cannot open the bus before that.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from brewski import __version__
from brewski.lcd import (
    Display,
    open_buttons,
    open_display,
    open_prompt_leds,
    progress_lines,
    prompt_lines,
)
from brewski.paths import data_directory
from brewski.store import load_settings

logger = logging.getLogger(__name__)

GITHUB_REPO = "anomaddev/Brewski"
REVISION_PATH = Path("/opt/brewski/revision")
INSTALL_SCRIPT = Path("/opt/brewski/install.sh")
APPLY_UNIT = "brewski-apply-update.service"
PROMPT_SECONDS = 60
_CACHE_SECONDS = 20.0
_cache_lock = threading.Lock()
_cache: tuple[float, ReleaseInfo] | None = None


@dataclass(frozen=True)
class ReleaseInfo:
    current: str
    latest: str | None
    available: bool
    reachable: bool
    revision: str | None = None


def normalize_version(tag: str) -> tuple[int, int, int] | None:
    text = tag.strip()
    if text[:1] in {"v", "V"}:
        text = text[1:]
    parts = text.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError:
        return None
    return (major, minor, patch)


def is_newer(latest: str, current: str) -> bool:
    new = normalize_version(latest)
    old = normalize_version(current)
    if new is None or old is None:
        return False
    return new > old


def check_release(
    *,
    opener=None,
    revision_path: Path | None = None,
    timeout: float = 30,
) -> ReleaseInfo:
    """Compare the running version with the latest GitHub release.

    An explicit opener or revision path skips the short cache so tests stay isolated.
    """

    global _cache
    if opener is not None or revision_path is not None:
        return _check_release(opener=opener, revision_path=revision_path, timeout=timeout)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache
    if cached is not None and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    info = _check_release(opener=None, revision_path=None, timeout=timeout)
    with _cache_lock:
        _cache = (time.monotonic(), info)
    return info


def apply_supported() -> bool:
    """True on a Pi install that can start the root updater."""

    return INSTALL_SCRIPT.is_file() and shutil.which("systemctl") is not None


def status_path(directory: Path | None = None) -> Path:
    folder = data_directory(False) if directory is None else directory
    return folder / "update-status.json"


def read_status(directory: Path | None = None) -> dict:
    path = status_path(directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"phase": "idle", "current": "", "latest": "", "error": None}
    if not isinstance(data, dict):
        return {"phase": "idle", "current": "", "latest": "", "error": None}
    return {
        "phase": data.get("phase") or "idle",
        "current": data.get("current") or "",
        "latest": data.get("latest") or "",
        "error": data.get("error"),
    }


def write_status(
    *,
    phase: str,
    current: str,
    latest: str,
    error: str | None,
    directory: Path | None = None,
) -> None:
    path = status_path(directory)
    payload = {"phase": phase, "current": current, "latest": latest, "error": error}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        logger.warning("Could not write %s", path)


def report(directory: Path | None = None) -> dict:
    release = check_release()
    status = read_status(directory)
    return {
        "current": release.current,
        "latest": release.latest,
        "available": release.available,
        "reachable": release.reachable,
        "apply_supported": apply_supported(),
        "phase": status["phase"],
        "error": status["error"],
    }


def start_apply_service() -> None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise OSError("systemctl was not found")
    subprocess.run(
        ["sudo", "-n", systemctl, "start", "--no-block", APPLY_UNIT],
        check=True,
    )


def run_boot_update() -> int:
    """Ask on the LCD when a newer release exists, then let the app start.

    Always returns 0. A declined, skipped, or failed update keeps the
    installed version, and the service that runs after this oneshot starts it.
    """

    directory = data_directory(False)
    settings = load_settings(directory)
    if settings is None or not settings.setup_complete:
        logger.info("Brewski setup has not saved the I2C pins. Skipping the update check.")
        return 0
    release = check_release()
    if not release.available:
        if release.reachable:
            logger.info("Brewski %s is current.", release.current)
        else:
            logger.info("GitHub could not be reached. Starting the installed version.")
        return 0
    if settings.gpio is None or not settings.gpio.lcd_enabled():
        logger.info("An update is available and the LCD is turned off. Keeping this version.")
        return 0
    display = open_display(settings)
    if not display.hardware:
        logger.warning("An update is available and the LCD could not be opened. Keeping this version.")
        display.close()
        return 0
    latest = release.latest or ""
    display.write(prompt_lines(release.current, latest, PROMPT_SECONDS))
    buttons = open_buttons(settings)
    if buttons is None:
        logger.warning(
            "An update is available and the buttons could not be read. "
            "Waiting %ss, then keeping this version.",
            PROMPT_SECONDS,
        )
    leds = open_prompt_leds(settings)
    logger.info("Update %s is available. Waiting %ss for Start or Stop.", latest, PROMPT_SECONDS)
    try:
        choice = wait_for_choice(
            display,
            buttons.pressed if buttons is not None else (lambda: None),
            current=release.current,
            latest=latest,
            seconds=PROMPT_SECONDS,
        )
    finally:
        if leds is not None:
            leds.close()
        if buttons is not None:
            buttons.close()
    if choice != "start":
        display.write(progress_lines("kept", release.current, release.latest or ""))
        time.sleep(1)
        display.close()
        return 0
    run_installer(display, release, directory)
    time.sleep(2)
    display.close()
    return 0


def run_apply_update() -> int:
    """Install the release the user already accepted, then restart Brewski."""

    from brewski.lcd import NullDisplay

    directory = data_directory(False)
    settings = load_settings(directory)
    release = check_release()
    display = open_display(settings) if settings is not None else NullDisplay()
    try:
        ok = run_installer(display, release, directory)
        if ok:
            time.sleep(2)
    finally:
        display.close()
    if ok:
        restart_service()
        return 0
    return 1


def wait_for_choice(
    display: Display,
    pressed,
    *,
    current: str,
    latest: str,
    seconds: int = PROMPT_SECONDS,
    now=None,
    sleep=None,
) -> str:
    """Block until Start, Stop, or the timeout. The timeout keeps the current version."""

    if now is None:
        now = time.monotonic
    if sleep is None:
        sleep = time.sleep
    deadline = now() + seconds
    shown: list[str] | None = None
    while True:
        remaining = int(deadline - now() + 0.999)
        if remaining <= 0:
            return "stop"
        lines = prompt_lines(current, latest, remaining)
        if lines != shown:
            display.write(lines)
            shown = lines
        choice = pressed()
        if choice in {"start", "stop"}:
            return choice
        sleep(0.05)


def run_installer(display: Display, release: ReleaseInfo, directory: Path) -> bool:
    latest = release.latest or ""
    write_status(
        phase="downloading",
        current=release.current,
        latest=latest,
        error=None,
        directory=directory,
    )
    display.write(progress_lines("downloading", release.current, latest))
    stop = threading.Event()

    def mirror() -> None:
        seen = "downloading"
        while not stop.is_set():
            phase = read_status(directory).get("phase") or "downloading"
            if phase != seen and phase != "idle":
                display.write(progress_lines(str(phase), release.current, latest))
                seen = str(phase)
            stop.wait(0.25)

    thread = threading.Thread(target=mirror, name="brewski-update-lcd", daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env["BREWSKI_STATUS_FILE"] = str(status_path(directory))
        try:
            result = subprocess.run(
                [str(INSTALL_SCRIPT), "--apply"],
                check=False,
                env=env,
            )
        except OSError as exc:
            logger.error("Could not run the installer: %s", exc)
            result = subprocess.CompletedProcess([str(INSTALL_SCRIPT)], 1)
    finally:
        stop.set()
        thread.join(timeout=1)
    if result.returncode != 0:
        write_status(
            phase="failed",
            current=release.current,
            latest=latest,
            error="The installer did not finish.",
            directory=directory,
        )
        display.write(progress_lines("failed", release.current, latest))
        logger.error("Brewski update failed. Keeping %s.", release.current)
        return False
    write_status(
        phase="complete",
        current=release.current,
        latest=latest,
        error=None,
        directory=directory,
    )
    display.write(progress_lines("complete", release.current, latest))
    logger.info("Brewski updated to %s.", latest or release.current)
    return True


def restart_service() -> None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        logger.error("systemctl was not found. Start Brewski yourself.")
        return
    subprocess.run([systemctl, "restart", "brewski"], check=False)


def _check_release(*, opener, revision_path: Path | None, timeout: float) -> ReleaseInfo:
    revision = _read_revision(REVISION_PATH if revision_path is None else revision_path)
    latest, reachable = _fetch_latest(opener, timeout)
    available = False
    if reachable and latest:
        if revision is not None and revision == latest:
            available = False
        else:
            available = is_newer(latest, __version__)
    return ReleaseInfo(
        current=__version__,
        latest=latest,
        available=available,
        reachable=reachable,
        revision=revision,
    )


def _read_revision(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _fetch_latest(opener, timeout: float) -> tuple[str | None, bool]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "brewski",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, True
        logger.info("GitHub returned HTTP %s.", exc.code)
        return None, False
    except Exception:
        logger.info("GitHub could not be reached.")
        return None, False
    if status == 404:
        return None, True
    if status != 200:
        return None, False
    try:
        tag = json.loads(body)["tag_name"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None, False
    if not isinstance(tag, str) or not tag.strip():
        return None, False
    return tag.strip(), True
