import pathlib

import pytest


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Keep every test off the developer's real credentials and log database."""
    monkeypatch.setenv("LOG_DB_PATH", str(tmp_path / "logs.db"))
    monkeypatch.setenv("LOG_ENABLED", "1")
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("GATEWAY_AUTH_FILE", raising=False)
    monkeypatch.delenv("CODEBUDDY_AUTH_FILE", raising=False)
    # Default auth and credential paths must not fall through to the real $HOME.
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
    yield
