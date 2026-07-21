"""API key management with scoped permissions and per-key rate limiting (Issue #195).

Keys are stored as BLAKE2b hashes — the plaintext is returned once on creation and
never persisted. Per-key rate limiting delegates to :mod:`detection.rate_limiter`,
a Redis-backed distributed sliding-window counter (with an in-process fallback
when Redis is unreachable) shared by every enforcement path — the REST gateway
(``api/gateway.py``), the legacy ``require_scope`` dependency (``api/api_key_router.py``),
and the gRPC scoring service (``api/grpc_scoring_service.py``). See
``docs/waf_and_rate_limiting.md`` for the distributed design and its
consistency/failure-mode tradeoffs.
"""

import hashlib
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from detection.rate_limiter import check_rate_limit as _distributed_check_rate_limit

logger = logging.getLogger("ledgerlens.api_key_store")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    namespace_id TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    daily_quota INTEGER NOT NULL DEFAULT 0,
    namespace_daily_quota INTEGER NOT NULL DEFAULT 0,
    monthly_quota INTEGER NOT NULL DEFAULT 0,
    namespace_monthly_quota INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_revoked ON api_keys (revoked);
CREATE INDEX IF NOT EXISTS idx_api_keys_namespace ON api_keys (namespace_id);
"""

_VALID_SCOPES = {"read:scores", "write:suppressions", "admin"}


@contextmanager
def _connect():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _init_table() -> None:
    """Ensure the canonical api_keys table exists with all required columns.

    Handles the case where a legacy api_keys table (from api/api_keys_router.py
    or api/namespace.py) already exists with a different schema by adding any
    missing columns via ALTER TABLE.
    """
    with _connect() as conn:
        # Get existing columns
        existing = {r[1] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}

        if not existing:
            # Table doesn't exist — create from scratch
            conn.executescript(_CREATE_TABLE)
            _ensure_gateway_log_table(conn)
            conn.commit()
            return

        # Table exists — add any missing columns idempotently
        _ensure_column(conn, existing, "daily_quota", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, existing, "namespace_daily_quota", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, existing, "monthly_quota", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, existing, "namespace_monthly_quota", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, existing, "key_id", "TEXT")
        _ensure_column(conn, existing, "scopes", "TEXT NOT NULL DEFAULT 'read:scores'")
        _ensure_column(conn, existing, "rate_limit_per_minute", "INTEGER NOT NULL DEFAULT 60")
        _ensure_column(conn, existing, "expires_at", "TEXT")
        _ensure_column(conn, existing, "last_used_at", "TEXT")
        _ensure_column(conn, existing, "revoked", "INTEGER NOT NULL DEFAULT 0")

        # Ensure indexes exist
        _ensure_index(conn, "idx_api_keys_hash", "api_keys", "key_hash")
        _ensure_index(conn, "idx_api_keys_revoked", "api_keys", "revoked")
        _ensure_index(conn, "idx_api_keys_namespace", "api_keys", "namespace_id")

        # Ensure gateway_request_log table exists
        _ensure_gateway_log_table(conn)

        conn.commit()


def _ensure_column(conn: sqlite3.Connection, existing: set, col: str, definition: str) -> None:
    """Add a column if it doesn't exist. Idempotent."""
    if col not in existing:
        try:
            conn.execute(f"ALTER TABLE api_keys ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # Column already exists or was added concurrently


def _ensure_index(conn: sqlite3.Connection, index_name: str, table: str, column: str) -> None:
    """Create an index if it doesn't exist. Idempotent."""
    try:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})")
    except sqlite3.OperationalError:
        pass


def _hash_key(plaintext: str) -> str:
    return hashlib.blake2b(plaintext.encode(), digest_size=32).hexdigest()


def create_api_key(
    scopes: list[str],
    namespace_id: str = "",
    rate_limit_per_minute: int = 60,
    expires_at: Optional[str] = None,
) -> dict:
    """Create a new API key. Returns the plaintext key once — it is not stored."""
    _init_table()
    invalid = set(scopes) - _VALID_SCOPES
    if invalid:
        raise ValueError(f"Invalid scopes: {sorted(invalid)}. Valid: {sorted(_VALID_SCOPES)}")

    import uuid
    key_id = str(uuid.uuid4())
    plaintext = f"ll_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(plaintext)
    now = datetime.now(timezone.utc).isoformat()

    daily_quota = getattr(settings, "gateway_default_daily_quota", 100000)
    namespace_daily_quota = getattr(settings, "gateway_default_namespace_daily_quota", 0)
    monthly_quota = getattr(settings, "gateway_default_monthly_quota", 0)
    namespace_monthly_quota = getattr(settings, "gateway_default_namespace_monthly_quota", 0)

    with _connect() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, key_hash, namespace_id, scopes, rate_limit_per_minute, "
            "daily_quota, namespace_daily_quota, monthly_quota, namespace_monthly_quota, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key_id, key_hash, namespace_id, ",".join(sorted(scopes)), rate_limit_per_minute,
             daily_quota, namespace_daily_quota, monthly_quota, namespace_monthly_quota, now, expires_at),
        )
        conn.commit()

    return {
        "key_id": key_id,
        "plaintext_key": plaintext,
        "scopes": sorted(scopes),
        "namespace_id": namespace_id,
        "rate_limit_per_minute": rate_limit_per_minute,
        "daily_quota": daily_quota,
        "namespace_daily_quota": namespace_daily_quota,
        "monthly_quota": monthly_quota,
        "namespace_monthly_quota": namespace_monthly_quota,
        "created_at": now,
        "expires_at": expires_at,
    }


def revoke_api_key(key_id: str) -> bool:
    """Revoke a key by ID. Returns True if the key existed and was revoked."""
    _init_table()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked=1 WHERE key_id=? AND revoked=0", (key_id,)
        )
        conn.commit()
        return cur.rowcount > 0


def lookup_key(plaintext: str) -> Optional[dict]:
    """Resolve a plaintext key to its metadata row, or None if invalid/revoked/expired."""
    _init_table()
    key_hash = _hash_key(plaintext)
    now = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash=? AND revoked=0", (key_hash,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] and row["expires_at"] < now:
            return None
        conn.execute(
            "UPDATE api_keys SET last_used_at=? WHERE key_id=?", (now, row["key_id"])
        )
        conn.commit()
        return dict(row)


def check_rate_limit(key_id: str, limit_per_minute: int) -> tuple[bool, int]:
    """Check the per-key per-minute rate limit. Returns (allowed, retry_after_seconds).

    Delegates to :func:`detection.rate_limiter.check_rate_limit` — a Redis-backed
    sliding-window counter shared across every replica and both protocols (REST
    and gRPC), falling back to a local in-process window only while Redis is
    unreachable. This is the single, canonical rate-limit check: every
    enforcement path (``api/gateway.py``, ``api/api_key_router.py``,
    ``api/grpc_scoring_service.py``) calls this same function.
    """
    return _distributed_check_rate_limit(key_id, limit_per_minute)


def list_api_keys() -> list[dict]:
    """Return all API keys (without key_hash)."""
    _init_table()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT key_id, namespace_id, scopes, rate_limit_per_minute, daily_quota, "
            "namespace_daily_quota, created_at, "
            "expires_at, last_used_at, revoked FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_api_key_by_hash(key_hash: str) -> Optional[dict]:
    """Look up an API key by its BLAKE2b hash.

    Returns the full record dict (including key_id) or None if not found,
    revoked, or expired.
    """
    _init_table()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash=? AND revoked=0", (key_hash,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] and row["expires_at"] < now:
            return None
        return dict(row)


def touch_api_key_last_used(key_id: str) -> None:
    """Update the last_used_at timestamp for a key."""
    now = datetime.now(timezone.utc).isoformat()
    _init_table()
    with _connect() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at=? WHERE key_id=?", (now, key_id)
        )
        conn.commit()


def check_daily_quota(key_id: str, daily_limit: int) -> tuple[bool, str]:
    """Check daily request quota for a key.

    Returns (allowed, reset_date_iso). When allowed is False, reset_date_iso
    is the ISO-8601 date when the quota resets (next midnight UTC).
    """
    if daily_limit <= 0:
        return True, ""
    _init_table()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM gateway_request_log WHERE key_id=? AND date(recorded_at)=?",
            (key_id, today),
        ).fetchone()[0]
    if count >= daily_limit:
        from datetime import timedelta
        next_reset = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return False, next_reset.isoformat()
    return True, ""


def check_namespace_quota(namespace_id: str, daily_limit: int) -> tuple[bool, str]:
    """Check daily request quota for a namespace.

    Returns (allowed, reset_date_iso). Wildcard namespace ('*') is exempt.
    """
    if namespace_id == "*" or daily_limit <= 0:
        return True, ""
    _init_table()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM gateway_request_log WHERE namespace_id=? AND date(recorded_at)=?",
            (namespace_id, today),
        ).fetchone()[0]
    if count >= daily_limit:
        from datetime import timedelta
        next_reset = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return False, next_reset.isoformat()
    return True, ""


def check_monthly_quota(key_id: str, monthly_limit: int) -> tuple[bool, str]:
    """Check monthly request quota for a key.

    Returns (allowed, reset_date_iso). When allowed is False, reset_date_iso
    is the ISO-8601 date when the quota resets (first day of next month UTC).
    """
    if monthly_limit <= 0:
        return True, ""
    _init_table()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM gateway_request_log WHERE key_id=? AND strftime('%Y-%m', recorded_at)=?",
            (key_id, month),
        ).fetchone()[0]
    if count >= monthly_limit:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        next_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
        next_month = next_month.replace(day=1)
        return False, next_month.isoformat()
    return True, ""


def check_namespace_monthly_quota(namespace_id: str, monthly_limit: int) -> tuple[bool, str]:
    """Check monthly request quota for a namespace.

    Returns (allowed, reset_date_iso). Wildcard namespace ('*') is exempt.
    """
    if namespace_id == "*" or monthly_limit <= 0:
        return True, ""
    _init_table()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM gateway_request_log WHERE namespace_id=? AND strftime('%Y-%m', recorded_at)=?",
            (namespace_id, month),
        ).fetchone()[0]
    if count >= monthly_limit:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        next_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
        next_month = next_month.replace(day=1)
        return False, next_month.isoformat()
    return True, ""


def log_gateway_request(
    key_id: str,
    namespace_id: str,
    method: str,
    path: str,
    status_code: int,
    latency_ms: float,
    scope: str,
) -> None:
    """Log a gateway request record."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as conn:
            _ensure_gateway_log_table(conn)
            conn.execute(
                """INSERT INTO gateway_request_log
                   (key_id, namespace_id, method, path, status_code, latency_ms, scope, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (key_id, namespace_id, method, path, status_code, latency_ms, scope, now),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to log gateway request")


def _ensure_gateway_log_table(conn: sqlite3.Connection) -> None:
    """Create the gateway_request_log table if it doesn't exist. Idempotent."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gateway_request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT,
            namespace_id TEXT,
            method TEXT,
            path TEXT,
            status_code INTEGER,
            latency_ms REAL,
            scope TEXT,
            recorded_at TEXT
        )"""
    )
    for name, col in [
        ("idx_gateway_log_key", "key_id"),
        ("idx_gateway_log_ns", "namespace_id"),
        ("idx_gateway_log_date", "recorded_at"),
    ]:
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON gateway_request_log ({col})")
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# Consolidation migration helpers
# ---------------------------------------------------------------------------

_LEGACY_API_KEYS_COLS = ["key_hash", "namespace_id", "scopes", "rate_limit_per_minute",
                          "created_at", "expires_at", "last_used_at", "revoked"]
_NAMESPACE_API_KEYS_COLS = ["api_key_hash", "namespace_id", "description", "is_active",
                            "created_at", "last_used_at"]


def migrate_legacy_api_keys(conn: sqlite3.Connection) -> dict:
    """Ensure the canonical api_keys table has all required columns and data.

    Since all three schemas (canonical, api_keys_router, namespace) use the
    same table name ``api_keys``, only one schema is active at a time.
    This migration:

    1. Adds any missing columns to the existing table.
    2. Populates ``key_id`` for rows that have NULL (legacy schemas use
       ``id`` autoincrement).
    3. Ensures all rows have at least ``read:scores`` scope.

    Returns a report dict::
        {"migrated": int, "columns_added": list[str],
         "rows_updated_key_id": int, "rows_updated_scopes": int}
    """
    _init_table()  # Ensures all columns exist

    report = {
        "migrated": 0,
        "columns_added": [],
        "rows_updated_key_id": 0,
        "rows_updated_scopes": 0,
    }

    # Detect which columns existed before _init_table() added missing ones
    columns = {r[1] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    expected = {"key_id", "key_hash", "namespace_id", "scopes", "rate_limit_per_minute",
                "daily_quota", "namespace_daily_quota", "monthly_quota", "namespace_monthly_quota",
                "created_at", "expires_at", "last_used_at", "revoked"}

    # Report any columns that were missing and could not be added
    still_missing = expected - columns
    if still_missing:
        logger.warning("Some columns could not be verified: %s", still_missing)

    # Populate key_id for rows that have NULL (legacy schema used id autoincrement)
    import uuid
    rows_without_key_id = conn.execute(
        "SELECT rowid FROM api_keys WHERE key_id IS NULL"
    ).fetchall()
    for (rowid,) in rows_without_key_id:
        conn.execute(
            "UPDATE api_keys SET key_id=? WHERE rowid=?",
            (str(uuid.uuid4()), rowid),
        )
    report["rows_updated_key_id"] = len(rows_without_key_id)

    # Ensure all rows have scopes set
    cur = conn.execute(
        "UPDATE api_keys SET scopes='read:scores' WHERE scopes IS NULL OR scopes=''"
    )
    report["rows_updated_scopes"] = cur.rowcount

    # Ensure namespace_id has a value
    conn.execute(
        "UPDATE api_keys SET namespace_id='default' WHERE namespace_id IS NULL OR namespace_id=''"
    )

    report["migrated"] = len(rows_without_key_id) + cur.rowcount
    conn.commit()
    return report
