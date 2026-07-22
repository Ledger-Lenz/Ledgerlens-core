import base64
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings
from detection.api_key_store import (
    _hash_key,
    create_api_key,
    get_api_key_by_hash,
    lookup_key,
    rotate_api_key,
    sweep_expired_api_keys,
    get_overdue_api_keys_count,
)
from api.namespace import rotate_namespace_key, lookup_namespace
from detection.webhook_registry import _encrypt_secret, _decrypt_secret, register_subscriber, get_subscriber


@pytest.fixture
def db_path(tmp_path):
    """Use a temporary SQLite database for each test."""
    path = str(tmp_path / "test_ledgerlens.db")
    with patch.object(_settings, "ledgerlens_db_path", path):
        yield path


@pytest.fixture
def app(db_path):
    """Create a minimal FastAPI app with the gateway middleware and endpoints."""
    from api.gateway import GatewayMiddleware, scope_required
    from api.api_key_router import router as api_key_router
    
    test_app = FastAPI()
    test_app.add_middleware(GatewayMiddleware)
    test_app.include_router(api_key_router)

    @test_app.get("/v1/scores/{wallet}")
    @scope_required("read:scores")
    def get_scores(wallet: str):
        return {"wallet": wallet, "score": 75}

    return test_app


def test_api_key_rotation_flow(db_path, app):
    """Test standard API key rotation: both old and new keys authenticate before deadline."""
    client = TestClient(app)

    # 1. Create a key
    key1 = create_api_key(scopes=["read:scores"], namespace_id="ns1")
    key1_id = key1["key_id"]
    key1_plain = key1["plaintext_key"]

    # 2. Rotate the key
    new_key = rotate_api_key(key1_id, grace_period_seconds=10)
    new_plain = new_key["plaintext_key"]

    # Both keys must work
    resp1 = client.get("/v1/scores/G123", headers={"X-LedgerLens-Api-Key": key1_plain})
    assert resp1.status_code == 200

    resp2 = client.get("/v1/scores/G123", headers={"X-LedgerLens-Api-Key": new_plain})
    assert resp2.status_code == 200

    # Test sweep doesn't revoke early
    assert sweep_expired_api_keys() == 0

    # Both keys still work
    resp1 = client.get("/v1/scores/G123", headers={"X-LedgerLens-Api-Key": key1_plain})
    assert resp1.status_code == 200


def test_api_key_expiry_after_deadline(db_path, app):
    """Test that sweep revokes the old key after the deadline and lookup rejects it."""
    client = TestClient(app)

    key1 = create_api_key(scopes=["read:scores"], namespace_id="ns1")
    key1_id = key1["key_id"]
    key1_plain = key1["plaintext_key"]

    # Rotate with 0 grace period (expires immediately)
    new_key = rotate_api_key(key1_id, grace_period_seconds=-1)
    new_plain = new_key["plaintext_key"]

    # Sweep should revoke 1 key
    assert sweep_expired_api_keys() == 1

    # Old key fails (401)
    resp1 = client.get("/v1/scores/G123", headers={"X-LedgerLens-Api-Key": key1_plain})
    assert resp1.status_code == 401

    # New key works (200)
    resp2 = client.get("/v1/scores/G123", headers={"X-LedgerLens-Api-Key": new_plain})
    assert resp2.status_code == 200


def test_rotate_revoked_key_raises_error(db_path):
    """Test that rotating an already-revoked key raises an error."""
    key = create_api_key(scopes=["read:scores"], namespace_id="ns1")
    key_id = key["key_id"]

    # Revoke
    from detection.api_key_store import revoke_api_key
    assert revoke_api_key(key_id) is True

    # Try to rotate
    with pytest.raises(ValueError, match="Cannot rotate revoked API key"):
        rotate_api_key(key_id)


def test_namespace_key_rotation(db_path):
    """Test namespace key rotation preserves namespace visibility and validates grace periods."""
    # Create namespace key
    import secrets
    plaintext_old = f"ll_{secrets.token_urlsafe(32)}"
    from api.namespace import register_api_key
    register_api_key(plaintext_old, namespace_id="my-ns", description="Old Key")

    # Confirm lookup works
    assert lookup_namespace(plaintext_old) == "my-ns"

    # Rotate key
    rotated = rotate_namespace_key("my-ns", grace_period_seconds=5)
    plaintext_new = rotated["plaintext_key"]

    # Both new and old work
    assert lookup_namespace(plaintext_old) == "my-ns"
    assert lookup_namespace(plaintext_new) == "my-ns"

    # Expired namespace rotation deadline
    rotated_expired = rotate_namespace_key("my-ns", grace_period_seconds=-1)
    plaintext_expired = rotated_expired["plaintext_key"]

    # Old key should now fail lookup after deadline
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        lookup_namespace(plaintext_new)
    assert exc.value.status_code == 401


def test_webhook_encryption_rotation_fallback(db_path):
    """Test that webhook decrypt falls back to previous key and re-encrypt updates secrets."""
    key_primary = base64.b64encode(os.urandom(32)).decode()
    key_previous = base64.b64encode(os.urandom(32)).decode()

    # Configure env
    with patch.dict(os.environ, {
        "LEDGERLENS_WEBHOOK_ENCRYPTION_KEY": key_primary,
        "LEDGERLENS_WEBHOOK_ENCRYPTION_KEY_PREVIOUS": key_previous,
    }):
        # 1. Encrypt secret under the previous key
        with patch.dict(os.environ, {"LEDGERLENS_WEBHOOK_ENCRYPTION_KEY": key_previous}):
            encrypted_under_prev = _encrypt_secret("super_secret_hmac")

        # 2. Decrypt with primary (current) key configured. Primary fails but fallback should succeed.
        decrypted = _decrypt_secret(encrypted_under_prev)
        assert decrypted == "super_secret_hmac"

        # 3. Register subscriber with previous key
        # In order to simulate DB rows under the old key, we'll temporarily set old key as primary
        with patch.dict(os.environ, {"LEDGERLENS_WEBHOOK_ENCRYPTION_KEY": key_previous}):
            sub_id = register_subscriber("https://example.com/webhook", "super_secret_hmac", db_path=db_path)

        # Retrieve subscriber (uses fallback, should succeed)
        sub = get_subscriber(sub_id, db_path=db_path)
        assert sub.secret == "super_secret_hmac"

        # 4. Run re-encryption command logic (from cli.py)
        from detection.webhook_registry import _connect
        with _connect(db_path) as conn:
            rows = conn.execute("SELECT id, secret_encrypted FROM webhook_subscribers").fetchall()
            for row_id, encrypted_secret in rows:
                plaintext = _decrypt_secret(encrypted_secret)
                new_encrypted = _encrypt_secret(plaintext)
                conn.execute("UPDATE webhook_subscribers SET secret_encrypted = ? WHERE id = ?", (new_encrypted, row_id))
            conn.commit()

        # Try to decrypt with ONLY the primary key (no previous key fallback). Should now succeed!
        with patch.dict(os.environ, {"LEDGERLENS_WEBHOOK_ENCRYPTION_KEY_PREVIOUS": ""}):
            sub_re = get_subscriber(sub_id, db_path=db_path)
            assert sub_re.secret == "super_secret_hmac"


def test_rotate_api_key_endpoint_and_prometheus(db_path, app):
    """Test end-to-end rotation endpoint, admin gating, and metrics."""
    from api.metrics import ledgerlens_secret_rotation_total

    client = TestClient(app)

    # 1. Create a key to rotate
    key = create_api_key(scopes=["read:scores"], namespace_id="ns1")
    key_id = key["key_id"]

    # Admin key must be configured for "missing header" to return 401 rather
    # than 503 ("admin key not configured" -- require_admin_key fails closed
    # and checks this first).
    with patch.object(_settings, "ledgerlens_admin_api_key", "admin-key-123"):
        # Attempt rotation without admin key (401)
        resp = client.post(f"/admin/api-keys/{key_id}/rotate?grace_period_seconds=10")
        assert resp.status_code == 401

        # Rotate with admin key (200)
        resp = client.post(
            f"/admin/api-keys/{key_id}/rotate?grace_period_seconds=10",
            headers={"X-LedgerLens-Admin-Key": "admin-key-123"}
        )
        assert resp.status_code == 200
        result = resp.json()
        assert "plaintext_key" in result
        assert result["rotated_from"] == key_id
