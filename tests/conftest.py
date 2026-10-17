import pytest

from brewski.api import create_app
from brewski.controller import Controller
from brewski.sources.dummy import DummySource
from brewski.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "brewski.db")


@pytest.fixture
def source():
    return DummySource(noise=False)


@pytest.fixture
def controller(store, source):
    return Controller(store, source)


@pytest.fixture
def client(controller):
    app = create_app(controller)
    app.config["TESTING"] = True
    return app.test_client()
