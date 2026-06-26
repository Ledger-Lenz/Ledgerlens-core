"""Alert suppression rules engine (Issue #178).

Operators can whitelist specific wallets or patterns to prevent false alerts
on known-good actors such as DEX arbitrage bots, AMM liquidity managers, and
Stellar anchor wallets.

The suppression store is backed by a ``alert_suppressions`` table in the
main LedgerLens SQLite database. Rules expire automatically at ``expires_at``
(UTC); expired rules are ignored but not deleted until explicitly removed.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings

logger = logging.getLogger("ledgerlens.suppressions")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS alert_suppressions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet      TEXT    NOT NULL,
    reason      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_suppressions_wallet ON alert_suppressions (wallet);
CREATE INDEX IF NOT EXISTS idx_suppressions_expires_at ON alert_suppressions (expires_at);
"""


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


class SuppressionsStore:
    """CRUD interface for alert suppression rules."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db = db_path or settings.db_path
        self._init_table()

    def _init_table(self) -> None:
        with _connect(self._db) as conn:
            for stmt in _CREATE_TABLE_SQL.strip().split(";"):
                s = stmt.strip()
                if s:
                    conn.execute(s)
            conn.commit()

    def add(self, wallet: str, reason: str, expires_at: str | None = None) -> dict:
        """Insert a new suppression rule and return it as a dict."""
        created_at = datetime.now(timezone.utc).isoformat()
        with _connect(self._db) as conn:
            cur = conn.execute(
                "INSERT INTO alert_suppressions (wallet, reason, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (wallet, reason, created_at, expires_at),
            )
            conn.commit()
            rule_id = cur.lastrowid
        logger.info("Suppression rule added: id=%d wallet=%s reason=%s expires_at=%s", rule_id, wallet, reason, expires_at)
        return {"id": rule_id, "wallet": wallet, "reason": reason, "created_at": created_at, "expires_at": expires_at}

    def list_active(self) -> list[dict]:
        """Return all suppression rules that are not yet expired."""
        now = datetime.now(timezone.utc).isoformat()
        with _connect(self._db) as conn:
            rows = conn.execute(
                "SELECT id, wallet, reason, created_at, expires_at FROM alert_suppressions "
                "WHERE expires_at IS NULL OR expires_at > ?",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, rule_id: int) -> bool:
        """Remove a suppression rule by ID. Returns True if a row was deleted."""
        with _connect(self._db) as conn:
            cur = conn.execute("DELETE FROM alert_suppressions WHERE id = ?", (rule_id,))
            conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("Suppression rule deleted: id=%d", rule_id)
        return deleted

    def is_suppressed(self, wallet: str) -> Optional[dict]:
        """Return the active suppression rule for *wallet*, or None if not suppressed.

        A rule is active when its ``expires_at`` is NULL or still in the future.
        Returns the first matching rule so callers can log the rule ID and reason.
        """
        now = datetime.now(timezone.utc).isoformat()
        with _connect(self._db) as conn:
            row = conn.execute(
                "SELECT id, wallet, reason, created_at, expires_at FROM alert_suppressions "
                "WHERE wallet = ? AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                (wallet, now),
            ).fetchone()
        return dict(row) if row else None


# Module-level singleton
_store: SuppressionsStore | None = None


def get_store(db_path: str | None = None) -> SuppressionsStore:
    global _store
    if _store is None or db_path is not None:
        _store = SuppressionsStore(db_path)
    return _store


def is_suppressed(wallet: str, db_path: str | None = None) -> Optional[dict]:
    """Convenience function: return active suppression rule for wallet, or None."""
    return get_store(db_path).is_suppressed(wallet)


def filter_suppressed_alerts(alerts: list[dict], db_path: str | None = None) -> list[dict]:
    """Remove alerts for suppressed wallets, logging each suppression application.

    Returns only the alerts that should be emitted (un-suppressed wallets).
    """
    store = get_store(db_path)
    passed: list[dict] = []
    for alert in alerts:
        wallet = alert.get("wallet", "")
        rule = store.is_suppressed(wallet)
        if rule:
            logger.info(
                "Alert suppressed: wallet=%s rule_id=%d reason=%s",
                wallet,
                rule["id"],
                rule["reason"],
            )
        else:
            passed.append(alert)
    return passed
