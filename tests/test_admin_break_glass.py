"""Break-glass justification + time-boxed elevation for admin actions (Issue #974)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings
from storage import audit_log


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGERLENS_AUDIT_SECRET", "x" * 32)
    path = str(tmp_path / "audit.db")
    real = audit_log.log_break_glass_action

    def _log(actor, justification, db_path=None):
        return real(actor, justification, db_path=path)

    with patch("api.admin_router.log_break_glass_action", side_effect=_log):
        yield path


@pytest.fixture
def client(audit_db, tmp_path):
    from api import admin_router
    from api.auth import require_admin_key

    admin_router._elevations.clear()
    app = FastAPI()
    app.include_router(admin_router.router)
    app.dependency_overrides[require_admin_key] = lambda: None
    with (
        patch.object(_settings, "ledgerlens_db_path", str(tmp_path / "admin.db")),
        patch.object(_settings, "model_dir", str(tmp_path / "models")),
    ):
        yield TestClient(app)


def _elevate(client, justification="Incident INC-42: rollback bad model"):
    resp = client.post("/admin/elevate", json={"justification": justification})
    assert resp.status_code == 200
    return resp.json()["elevation_token"]


def test_sensitive_action_without_justification_is_rejected(client):
    token = _elevate(client)
    resp = client.post(
        "/admin/models/1.0.0/promote", headers={"X-LedgerLens-Elevation-Token": token}
    )
    assert resp.status_code == 400
    assert "justification" in resp.json()["detail"]


def test_elevate_without_justification_is_rejected(client):
    assert client.post("/admin/elevate", json={"justification": "  "}).status_code == 400
    assert client.post("/admin/elevate", json={}).status_code == 422


def test_sensitive_action_without_elevation_is_rejected(client):
    resp = client.post(
        "/admin/models/1.0.0/promote",
        headers={"X-LedgerLens-Justification": "Incident INC-42: rollback"},
    )
    assert resp.status_code == 403


def test_elevation_expires(client):
    from api import admin_router

    token = _elevate(client)
    admin_router._elevations[token] = datetime.now(timezone.utc) - timedelta(seconds=1)
    resp = client.post(
        "/admin/models/1.0.0/promote",
        headers={
            "X-LedgerLens-Elevation-Token": token,
            "X-LedgerLens-Justification": "Incident INC-42: rollback",
        },
    )
    assert resp.status_code == 403
    assert token not in admin_router._elevations


def test_break_glass_action_is_audited(client, audit_db):
    token = _elevate(client)
    resp = client.post(
        "/admin/models/9.9.9/promote",
        headers={
            "X-LedgerLens-Elevation-Token": token,
            "X-LedgerLens-Justification": "Incident INC-42: rollback",
        },
    )
    assert resp.status_code == 404  # passed the gate; version does not exist

    entries = [
        e
        for e in audit_log.get_all_entries(audit_db)
        if e["event_type"] == "break_glass_admin_action"
    ]
    assert len(entries) == 2
    assert entries[0]["justification"] == "elevate: Incident INC-42: rollback bad model"
    assert entries[1]["justification"] == (
        "POST /admin/models/9.9.9/promote: Incident INC-42: rollback"
    )
    assert all(e["actor"].startswith("admin:") and e["timestamp"] for e in entries)
    assert audit_log.is_chain_intact(audit_db)
