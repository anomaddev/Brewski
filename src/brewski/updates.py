"""GitHub release checks and the prompted installer.

A fresh boot does not contact GitHub until setup has saved the I2C pins.
Once those pins are saved, the LCD comes on before the network wait.
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
from dataclasses import dataclass, replace
from pathlib import Path

from brewski import __version__
from brewski.lcd import (
    Display,
    open_buttons,
    open_display,
    open_prompt_leds,
    progress_lines,
    prompt_lines,
    startup_lines,
)
from brewski.network import has_default_route
from brewski.paths import data_directory
from brewski.store import DEFAULT_UPDATE_SOURCE, load_settings

logger = logging.getLogger(__name__)

GITHUB_REPO = "anomaddev/Brewski"
REVISION_PATH = Path("/opt/brewski/revision")
INSTALL_SCRIPT = Path("/opt/brewski/install.sh")
APPLY_UNIT = "brewski-apply-update.service"
PROMPT_SECONDS = 60
NETWORK_WAIT_SECONDS = 20
_CACHE_SECONDS = 300.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, ReleaseInfo]] = {}


@dataclass(frozen=True)
class ReleaseInfo:
    current: str
    latest: str | None
    available: bool
    reachable: bool
    revision: str | None = None
    ref: str | None = None
    source: str = DEFAULT_UPDATE_SOURCE


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
    source: str | None = None,
) -> ReleaseInfo:
    """Compare this install with the update selected in Settings.

    Main uses the latest GitHub release. Beta uses the tip of the beta branch.
    An explicit opener or revision path skips the short cache so tests stay isolated.
    The cache is kept per source, so switching Main to Beta does not reuse the other answer.
    """

    chosen = source if source in {"main", "beta"} else None
    if opener is not None or revision_path is not None:
        return _check_release(
            opener=opener,
            revision_path=revision_path,
            timeout=timeout,
            source=chosen or DEFAULT_UPDATE_SOURCE,
        )
    chosen = chosen or _saved_update_source()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(chosen)
    if cached is not None and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    info = _check_release(opener=None, revision_path=None, timeout=timeout, source=chosen)
    with _cache_lock:
        _cache[chosen] = (time.monotonic(), info)
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
        "ref": data.get("ref") if isinstance(data.get("ref"), str) else None,
    }


def write_status(
    *,
    phase: str,
    current: str,
    latest: str,
    error: str | None,
    directory: Path | None = None,
    ref: str | None = None,
) -> None:
    path = status_path(directory)
    payload = {"phase": phase, "current": current, "latest": latest, "error": error, "ref": ref}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        logger.warning("Could not write %s", path)


def report(directory: Path | None = None) -> dict:
    status = read_status(directory)
    if status["phase"] in {"downloading", "building", "installing"}:
        return {
            "current": status["current"] or __version__,
            "latest": status["latest"] or None,
            "available": True,
            "reachable": True,
            "apply_supported": apply_supported(),
            "phase": status["phase"],
            "error": status["error"],
            "source": _saved_update_source(),
            "ref": status.get("ref"),
        }
    release = check_release()
    return {
        "current": release.current,
        "latest": release.latest,
        "available": release.available,
        "reachable": release.reachable,
        "apply_supported": apply_supported(),
        "phase": status["phase"],
        "error": status["error"],
        "source": release.source,
        "ref": release.ref,
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
    The display opens before the network wait so the panel can show startup.
    """

    directory = data_directory(False)
    settings = load_settings(directory)
    if settings is None or not settings.setup_complete:
        logger.info("Brewski setup has not saved the I2C pins. Skipping the update check.")
        return 0
    display = open_display(settings)
    if display.hardware:
        display.write(startup_lines("Starting"))
        if not _wait_for_route(display):
            logger.info("No network route. Starting the installed version.")
            display.close(clear=False)
            return 0
        display.write(startup_lines("Checking for updates"))
    release = check_release()
    if not release.available:
        if release.reachable:
            logger.info("Brewski %s is current.", release.current)
        else:
            logger.info("GitHub could not be reached. Starting the installed version.")
        if display.hardware:
            display.close(clear=False)
        return 0
    if settings.gpio is None or not settings.gpio.lcd_enabled():
        logger.info("An update is available and the LCD is turned off. Keeping this version.")
        if display.hardware:
            display.close(clear=False)
        return 0
    if not display.hardware:
        logger.warning("An update is available and the LCD could not be opened. Keeping this version.")
        display.close()
        return 0
    left, right = offer_ends(release)
    display.write(prompt_lines(left, right, PROMPT_SECONDS))
    buttons = open_buttons(settings)
    if buttons is None:
        logger.warning(
            "An update is available and the buttons could not be read. "
            "Waiting %ss, then keeping this version.",
            PROMPT_SECONDS,
        )
    leds = open_prompt_leds(settings)
    logger.info("Update %s is available. Waiting %ss for Start or Stop.", right, PROMPT_SECONDS)
    try:
        choice = wait_for_choice(
            display,
            buttons.pressed if buttons is not None else (lambda: None),
            current=left,
            latest=right,
            seconds=PROMPT_SECONDS,
        )
    finally:
        if leds is not None:
            leds.close()
        if buttons is not None:
            buttons.close()
    if choice != "start":
        display.write(progress_lines("kept", left, right))
        time.sleep(1)
        display.close()
        return 0
    run_installer(display, release, directory)
    time.sleep(2)
    display.close()
    return 0


def _wait_for_route(display: Display, seconds: int = NETWORK_WAIT_SECONDS) -> bool:
    """Give the network a short head start, and say so on the panel."""

    if has_default_route():
        return True
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        display.write(startup_lines("Waiting for network"))
        time.sleep(1)
        if has_default_route():
            return True
    display.write(startup_lines("Waiting for network"))
    return False


def run_apply_update() -> int:
    """Install the release the user already accepted, then restart Brewski."""

    from brewski.lcd import NullDisplay

    directory = data_directory(False)
    settings = load_settings(directory)
    release = check_release()
    saved_ref = read_status(directory).get("ref")
    if isinstance(saved_ref, str) and saved_ref.strip():
        release = replace(release, ref=saved_ref.strip())
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
    label = progress_label(release)
    write_status(
        phase="downloading",
        current=release.current,
        latest=latest,
        error=None,
        ref=release.ref,
        directory=directory,
    )
    display.write(progress_lines("downloading", release.current, label))
    stop = threading.Event()

    def mirror() -> None:
        seen = "downloading"
        while not stop.is_set():
            phase = read_status(directory).get("phase") or "downloading"
            if phase != seen and phase != "idle":
                display.write(progress_lines(str(phase), release.current, label))
                seen = str(phase)
            stop.wait(0.25)

    thread = threading.Thread(target=mirror, name="brewski-update-lcd", daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env["BREWSKI_STATUS_FILE"] = str(status_path(directory))
        if release.ref:
            env["BREWSKI_UPDATE_REF"] = release.ref
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
            ref=release.ref,
            directory=directory,
        )
        display.write(progress_lines("failed", release.current, label))
        logger.error("Brewski update failed. Keeping %s.", release.current)
        return False
    write_status(
        phase="complete",
        current=release.current,
        latest=latest,
        error=None,
        ref=release.ref,
        directory=directory,
    )
    display.write(progress_lines("complete", release.current, label))
    logger.info("Brewski updated to %s.", latest or release.current)
    return True


def restart_service() -> None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        logger.error("systemctl was not found. Start Brewski yourself.")
        return
    subprocess.run([systemctl, "restart", "brewski"], check=False)


def request_reboot() -> None:
    """Reboot the Pi. The service account needs passwordless sudo for this."""

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise OSError("systemctl was not found")
    subprocess.run(["sudo", "-n", systemctl, "reboot"], check=True)


def installed_revision(path: Path | None = None) -> str | None:
    """Release tag or commit the installer recorded. A dev checkout has neither."""

    return _read_revision(REVISION_PATH if path is None else path)


def about_identity(source: str, version: str, revision: str | None = None) -> tuple[str, str]:
    """Label and value for About.

    Beta names the installed commit. Main names the release. A beta setting
    with no commit recorded, such as a checkout that has not updated yet,
    still names the release.
    """

    if source == "beta" and revision is not None and _is_commit_sha(revision):
        return "Commit", revision[:7]
    text = (version or "").strip()
    if text[:1] in {"v", "V"}:
        text = text[1:]
    return "Release", text


def offer_ends(release: ReleaseInfo) -> tuple[str, str]:
    """Installed identity, then the offered release or commit, for one LCD line."""

    _label, value = about_identity(release.source, release.current, release.revision)
    return value, release.latest or ""


def progress_label(release: ReleaseInfo) -> str:
    """What the install screen shows while a download is in progress."""

    if release.source == "beta":
        left, right = offer_ends(release)
        return f"{left} -> {right}" if right else left
    return release.latest or ""


def _check_release(*, opener, revision_path: Path | None, timeout: float, source: str) -> ReleaseInfo:
    revision = _read_revision(REVISION_PATH if revision_path is None else revision_path)
    if source == "beta":
        sha, reachable = _fetch_commit(opener, timeout)
        available = bool(reachable and sha and revision != sha)
        return ReleaseInfo(
            current=__version__,
            latest=sha[:7] if sha else None,
            available=available,
            reachable=reachable,
            revision=revision,
            ref=sha,
            source="beta",
        )
    latest, reachable = _fetch_latest(opener, timeout)
    available = False
    if reachable and latest:
        if revision is not None and revision == latest:
            available = False
        elif _is_commit_sha(revision):
            available = True
        else:
            available = is_newer(latest, __version__)
    return ReleaseInfo(
        current=__version__,
        latest=latest,
        available=available,
        reachable=reachable,
        revision=revision,
        ref=latest,
        source="main",
    )


def _read_revision(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _saved_update_source() -> str:
    try:
        settings = load_settings(data_directory(False))
    except Exception:
        return DEFAULT_UPDATE_SOURCE
    if settings is None or settings.update_source not in {"main", "beta"}:
        return DEFAULT_UPDATE_SOURCE
    return settings.update_source


def _is_commit_sha(value: str | None) -> bool:
    if value is None or len(value) != 40:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "brewski",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_token() -> str:
    """Fine-grained or classic token. Never logged.

    The Pi keeps it in the data directory. A dev checkout can set
    BREWSKI_GITHUB_TOKEN, or the same name in a gitignored .env file.
    """

    for key in ("BREWSKI_GITHUB_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    for path in (Path("/var/lib/brewski/github-token"), Path(".env")):
        token = _token_from_file(path)
        if token:
            return token
    return ""


def _token_from_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if path.name != ".env":
        return text.strip()
    for line in text.splitlines():
        if line.startswith("BREWSKI_GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _fetch_commit(opener, timeout: float) -> tuple[str | None, bool]:
    """Full SHA at the tip of the beta branch.

    A missing branch is reachable with nothing to install. Any other failure
    means GitHub could not be read.
    """

    request = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/commits/beta",
        headers=_github_headers(),
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
        sha = json.loads(body)["sha"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None, False
    if not _is_commit_sha(sha):
        return None, False
    return sha, True


def _fetch_latest(opener, timeout: float) -> tuple[str | None, bool]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers=_github_headers(),
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
