"""Basic sanity tests for application bootstrap."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.db import Database
from app.main import app


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_lifespan_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_db_path = tmp_path / "lifespan_test.db"
    test_db = Database(test_db_path)
    monkeypatch.setattr("app.main.db", test_db)

    with TestClient(app):
        # Lifespan should have run migrations on test_db
        assert test_db.get_user_version() >= 2
