from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-control-plane-key")
    monkeypatch.setenv("API_KEY_ROLE", "admin")
    monkeypatch.setenv("API_KEY_ACTOR", "verified-test-admin")
    app = create_app(tmp_path / "test.db")
    with TestClient(app, headers={"X-API-Key": "test-control-plane-key"}) as test_client:
        yield test_client
