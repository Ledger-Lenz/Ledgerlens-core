"""SQLite-backed store for wallet allowlist/denylist overrides with full audit trail.

Every override requires dual approval and expires (Issue #995); see
:mod:`detection.override_approval` and ``docs/override_policy.md``.
"""

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from config.settings import settings
from detection.override_approval import (
    default_expiry_for_legacy_rows,
    ensure_audit_table,
    list_audit_records,
    record_audit,
    resolve_expiry,
    validate_approval,
)

logger = logging.getLogger("ledgerlens.wallet_overrides")


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS wallet_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    wallet TEXT NOT NULL,
    list_type TEXT NOT NULL CHECK (list_type IN ('allowlist', 'denylist')),
    reason TEXT NOT NULL,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    removed_by TEXT,
    removed_at TEXT,
    approvers TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wallet_overrides_wallet ON wallet_overrides (wallet);
CREATE INDEX IF NOT EXISTS idx_wallet_overrides_list_type ON wallet_overrides (list_type);
CREATE INDEX IF NOT EXISTS idx_wallet_overrides_removed_at ON wallet_overrides (removed_at);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_override_table() -> None:
    with _connect() as conn:
        conn.executescript(_CREATE_TABLE)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(wallet_overrides)").fetchall()}
        for col in ("approvers", "expires_at"):
            if col not in existing:
                conn.execute(f"ALTER TABLE wallet_overrides ADD COLUMN {col} TEXT")
        # Pre-existing overrides were single-approver and never expired: give
        # them a finite expiry so they are re-reviewed rather than kept forever.
        conn.execute(
            "UPDATE wallet_overrides SET expires_at = ? WHERE expires_at IS NULL AND removed_at IS NULL",
            (default_expiry_for_legacy_rows(),),
        )
        ensure_audit_table(conn)
        conn.commit()


# Initialize table at module import time
init_override_table()


def _active_row(conn: sqlite3.Connection, wallet: str, list_type: str, now: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM wallet_overrides WHERE wallet=? AND list_type=? AND removed_at IS NULL AND expires_at > ?",
        (wallet, list_type, now),
    ).fetchone()


def add_override(
    wallet: str,
    list_type: str,
    reason: str,
    added_by: str,
    approvers: list[str] | None = None,
    expires_at: str | None = None,
) -> dict:
    """Apply an override. Raises :class:`ApprovalError` without two distinct approvers."""
    approvers = validate_approval(added_by, approvers, reason)
    expires_at = resolve_expiry(expires_at)
    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        if _active_row(conn, wallet, list_type, now):
            raise ValueError(f"Wallet {wallet} is already on the {list_type}")
        # An expired, never-removed entry is closed out so history stays unambiguous.
        conn.execute(
            "UPDATE wallet_overrides SET removed_by='expired', removed_at=? "
            "WHERE wallet=? AND list_type=? AND removed_at IS NULL",
            (now, wallet, list_type),
        )
        conn.execute(
            "INSERT INTO wallet_overrides (entry_id, wallet, list_type, reason, added_by, added_at, "
            "approvers, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, wallet, list_type, reason, added_by, now, ",".join(approvers), expires_at),
        )
        after = dict(_active_row(conn, wallet, list_type, now))
        record_audit(
            conn, target_type="wallet_override", target_id=entry_id, wallet=wallet, action="add",
            requester=added_by, approvers=approvers, justification=reason, before=None, after=after,
        )
        conn.commit()
    logger.info(
        "Wallet override added: wallet=%s list_type=%s entry_id=%s added_by=%s approvers=%s reason=%s",
        wallet, list_type, entry_id, added_by, approvers, reason,
    )
    return after


def renew_override(
    wallet: str,
    list_type: str,
    renewed_by: str,
    approvers: list[str] | None,
    justification: str,
    expires_at: str | None = None,
) -> dict | None:
    """Extend an active override's expiry. Requires the same dual approval as adding one."""
    approvers = validate_approval(renewed_by, approvers, justification)
    expires_at = resolve_expiry(expires_at)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = _active_row(conn, wallet, list_type, now)
        if not row:
            return None
        before = dict(row)
        conn.execute(
            "UPDATE wallet_overrides SET expires_at=?, approvers=? WHERE entry_id=?",
            (expires_at, ",".join(approvers), row["entry_id"]),
        )
        after = dict(_active_row(conn, wallet, list_type, now))
        record_audit(
            conn, target_type="wallet_override", target_id=row["entry_id"], wallet=wallet, action="renew",
            requester=renewed_by, approvers=approvers, justification=justification, before=before, after=after,
        )
        conn.commit()
    return after


def remove_override(wallet: str, list_type: str, removed_by: str) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM wallet_overrides WHERE wallet=? AND list_type=? AND removed_at IS NULL",
            (wallet, list_type),
        ).fetchone()
        if not row:
            logger.debug("Override not found for removal: wallet=%s list_type=%s", wallet, list_type)
            return None
        conn.execute(
            "UPDATE wallet_overrides SET removed_by=?, removed_at=? WHERE entry_id=?",
            (removed_by, now, row["entry_id"]),
        )
        after = dict(conn.execute("SELECT * FROM wallet_overrides WHERE entry_id=?", (row["entry_id"],)).fetchone())
        # Removal restores normal detection, so it needs no second approver, but it is still audited.
        record_audit(
            conn, target_type="wallet_override", target_id=row["entry_id"], wallet=wallet, action="remove",
            requester=removed_by, approvers=[], justification="removed", before=dict(row), after=after,
        )
        conn.commit()
    logger.info(
        "Wallet override removed: wallet=%s list_type=%s entry_id=%s removed_by=%s",
        wallet, list_type, row["entry_id"], removed_by,
    )
    return {"entry_id": row["entry_id"], "wallet": wallet, "removed_by": removed_by, "removed_at": now}


def get_active_override(wallet: str) -> dict | None:
    """Return the wallet's active, unexpired override, or None."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM wallet_overrides WHERE wallet=? AND removed_at IS NULL AND expires_at > ? LIMIT 1",
            (wallet, now),
        ).fetchone()
        return dict(row) if row else None


def list_override_audit(wallet: str | None = None) -> list[dict]:
    """Return the audit trail for wallet overrides, optionally for one wallet."""
    with _connect() as conn:
        return list_audit_records(conn, target_type="wallet_override", wallet=wallet)


def list_overrides(list_type: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_overrides WHERE list_type=? ORDER BY added_at DESC LIMIT ? OFFSET ?",
            (list_type, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
