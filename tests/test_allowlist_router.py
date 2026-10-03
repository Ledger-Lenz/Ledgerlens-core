"""Regression coverage for the api/allowlist_router.py cleanup (Issue #407).

Locks in that the router writes through ``detection.wallet_override_store``,
so the row it creates is the same row the scoring path reads back.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings

WALLET = "GA" + "A" * 54
APPROVAL = {"added_by": "ops", "reason": "partner", "approvers": ["alice", "bob"]}


@pytest.fixture
def client(tmp_path):
    path = str(tmp_path / "overrides.db")
    with patch.object(_settings, "ledgerlens_db_path", path):
        from api.allowlist_router import router
        from api.auth import require_admin_key
        from detection.wallet_override_store import init_override_table

        init_override_table()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin_key] = lambda: None
        yield TestClient(app)


def test_add_to_allowlist_is_readable_by_the_scoring_path(client):
    """The insert must succeed against the store-owned schema and be visible to it."""
    from detection.wallet_override_store import get_active_override

    resp = client.post("/admin/allowlist", json={"wallet": WALLET, **APPROVAL})
    assert resp.status_code == 201
    assert resp.json()["list_type"] == "allowlist"

    active = get_active_override(WALLET)
    assert active is not None
    assert active["list_type"] == "allowlist"
    assert active["reason"] == "partner"


def test_duplicate_add_returns_409(client):
    assert client.post("/admin/allowlist", json={"wallet": WALLET, **APPROVAL}).status_code == 201
    resp = client.post("/admin/allowlist", json={"wallet": WALLET, **APPROVAL})
    assert resp.status_code == 409


def test_remove_soft_deletes_and_preserves_history(client):
    client.post("/admin/denylist", json={"wallet": WALLET, **APPROVAL, "reason": "wash"})

    resp = client.delete(f"/admin/denylist/{WALLET}?removed_by=ops")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert resp.json()["removed_by"] == "ops"

    # History is retained in the listing, but no override is active any more.
    from detection.wallet_override_store import get_active_override

    assert get_active_override(WALLET) is None
    assert len(client.get("/admin/denylist").json()) == 1


def test_remove_unknown_wallet_returns_404(client):
    resp = client.delete(f"/admin/allowlist/{WALLET}")
    assert resp.status_code == 404


def test_listing_is_paginated_per_list_type(client):
    client.post("/admin/allowlist", json={"wallet": WALLET, **APPROVAL})
    client.post("/admin/denylist", json={"wallet": "GB" + "B" * 54, **APPROVAL})

    assert len(client.get("/admin/allowlist").json()) == 1
    assert len(client.get("/admin/denylist").json()) == 1
    assert client.get("/admin/allowlist?page=2&page_size=50").json() == []
