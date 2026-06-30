"""API key management with scoped permissions and per-key rate limiting (Issue #195).

Keys are stored as BLAKE2b hashes — the plaintext is returned once on creation and
never persisted. Per-key rate limiting uses an in-process sliding window counter
(no Redis dependency required for the store itself).
"""

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from config.settings import settings

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    namespace_id TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_revoked ON api_keys (revoked);
"""

_VALID_SCOPES = {"read:scores", "write:suppressions", "admin"}

# In-process sliding window: {key_id -> [timestamps]}
_rate_windows: dict[str, list[float]] = {}
_rate_lock = Lock()


@contextmanager
def _connect():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _init_table() -> None:
    with _connect() as conn:
        conn.executescript(_CREATE_TABLE)


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

    with _connect() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, key_hash, namespace_id, scopes, rate_limit_per_minute, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key_id, key_hash, namespace_id, ",".join(sorted(scopes)), rate_limit_per_minute, now, expires_at),
        )
        conn.commit()

    return {
        "key_id": key_id,
        "plaintext_key": plaintext,
        "scopes": sorted(scopes),
        "namespace_id": namespace_id,
        "rate_limit_per_minute": rate_limit_per_minute,
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
    """Check sliding-window rate limit. Returns (allowed, retry_after_seconds)."""
    now = time.monotonic()
    window = 60.0
    cutoff = now - window

    with _rate_lock:
        timestamps = _rate_windows.get(key_id, [])
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= limit_per_minute:
            oldest = timestamps[0]
            retry_after = int(window - (now - oldest)) + 1
            _rate_windows[key_id] = timestamps
            return False, retry_after
        timestamps.append(now)
        _rate_windows[key_id] = timestamps
        return True, 0


def list_api_keys() -> list[dict]:
    """Return all API keys (without key_hash)."""
    _init_table()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT key_id, namespace_id, scopes, rate_limit_per_minute, created_at, "
            "expires_at, last_used_at, revoked FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
