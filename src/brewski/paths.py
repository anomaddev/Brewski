"""Locations for the database and the built dashboard."""

from __future__ import annotations

import os
from pathlib import Path


def data_directory(dev: bool) -> Path:
    """Directory that holds ``brewski.db``.

    ``BREWSKI_DATA_DIR`` wins. Dev mode otherwise uses ``./data`` so a checkout
    on a Mac stays self-contained. A normal install uses the user data dir.
    """

    override = os.environ.get("BREWSKI_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if dev:
        return Path.cwd() / "data"
    return Path.home() / ".local" / "share" / "brewski"


def host_and_port(dev: bool) -> tuple[str, int]:
    if dev:
        host = os.environ.get("BREWSKI_HOST", "127.0.0.1")
        port = int(os.environ.get("BREWSKI_PORT", "5000"))
    else:
        host = os.environ.get("BREWSKI_HOST", "0.0.0.0")
        port = int(os.environ.get("BREWSKI_PORT", "8080"))
    return host, port


def web_project_dir() -> Path | None:
    """The Vite project, when this process is running from a checkout."""

    here = Path(__file__).resolve()
    candidates = [here.parents[2] / "web", Path.cwd() / "web"]
    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return candidate
    return None


def web_dist_dir() -> Path | None:
    """Built dashboard files, if ``npm run build`` has been run.

    An installed copy looks inside the package (``brewski/dist``). A checkout
    looks at ``web/dist``.
    """

    candidates = [Path(__file__).resolve().parent / "dist"]
    project = web_project_dir()
    if project is not None:
        candidates.append(project / "dist")
    candidates.append(Path.cwd() / "web" / "dist")

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if (candidate / "index.html").is_file():
            return candidate
    return None
