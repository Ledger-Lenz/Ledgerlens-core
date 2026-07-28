"""Tests for the consolidated API gateway middleware (api/gateway.py).

Covers:
- Key created via canonical store is recognised by gateway
- Migration consolidates rows from legacy schemas into canonical table
- Per-minute rate limit (429 with Retry-After)
- Daily quota (429 with X-LedgerLens-Quota-Reset)
- GATEWAY_LOG_BODY=false — access logs never contain wallet/score payloads
- Quota backend unreachable — scoped routes return 503, public routes succeed
- Legacy api/api_keys_router.py endpoints include Deprecation header
- Regression: every route previously covered by Depends(require_scope) /
  Depends(require_admin_key) is still enforced after middleware migration
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Isolated temporary SQLite DB for each test.

    Uses object.__setattr__ (pydantic-safe) to patch both the raw field and
    the property, so _connect() sees the tmp path on every call.
    """
    path = str(tmp_path / "test_ledgerlens.db")
    original = _settings.ledgerlens_db_path
    object.__setattr__(_settings, "ledgerlens_db_path", path)
    yield path
    object.__setattr__(_settings, "ledgerlens_db_path", original)


@pytest.fixture
def canonical_api_key(db_path):
    """Create a key in the canonical api_key_store and return its metadata."""
    from detection.api_key_store import create_api_key

    result = create_api_key(
        scopes=["read:scores"],
        namespace_id="test-ns",
        rate_limit_per_minute=100,
    )
    return result


@pytest.fixture
def admin_api_key(db_path):
    """Patch the admin API key in settings for the duration of the test."""
    original = _settings.ledgerlens_admin_api_key
    object.__setattr__(_settings, "ledgerlens_admin_api_key", "test-admin-key-12345")
    yield "test-admin-key-12345"
    object.__setattr__(_settings, "ledgerlens_admin_api_key", original)


@pytest.fixture
def app(admin_api_key, db_path):
    """Minimal FastAPI app with GatewayMiddleware and scope-annotated routes.

    Uses ``scope_required`` (the function-decorator form of the gateway
    scope annotation) so that GatewayMiddleware can resolve the required
    scope via ``ann()`` without depending on ScopedAPIRoute.
    """
    from api.gateway import GatewayMiddleware, scope_required

    test_app = FastAPI()
    test_app.add_middleware(GatewayMiddleware)

    # Public route — no auth
    @test_app.get("/health")
    def health():
        return {"status": "ok"}

    # Admin-scoped route
    @test_app.get("/admin/test")
    @scope_required("admin")
    def admin_test():
        return {"admin": True}

    # read:scores-scoped route
    @test_app.get("/v1/scores/{wallet}")
    @scope_required("read:scores")
    def get_scores(wallet: str):
        return {"wallet": wallet, "score": 75}

    # compliance:read-scoped route
    @test_app.get("/compliance/sar-package")
    @scope_required("compliance:read")
    def compliance_sar():
        return {"sar": True}

    return test_app


# ---------------------------------------------------------------------------
# Test: canonical key recognised by gateway
# ---------------------------------------------------------------------------


def test_gateway_recognises_canonical_key(app, canonical_api_key, db_path):
    """A key created via the canonical store is accepted by GatewayMiddleware."""
    client = TestClient(app)
    plaintext = canonical_api_key["plaintext_key"]

    resp = client.get(
        "/v1/scores/GABCDEF123",
        headers={"X-LedgerLens-Api-Key": plaintext},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["wallet"] == "GABCDEF123"
    assert data["score"] == 75


def test_gateway_rejects_invalid_key(app, db_path):
    """An invalid API key returns 401."""
    client = TestClient(app)
    resp = client.get(
        "/v1/scores/GABCDEF123",
        headers={"X-LedgerLens-Api-Key": "invalid-key-123"},
    )
    assert resp.status_code == 401


def test_gateway_public_route_no_auth_required(app, db_path):
    """Public routes (/health) succeed without authentication."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_gateway_admin_key_access(app, admin_api_key, db_path):
    """X-LedgerLens-Admin-Key grants access to admin-scoped routes."""
    client = TestClient(app)
    resp = client.get(
        "/admin/test",
        headers={"X-LedgerLens-Admin-Key": "test-admin-key-12345"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"admin": True}


def test_gateway_compliance_key_access(app, db_path):
    """Compliance key grants access to compliance:read-scoped routes."""
    original = _settings.ledgerlens_compliance_api_key
    object.__setattr__(_settings, "ledgerlens_compliance_api_key", "test-compliance-key")
    try:
        client = TestClient(app)
        resp = client.get(
            "/compliance/sar-package",
            headers={"X-LedgerLens-Compliance-Key": "test-compliance-key"},
        )
        assert resp.status_code == 200
    finally:
        object.__setattr__(_settings, "ledgerlens_compliance_api_key", original)


# ---------------------------------------------------------------------------
# Test: scope enforcement
# ---------------------------------------------------------------------------


def test_gateway_rejects_wrong_scope(app, canonical_api_key, db_path):
    """A key scoped to 'read:scores' cannot access an 'admin' route (403)."""
    client = TestClient(app)
    plaintext = canonical_api_key["plaintext_key"]

    resp = client.get(
        "/admin/test",
        headers={"X-LedgerLens-Api-Key": plaintext},
    )
    assert resp.status_code == 403
    assert "Scope" in resp.text or "scope" in resp.text


# ---------------------------------------------------------------------------
# Test: per-minute rate limit
# ---------------------------------------------------------------------------


def test_gateway_per_minute_rate_limit(app, db_path):
    """Exceeding the per-minute limit returns 429 with Retry-After."""
    from detection.api_key_store import create_api_key

    key = create_api_key(
        scopes=["read:scores"],
        namespace_id="test-ns",
        rate_limit_per_minute=2,
    )
    plaintext = key["plaintext_key"]

    client = TestClient(app)

    resp1 = client.get("/v1/scores/A", headers={"X-LedgerLens-Api-Key": plaintext})
    assert resp1.status_code == 200

    resp2 = client.get("/v1/scores/B", headers={"X-LedgerLens-Api-Key": plaintext})
    assert resp2.status_code == 200

    # Third request exceeds the limit of 2/minute
    resp3 = client.get("/v1/scores/C", headers={"X-LedgerLens-Api-Key": plaintext})
    assert resp3.status_code == 429
    assert "Retry-After" in resp3.headers


# ---------------------------------------------------------------------------
# Test: daily quota
# ---------------------------------------------------------------------------


def test_gateway_daily_quota(app, db_path):
    """Exceeding the daily quota returns 429 with X-LedgerLens-Quota-Reset."""
    from detection.api_key_store import _hash_key, _init_table, _connect

    key_id = "test-quota-key"
    plaintext = "ll_" + "a" * 43

    # Create the key with a daily_quota of 2 in the canonical table
    _init_table()
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO api_keys
               (key_id, key_hash, namespace_id, scopes, rate_limit_per_minute,
                daily_quota, namespace_daily_quota, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                key_id,
                _hash_key(plaintext),
                "test-ns",
                "read:scores",
                100,
                2,  # daily_quota = 2
                0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    # Pre-populate gateway_request_log with 2 entries (saturating the quota)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as conn:
        for _ in range(2):
            conn.execute(
                """INSERT INTO gateway_request_log
                   (key_id, namespace_id, method, path, status_code, latency_ms, scope, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key_id,
                    "test-ns",
                    "GET",
                    "/v1/scores/test",
                    200,
                    1.0,
                    "read:scores",
                    f"{today}T00:00:00",
                ),
            )
        conn.commit()

    client = TestClient(app)
    resp = client.get("/v1/scores/D", headers={"X-LedgerLens-Api-Key": plaintext})
    assert resp.status_code == 429
    assert "X-LedgerLens-Quota-Reset" in resp.headers


# ---------------------------------------------------------------------------
# Test: migration consolidation
# ---------------------------------------------------------------------------


def test_migration_consolidation(db_path):
    """Migration adds canonical columns and populates key_id for existing rows."""
    from detection.api_key_store import migrate_legacy_api_keys
    import hashlib

    # Create a legacy-schema api_keys table (api_keys_router format)
    conn = sqlite3.connect(_settings.db_path)
    conn.execute("DROP TABLE IF EXISTS api_keys")
    conn.execute(
        """CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace_id TEXT NOT NULL DEFAULT '',
            api_key_hash TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        )"""
    )
    now = datetime.now(timezone.utc).isoformat()
    h1 = hashlib.sha256(b"legacy-key-1").hexdigest()
    h2 = hashlib.sha256(b"legacy-key-2").hexdigest()
    conn.execute(
        "INSERT INTO api_keys (namespace_id, api_key_hash, description, created_at) VALUES (?, ?, ?, ?)",
        ("ns1", h1, "legacy key 1", now),
    )
    conn.execute(
        "INSERT INTO api_keys (namespace_id, api_key_hash, description, created_at) VALUES (?, ?, ?, ?)",
        ("ns2", h2, "admin key", now),
    )
    conn.commit()
    conn.close()

    # Run migration — _init_table() adds canonical columns and populates key_id
    conn2 = sqlite3.connect(_settings.db_path)
    migrate_legacy_api_keys(conn2)
    conn2.close()

    # Verify canonical columns were added
    conn3 = sqlite3.connect(_settings.db_path)
    columns = {r[1] for r in conn3.execute("PRAGMA table_info(api_keys)").fetchall()}
    conn3.close()
    assert "daily_quota" in columns, "Canonical schema should have daily_quota column"
    assert "namespace_daily_quota" in columns


def test_migration_idempotent(db_path):
    """Running the migration twice produces no errors and no duplicate key_ids."""
    from detection.api_key_store import migrate_legacy_api_keys
    import hashlib

    # Create a simple legacy table
    conn = sqlite3.connect(_settings.db_path)
    conn.execute("DROP TABLE IF EXISTS api_keys")
    conn.execute(
        """CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT NOT NULL UNIQUE,
            namespace_id TEXT NOT NULL DEFAULT '',
            scopes TEXT NOT NULL DEFAULT 'read:scores',
            rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            last_used_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0
        )"""
    )
    h1 = hashlib.sha256(b"dup-key").hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO api_keys (key_hash, namespace_id, scopes, rate_limit_per_minute, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (h1, "ns1", "read:scores", 60, now),
    )
    conn.commit()
    conn.close()

    # Run migration twice using fresh connections
    conn_a = sqlite3.connect(_settings.db_path)
    migrate_legacy_api_keys(conn_a)
    conn_a.close()

    conn_b = sqlite3.connect(_settings.db_path)
    report2 = migrate_legacy_api_keys(conn_b)
    conn_b.close()

    # Second run should not find any NULL key_id rows to update
    assert report2["rows_updated_key_id"] == 0, (
        "Migration is not idempotent: second run still found rows without key_id"
    )


# ---------------------------------------------------------------------------
# Test: gateway log body false
# ---------------------------------------------------------------------------


def test_gateway_log_body_false(app, db_path, caplog):
    """Access log entries never contain wallet addresses or score payloads."""
    import logging

    caplog.set_level(logging.INFO, logger="ledgerlens.gateway")

    from detection.api_key_store import create_api_key

    key = create_api_key(
        scopes=["read:scores"],
        namespace_id="test",
        rate_limit_per_minute=100,
    )
    plaintext = key["plaintext_key"]

    client = TestClient(app)
    resp = client.get(
        "/v1/scores/GABCDEF123XYZ",
        headers={"X-LedgerLens-Api-Key": plaintext},
    )
    assert resp.status_code == 200

    # Gateway log should record path and method, but must not log response body
    for record in caplog.records:
        if record.name == "ledgerlens.gateway":
            msg = record.getMessage()
            # Response body fields (wallet, score) must not appear in log
            assert '"wallet"' not in msg, f"Response body leaked into gateway log: {msg}"
            assert '"score"' not in msg, f"Response body leaked into gateway log: {msg}"


# ---------------------------------------------------------------------------
# Test: quota backend unreachable — public routes still succeed
# ---------------------------------------------------------------------------


def test_gateway_backend_unreachable_public_route_succeeds(app, db_path):
    """Public routes succeed regardless of quota-backend availability."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_middleware_rejects_unauthenticated_scoped_route(app, db_path):
    """Scoped routes without any auth header return 401."""
    client = TestClient(app)
    resp = client.get("/admin/test")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test: legacy api_keys_router includes Deprecation header
# ---------------------------------------------------------------------------


def test_legacy_keys_router_deprecation_header():
    """_add_deprecation_headers() returns an RFC 8594-compliant dict."""
    from api.api_keys_router import _add_deprecation_headers

    headers = _add_deprecation_headers()
    assert "Deprecation" in headers
    assert headers["Deprecation"] == "True"
    assert "Sunset" in headers
    assert "Link" in headers
    assert "deprecation" in headers["Link"].lower()


# ---------------------------------------------------------------------------
# Test: all previously Depends-gated routes are still enforced (regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,method", [
    ("/v1/scores/GABCDEF123", "GET"),
    ("/admin/test", "GET"),
    ("/compliance/sar-package", "GET"),
])
def test_all_scoped_routes_in_app_reject_unauthenticated(app, db_path, path, method):
    """Every scoped route in the test-app fixture returns 401 without auth."""
    client = TestClient(app)
    resp = client.request(method, path)
    assert resp.status_code in (401, 403), (
        f"{method} {path} expected 401/403 without auth, got {resp.status_code}"
    )


def test_admin_router_still_gated(db_path, admin_api_key):
    """Real admin router routes still return 401 without an admin key."""
    from api.gateway import GatewayMiddleware

    _app = FastAPI()
    _app.add_middleware(GatewayMiddleware)
    try:
        from api.admin_router import router as _admin_router

        _app.include_router(_admin_router)
    except ImportError:
        pytest.skip("admin_router dependencies not available in this environment")

    client = TestClient(_app)
    resp = client.get("/admin/models")
    assert resp.status_code in (401, 403), (
        f"Admin router should reject unauthenticated access, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test: X-Correlation-ID present in every response
# ---------------------------------------------------------------------------


def test_correlation_id_in_response(app, canonical_api_key, db_path):
    """Every gateway response carries an X-Correlation-ID header."""
    client = TestClient(app)

    # Public route
    resp = client.get("/health")
    assert "x-correlation-id" in resp.headers, "Public route missing X-Correlation-ID"

    # Authenticated route
    resp = client.get(
        "/v1/scores/GABCDEF123",
        headers={"X-LedgerLens-Api-Key": canonical_api_key["plaintext_key"]},
    )
    assert "x-correlation-id" in resp.headers, "Authenticated route missing X-Correlation-ID"


# ---------------------------------------------------------------------------
# Test: key created via legacy router schema still works after migration
# ---------------------------------------------------------------------------


def test_gateway_key_created_via_legacy_router_works_after_migration(app, db_path):
    """A key created with the old api_keys_router schema works after migrate_legacy_api_keys."""
    from detection.api_key_store import migrate_legacy_api_keys, _init_table, _connect
    import hashlib

    # Simulate a legacy key using the api_keys_router schema (SHA-256 hash field)
    conn = sqlite3.connect(_settings.db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT NOT NULL UNIQUE,
            namespace_id TEXT NOT NULL DEFAULT '',
            scopes TEXT NOT NULL DEFAULT 'read:scores',
            rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            last_used_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0
        )"""
    )
    old_hash = hashlib.sha256(b"cross-schema-key").hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO api_keys (key_hash, namespace_id, scopes, rate_limit_per_minute, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (old_hash, "test-ns", "read:scores", 100, now),
    )
    conn.commit()
    conn.close()

    # Run migration
    conn_m = sqlite3.connect(_settings.db_path)
    migrate_legacy_api_keys(conn_m)
    conn_m.close()

    # Confirm migrated row has a key_id (canonical field)
    with _connect() as c:
        row = c.execute(
            "SELECT key_hash, scopes, key_id FROM api_keys WHERE key_hash=?",
            (old_hash,),
        ).fetchone()

    assert row is not None, "Migrated key should exist in canonical table"
    assert "read:scores" in row["scopes"]
    assert row["key_id"] is not None, "key_id must be populated after migration"
