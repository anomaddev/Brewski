import json
import urllib.error

from brewski import __version__
from brewski.lcd import NullDisplay
from brewski.store import Store, merge_settings
from brewski.updates import (
    ReleaseInfo,
    check_release,
    is_newer,
    run_boot_update,
    wait_for_choice,
)


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _opener(status, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def opener(_request, timeout):
        assert timeout
        if status == 404:
            raise urllib.error.HTTPError("https://api.github.com", 404, "missing", None, None)
        if status >= 400:
            raise urllib.error.HTTPError("https://api.github.com", status, "error", None, None)
        return _Response(status, body)

    return opener


def test_version_compare():
    assert is_newer("v0.3.0", "0.2.0")
    assert is_newer("0.2.1", "v0.2.0")
    assert not is_newer("v0.2.0", "0.2.0")
    assert not is_newer("0.1.0", "0.2.0")
    assert not is_newer("brewski", "0.2.0")


def test_release_check_without_a_network(tmp_path):
    revision = tmp_path / "revision"
    current = check_release(opener=_opener(200, {"tag_name": f"v{__version__}"}), revision_path=revision)
    assert current.reachable is True
    assert current.available is False

    newer = check_release(opener=_opener(200, {"tag_name": "v9.9.9"}), revision_path=revision)
    assert newer.available is True
    assert newer.latest == "v9.9.9"
    assert newer.current == __version__

    revision.write_text("v9.9.9\n")
    same_tag = check_release(opener=_opener(200, {"tag_name": "v9.9.9"}), revision_path=revision)
    assert same_tag.available is False

    missing = check_release(opener=_opener(404, {}), revision_path=revision)
    assert missing.reachable is True
    assert missing.latest is None
    assert missing.available is False

    offline = check_release(opener=_opener(500, {}), revision_path=tmp_path / "other")
    assert offline.reachable is False
    assert offline.available is False


def test_boot_does_not_check_before_setup(tmp_path, monkeypatch):
    Store(tmp_path)
    monkeypatch.setenv("BREWSKI_DATA_DIR", str(tmp_path))

    def explode(**_kwargs):
        raise AssertionError("GitHub was contacted before setup")

    monkeypatch.setattr("brewski.updates.check_release", explode)
    assert run_boot_update() == 0


def test_boot_does_not_check_when_the_database_is_missing(tmp_path, monkeypatch):
    folder = tmp_path / "empty"
    folder.mkdir()
    monkeypatch.setenv("BREWSKI_DATA_DIR", str(folder))

    def explode(**_kwargs):
        raise AssertionError("GitHub was contacted")

    monkeypatch.setattr("brewski.updates.check_release", explode)
    assert run_boot_update() == 0


def test_boot_declines_when_the_lcd_cannot_open(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_settings(merge_settings(store.get_settings(), {"setup_complete": True}))
    monkeypatch.setenv("BREWSKI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("brewski.updates.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "brewski.updates.check_release",
        lambda **_kwargs: ReleaseInfo(
            current="0.2.0",
            latest="v0.3.0",
            available=True,
            reachable=True,
        ),
    )
    monkeypatch.setattr("brewski.updates.open_display", lambda _settings: NullDisplay())
    installed = []
    monkeypatch.setattr("brewski.updates.run_installer", lambda *_args: installed.append(True))
    assert run_boot_update() == 0
    assert installed == []


def test_boot_installs_when_start_is_pressed(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_settings(merge_settings(store.get_settings(), {"setup_complete": True}))
    monkeypatch.setenv("BREWSKI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("brewski.updates.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "brewski.updates.check_release",
        lambda **_kwargs: ReleaseInfo(
            current="0.2.0",
            latest="v0.3.0",
            available=True,
            reachable=True,
        ),
    )

    class Panel(NullDisplay):
        @property
        def hardware(self):
            return True

    class Buttons:
        def pressed(self):
            return "start"

        def close(self):
            pass

    monkeypatch.setattr("brewski.updates.open_display", lambda _settings: Panel())
    monkeypatch.setattr("brewski.updates.open_buttons", lambda _settings: Buttons())
    installed = []
    monkeypatch.setattr(
        "brewski.updates.run_installer",
        lambda *_args: installed.append(True) or True,
    )
    assert run_boot_update() == 0
    assert installed == [True]


def test_prompt_times_out_as_a_decline():
    clock = {"now": 0.0}

    def now():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    display = NullDisplay()
    assert (
        wait_for_choice(
            display,
            lambda: None,
            current="0.2.0",
            latest="0.3.0",
            seconds=60,
            now=now,
            sleep=sleep,
        )
        == "stop"
    )
    assert display.lines[2] == "START update"
    assert display.lines[0] == "Update available"


def test_prompt_accepts_stop():
    display = NullDisplay()
    assert (
        wait_for_choice(
            display,
            lambda: "stop",
            current="0.2.0",
            latest="0.3.0",
            seconds=60,
            now=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        == "stop"
    )
