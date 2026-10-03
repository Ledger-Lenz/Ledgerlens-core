"""Consolidated API-key router: permission matrix, force-revoke and audit log (Issues #992, #993)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings

ADMIN_KEY = "test-admin-key"
SCOPES = ["read:scores", "write:suppressions", "admin"]


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "api_keys.db")
    with patch.object(_settings, "ledgerlens_db_path", path), \
            patch.object(_settings, "ledgerlens_admin_api_key", ADMIN_KEY):
        yield path


@pytest.fixture
def client(db_path):
    from api.api_key_router import require_scope, router

    test_app = FastAPI()
    test_app.include_router(router)

    for scope in SCOPES:
        test_app.add_api_route(
            f"/needs/{scope}",
            lambda: {"ok": True},
            methods=["GET"],
            dependencies=[Depends(require_scope(scope))],
        )
    return TestClient(test_app)


def _key(scopes):
    from detection.api_key_store import create_api_key

    return create_api_key(scopes=scopes)


def test_legacy_router_removed():
    with pytest.raises(ModuleNotFoundError):
        __import__("api.api_keys_router")


@pytest.mark.parametrize("key_scope", SCOPES)
@pytest.mark.parametrize("required", SCOPES)
def test_scope_permission_matrix(client, key_scope, required):
    key = _key([key_scope])
    resp = client.get(f"/needs/{required}", headers={"X-LedgerLens-Api-Key": key["plaintext_key"]})
    expected = 200 if key_scope in (required, "admin") else 403
    assert resp.status_code == expected


@pytest.mark.parametrize("required", SCOPES)
def test_scope_rejects_missing_and_unknown_keys(client, required):
    assert client.get(f"/needs/{required}").status_code == 401
    bad = client.get(f"/needs/{required}", headers={"X-LedgerLens-Api-Key": "ll_bogus"})
    assert bad.status_code == 401


@pytest.mark.parametrize("admin_header,expected", [(None, 401), ("wrong", 403), (ADMIN_KEY, 201)])
def test_management_endpoints_require_admin_key(client, admin_header, expected):
    headers = {"X-LedgerLens-Admin-Key": admin_header} if admin_header else {}
    resp = client.post("/admin/api-keys", json={"scopes": ["read:scores"]}, headers=headers)
    assert resp.status_code == expected


def test_scoped_key_cannot_manage_keys(client):
    key = _key(["admin"])
    resp = client.post(
        "/admin/api-keys",
        json={"scopes": ["read:scores"]},
        headers={"X-LedgerLens-Admin-Key": key["plaintext_key"]},
    )
    assert resp.status_code == 403


def test_rotated_key_valid_only_within_grace_period(db_path):
    from detection.api_key_store import lookup_key, rotate_api_key, sweep_expired_api_keys

    old = _key(["read:scores"])
    new = rotate_api_key(old["key_id"], grace_period_seconds=60)
    assert lookup_key(old["plaintext_key"]) is not None
    assert lookup_key(new["plaintext_key"]) is not None

    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    with patch("detection.api_key_store.datetime") as mock_dt:
        mock_dt.now.return_value = future
        assert lookup_key(old["plaintext_key"]) is None
        assert sweep_expired_api_keys() == 1
    assert lookup_key(new["plaintext_key"]) is not None


def test_force_revoke_ignores_grace_period(client):
    from detection.api_key_store import lookup_key, rotate_api_key

    old = _key(["read:scores"])
    rotate_api_key(old["key_id"], grace_period_seconds=3600)
    assert lookup_key(old["plaintext_key"]) is not None

    resp = client.post(
        f"/admin/api-keys/{old['key_id']}/force-revoke",
        params={"reason": "leaked in logs"},
        headers={"X-LedgerLens-Admin-Key": ADMIN_KEY},
    )
    assert resp.status_code == 200
    assert lookup_key(old["plaintext_key"]) is None
    resp = client.get("/needs/read:scores", headers={"X-LedgerLens-Api-Key": old["plaintext_key"]})
    assert resp.status_code == 401

    again = client.post(
        f"/admin/api-keys/{old['key_id']}/force-revoke", headers={"X-LedgerLens-Admin-Key": ADMIN_KEY}
    )
    assert again.status_code == 404


def test_rotation_and_revocation_events_are_audited(client):
    from detection.api_key_store import (
        force_revoke_api_key,
        list_api_key_audit_events,
        revoke_api_key,
        rotate_api_key,
    )

    a = _key(["read:scores"])
    b = _key(["read:scores"])
    new = rotate_api_key(a["key_id"], grace_period_seconds=60)
    force_revoke_api_key(new["key_id"], reason="compromise")
    revoke_api_key(b["key_id"])

    events = [(e["key_id"], e["event"]) for e in list_api_key_audit_events()]
    assert events == [
        (a["key_id"], "rotated"),
        (new["key_id"], "force_revoked"),
        (b["key_id"], "revoked"),
    ]
    rotated = list_api_key_audit_events(a["key_id"])[0]
    assert rotated["related_key_id"] == new["key_id"]

    resp = client.get("/admin/api-keys/audit", headers={"X-LedgerLens-Admin-Key": ADMIN_KEY})
    assert resp.status_code == 200
    assert len(resp.json()) == 3
