import json
import urllib.error

from brewski import __version__
from brewski.lcd import NullDisplay
from brewski.store import Store, merge_settings
from brewski.menu import SoftwareMenu
from brewski.updates import (
    ReleaseInfo,
    about_identity,
    check_release,
    is_newer,
    offer_ends,
    progress_label,
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


def test_main_offers_the_release_when_the_install_is_a_commit(tmp_path):
    revision = tmp_path / "revision"
    revision.write_text(f"{'a' * 40}\n")
    info = check_release(
        opener=_opener(200, {"tag_name": "v0.1.0"}),
        revision_path=revision,
        source="main",
    )
    assert info.available is True
    assert info.ref == "v0.1.0"
    assert info.source == "main"
    assert offer_ends(info) == (__version__, "v0.1.0")


def test_beta_offers_a_new_commit_and_stays_quiet_when_it_is_installed(tmp_path):
    installed = "a" * 40
    offered = "b" * 40
    revision = tmp_path / "revision"
    newer = check_release(
        opener=_opener(200, {"sha": offered}),
        revision_path=revision,
        source="beta",
    )
    assert newer.available is True
    assert newer.latest == offered[:7]
    assert newer.ref == offered
    assert newer.source == "beta"
    assert offer_ends(newer) == (__version__, offered[:7])
    assert progress_label(newer) == f"{__version__} -> {offered[:7]}"

    revision.write_text(f"{offered}\n")
    same = check_release(
        opener=_opener(200, {"sha": offered}),
        revision_path=revision,
        source="beta",
    )
    assert same.available is False
    assert same.reachable is True

    revision.write_text(f"{installed}\n")
    moved = check_release(
        opener=_opener(200, {"sha": offered}),
        revision_path=revision,
        source="beta",
    )
    assert moved.available is True
    assert offer_ends(moved) == (installed[:7], offered[:7])
    assert progress_label(moved) == f"{installed[:7]} -> {offered[:7]}"

    missing = check_release(opener=_opener(404, {}), revision_path=tmp_path / "gone", source="beta")
    assert missing.reachable is True
    assert missing.latest is None
    assert missing.available is False

    offline = check_release(opener=_opener(500, {}), revision_path=tmp_path / "down", source="beta")
    assert offline.reachable is False
    assert offline.available is False


def test_about_names_the_commit_on_beta_and_the_release_on_main():
    installed = "abc1234" + "a" * 33
    assert about_identity("main", "0.6.25", installed) == ("Release", "0.6.25")
    assert about_identity("main", "v0.6.25", None) == ("Release", "0.6.25")
    assert about_identity("beta", "0.6.25", installed) == ("Commit", "abc1234")
    assert about_identity("beta", "0.6.25", "v0.6.25") == ("Release", "0.6.25")
    assert about_identity("beta", "0.6.25", None) == ("Release", "0.6.25")


def test_beta_menu_shows_the_commit_arrow():
    menu = SoftwareMenu()
    installed = "abc1234" + "a" * 33
    offered = "def5678" + "b" * 33
    menu.show_release(
        ReleaseInfo(
            current="0.6.23",
            latest=offered[:7],
            available=True,
            reachable=True,
            revision=installed,
            ref=offered,
            source="beta",
        ),
        True,
    )
    assert menu.lines("0.6.23")[1] == "abc1234 -> def5678"
    menu.show_release(
        ReleaseInfo(
            current="0.6.23",
            latest=installed[:7],
            available=False,
            reachable=True,
            revision=installed,
            source="beta",
        ),
        False,
    )
    assert menu.lines("0.6.23") == ["Check for Update", "Up to date", "abc1234", "STOP back"]


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

    flashes = []

    class Leds:
        def close(self):
            flashes.append("closed")

    monkeypatch.setattr("brewski.updates.open_display", lambda _settings: Panel())
    monkeypatch.setattr("brewski.updates.open_buttons", lambda _settings: Buttons())
    monkeypatch.setattr(
        "brewski.updates.open_prompt_leds",
        lambda _settings: flashes.append("opened") or Leds(),
    )
    installed = []
    monkeypatch.setattr(
        "brewski.updates.run_installer",
        lambda *_args: installed.append(True) or True,
    )
    assert run_boot_update() == 0
    assert installed == [True]
    assert flashes == ["opened", "closed"]


def test_boot_waits_on_the_prompt_when_buttons_cannot_open(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_settings(merge_settings(store.get_settings(), {"setup_complete": True}))
    monkeypatch.setenv("BREWSKI_DATA_DIR", str(tmp_path))
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr("brewski.updates.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("brewski.updates.time.sleep", sleep)
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
        def __init__(self):
            super().__init__()
            self.history: list[list[str]] = []

        @property
        def hardware(self):
            return True

        def write(self, lines):
            super().write(lines)
            self.history.append(list(self.lines))

    class Leds:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    panel = Panel()
    leds = Leds()
    monkeypatch.setattr("brewski.updates.open_display", lambda _settings: panel)
    monkeypatch.setattr("brewski.updates.open_buttons", lambda _settings: None)
    monkeypatch.setattr("brewski.updates.open_prompt_leds", lambda _settings: leds)
    installed = []
    monkeypatch.setattr("brewski.updates.run_installer", lambda *_args: installed.append(True))
    assert run_boot_update() == 0
    assert installed == []
    assert panel.history[0][0] == "Update available"
    assert panel.history[0][2] == "START update"
    assert panel.history[0][3] == "STOP keep 60s"
    assert panel.lines[0] == "Keeping this copy"
    assert clock["now"] >= 60
    assert leds.closed


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
