"""Regression coverage for the api/admin_router.py cleanup (Issue #406)."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "admin_router.db")
    with patch.object(_settings, "ledgerlens_db_path", path):
        yield path


@pytest.fixture
def client(db_path, tmp_path):
    from api.admin_router import require_break_glass, router
    from api.auth import require_admin_key

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin_key] = lambda: None
    app.dependency_overrides[require_break_glass] = lambda: None
    with patch.object(_settings, "model_dir", str(tmp_path / "models")):
        yield TestClient(app)


def test_get_config_returns_empty_when_table_missing(client):
    """A database without `runtime_config` yields {} rather than a 500."""
    resp = client.get("/admin/config")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_patch_config_round_trips(client):
    """PATCH creates the table lazily and the value is readable afterwards."""
    with patch("api.admin_router.bump_config_version"), patch(
        "api.admin_router.invalidate_runtime_config_cache"
    ):
        resp = client.patch("/admin/config", json={"updates": {"score_threshold": "80"}})
    assert resp.status_code == 200
    assert resp.json() == {"updated": ["score_threshold"]}

    assert client.get("/admin/config").json() == {"score_threshold": "80"}


def test_list_models_handles_missing_model_dir(client):
    """A missing model directory yields an empty list, not an unhandled OSError."""
    resp = client.get("/admin/models")
    assert resp.status_code == 200
    assert resp.json() == []


def test_promote_model_rejects_unknown_version(client):
    resp = client.post("/admin/models/9.9.9/promote")
    assert resp.status_code == 404
    assert "9.9.9" in resp.json()["detail"]


def test_module_has_no_dead_rate_limiter():
    """The unused slowapi limiter and stray logger alias were removed."""
    import api.admin_router as admin_router

    assert not hasattr(admin_router, "_limiter")
    assert not hasattr(admin_router, "_logger")
