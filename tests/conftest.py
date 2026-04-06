import pytest


@pytest.fixture(autouse=True)
def set_dummy_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
