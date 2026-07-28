"""SQLite-backed store for wallet allowlist/denylist overrides with full audit trail."""

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from config.settings import settings

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
    removed_at TEXT
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


# Initialize table at module import time
init_override_table()


def add_override(wallet: str, list_type: str, reason: str, added_by: str) -> dict:
    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT entry_id FROM wallet_overrides WHERE wallet=? AND list_type=? AND removed_at IS NULL",
            (wallet, list_type),
        ).fetchone()
        if existing:
            raise ValueError(f"Wallet {wallet} is already on the {list_type}")
        conn.execute(
            "INSERT INTO wallet_overrides (entry_id, wallet, list_type, reason, added_by, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, wallet, list_type, reason, added_by, now),
        )
        conn.commit()
    logger.info(
        "Wallet override added: wallet=%s list_type=%s entry_id=%s added_by=%s reason=%s",
        wallet, list_type, entry_id, added_by, reason,
    )
    return {"entry_id": entry_id, "wallet": wallet, "list_type": list_type,
            "reason": reason, "added_by": added_by, "added_at": now}


def remove_override(wallet: str, list_type: str, removed_by: str) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT entry_id FROM wallet_overrides WHERE wallet=? AND list_type=? AND removed_at IS NULL",
            (wallet, list_type),
        ).fetchone()
        if not row:
            logger.debug("Override not found for removal: wallet=%s list_type=%s", wallet, list_type)
            return None
        conn.execute(
            "UPDATE wallet_overrides SET removed_by=?, removed_at=? WHERE entry_id=?",
            (removed_by, now, row["entry_id"]),
        )
        conn.commit()
    logger.info(
        "Wallet override removed: wallet=%s list_type=%s entry_id=%s removed_by=%s",
        wallet, list_type, row["entry_id"], removed_by,
    )
    return {"entry_id": row["entry_id"], "wallet": wallet, "removed_by": removed_by, "removed_at": now}


def get_active_override(wallet: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM wallet_overrides WHERE wallet=? AND removed_at IS NULL LIMIT 1",
            (wallet,),
        ).fetchone()
        return dict(row) if row else None


def list_overrides(list_type: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_overrides WHERE list_type=? ORDER BY added_at DESC LIMIT ? OFFSET ?",
            (list_type, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
