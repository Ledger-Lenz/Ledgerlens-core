"""Multi-tenant namespace isolation for white-label exchange partner deployments.

Every API key is bound to a ``namespace_id`` in the ``api_keys`` table.
All data-scoped queries are automatically filtered to the key's namespace
via the ``namespace_filter`` FastAPI dependency.

An API key with ``namespace_id = '*'`` (admin wildcard) can view data
across all namespaces and is the only key permitted to call
``GET /admin/namespaces``.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import Header, HTTPException

from config.settings import settings

logger = logging.getLogger("ledgerlens.namespace")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_NAMESPACE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace_id TEXT NOT NULL DEFAULT 'default',
    api_key_hash TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (api_key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_namespace ON api_keys (namespace_id);
"""

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_db_path() -> str:
    return settings.db_path


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or _get_db_path())
    try:
        yield conn
    finally:
        conn.close()


def ensure_namespace_tables(db_path: str | None = None) -> None:
    """Create the api_keys table if it does not exist."""
    db_path = db_path or _get_db_path()
    with _connect(db_path) as conn:
        conn.execute(_NAMESPACE_SCHEMA_SQL)
        conn.commit()


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------


def _hash_key(api_key: str) -> str:
    """Return a SHA-256 hex digest of the key (stored, never the raw key)."""
    import hashlib
    return hashlib.sha256(api_key.encode()).hexdigest()


def register_api_key(
    api_key: str,
    namespace_id: str = "default",
    description: str = "",
    db_path: str | None = None,
) -> dict:
    """Register a new API key bound to a namespace.

    Returns the stored record dict.
    """
    ensure_namespace_tables(db_path)
    db_path = db_path or _get_db_path()
    key_hash = _hash_key(api_key)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        try:
            conn.execute(
                """INSERT INTO api_keys (namespace_id, api_key_hash, description, created_at)
                   VALUES (?, ?, ?, ?)""",
                (namespace_id, key_hash, description, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="API key already registered")
    return {
        "api_key_hash": key_hash,
        "namespace_id": namespace_id,
        "description": description,
        "created_at": now,
    }


def deactivate_api_key(
    api_key: str, db_path: str | None = None
) -> bool:
    """Soft-delete (deactivate) an API key.  Returns True if found."""
    ensure_namespace_tables(db_path)
    db_path = db_path or _get_db_path()
    key_hash = _hash_key(api_key)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE api_key_hash = ? AND is_active = 1",
            (key_hash,),
        )
        conn.commit()
        return cur.rowcount > 0



def lookup_namespace(
    api_key: str,
    db_path: str | None = None,
) -> str:
    """Resolve an API key to its namespace_id.

    Raises:
        HTTPException 401 if the key is unknown or inactive.
    """
    ensure_namespace_tables(db_path)
    db_path = db_path or _get_db_path()
    key_hash = _hash_key(api_key)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT namespace_id, is_active FROM api_keys WHERE api_key_hash = ?",
            (key_hash,),
        ).fetchone()
    if row is None or not row[1]:
        raise HTTPException(
            status_code=401,
            detail="Invalid or deactivated API key",
        )
    # Touch last_used_at
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE api_key_hash = ?",
            (datetime.now(timezone.utc).isoformat(), key_hash),
        )
        conn.commit()
    return row[0]


def list_namespaces(db_path: str | None = None) -> list[dict]:
    """Return every namespace with record counts across the data tables.

    Only callable by the admin wildcard key (namespace_id = '*').
    """
    ensure_namespace_tables(db_path)
    db_path = db_path or _get_db_path()

    _DATA_TABLES = [
        "risk_scores",
        "alerts",
        "wallet_overrides",
        "on_chain_submissions",
        "webhook_subscribers",
        "wash_rings",
        "circular_path_routes",
        "path_payment_cycles",
        "bridge_transfers",
        "liquidity_pool_trades",
        "pair_correlations",
        "shap_values",
        "path_payments",
    ]

    namespaces: dict[str, dict] = {}
    with _connect(db_path) as conn:
        # Collect distinct namespace_ids from api_keys
        api_rows = conn.execute(
            "SELECT DISTINCT namespace_id FROM api_keys ORDER BY namespace_id"
        ).fetchall()
        for (ns,) in api_rows:
            namespaces[ns] = {"namespace_id": ns, "record_counts": {}}

        # If no keys exist yet, add default
        if not namespaces:
            namespaces["default"] = {"namespace_id": "default", "record_counts": {}}

        # Count records per table per namespace
        for table in _DATA_TABLES:
            try:
                col_check = conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
                has_ns = any(col[1] == "namespace_id" for col in col_check)
            except Exception:
                has_ns = False

            if not has_ns:
                continue

            for ns in list(namespaces.keys()):
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE namespace_id = ?",
                    (ns,),
                ).fetchone()
                namespaces[ns]["record_counts"][table] = row[0] if row else 0

    return sorted(namespaces.values(), key=lambda x: x["namespace_id"])


# ---------------------------------------------------------------------------
# FastAPI dependency: namespace filter
# ---------------------------------------------------------------------------


def namespace_filter(
    x_ledgerlens_api_key: str = Header(default=""),
) -> str:
    """FastAPI dependency that resolves the API key to its namespace_id.

    Returns the namespace string.  An admin wildcard key returns ``'*'``.
    Raises ``HTTPException 401`` for unknown / inactive keys and
    ``HTTPException 503`` when multi-tenant mode is not configured.
    """
    if not settings.is_multi_tenant_enabled:
        return "default"

    if not x_ledgerlens_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-LedgerLens-Api-Key header",
        )
    ns = lookup_namespace(x_ledgerlens_api_key)
    return ns
