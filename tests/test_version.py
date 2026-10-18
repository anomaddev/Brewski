import json
import re
from pathlib import Path

import pytest

from brewski import __version__
from brewski.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_web_package_matches_app_version():
    package = json.loads((ROOT / "web" / "package.json").read_text())
    assert package["version"] == __version__


def test_cli_prints_version(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"brewski {__version__}"
