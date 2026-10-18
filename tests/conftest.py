import pytest

from brewski.api import create_app
from brewski.controller import Controller
from brewski.sources.dummy import DummySource
from brewski.store import Store, merge_settings


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "brewski.db")


@pytest.fixture
def source():
    return DummySource(noise=False)


@pytest.fixture
def controller(store, source):
    store.save_settings(
        merge_settings(
            store.get_settings(),
            {"learned_coast_f": 3, "learned_coast_seconds": 8, "setup_complete": True},
        )
    )
    return Controller(store, source)


@pytest.fixture
def client(controller):
    app = create_app(controller)
    app.config["TESTING"] = True
    return app.test_client()
