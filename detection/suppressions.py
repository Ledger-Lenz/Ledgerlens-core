"""Alert suppression rules engine (Issue #178).

Operators can whitelist specific wallets or patterns to prevent false alerts
on known-good actors such as DEX arbitrage bots, AMM liquidity managers, and
Stellar anchor wallets.

The suppression store is backed by a ``alert_suppressions`` table in the
main LedgerLens SQLite database. Rules expire automatically at ``expires_at``
(UTC); expired rules are ignored but not deleted until explicitly removed.

Every rule requires dual approval and a finite expiry, and every change is
audited (Issue #995); see :mod:`detection.override_approval`.
"""

from __future__ import annotations

import logging
import sqlite3
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

logger = logging.getLogger("ledgerlens.suppressions")

_COLUMNS = "id, wallet, reason, created_at, expires_at, requested_by, approvers"

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
            existing = {r[1] for r in conn.execute("PRAGMA table_info(alert_suppressions)").fetchall()}
            for col in ("requested_by", "approvers"):
                if col not in existing:
                    conn.execute(f"ALTER TABLE alert_suppressions ADD COLUMN {col} TEXT")
            # Pre-existing permanent rules get a finite expiry so they are re-reviewed.
            conn.execute(
                "UPDATE alert_suppressions SET expires_at = ? WHERE expires_at IS NULL",
                (default_expiry_for_legacy_rows(),),
            )
            ensure_audit_table(conn)
            conn.commit()

    def _get(self, conn: sqlite3.Connection, rule_id: int) -> dict | None:
        row = conn.execute(f"SELECT {_COLUMNS} FROM alert_suppressions WHERE id = ?", (rule_id,)).fetchone()
        return dict(row) if row else None

    def add(
        self,
        wallet: str,
        reason: str,
        expires_at: str | None = None,
        requested_by: str = "",
        approvers: list[str] | None = None,
    ) -> dict:
        """Insert a new suppression rule and return it as a dict.

        Raises :class:`~detection.override_approval.ApprovalError` unless the
        rule has a requester, a reason, and two distinct other approvers.
        ``expires_at`` defaults to ``DEFAULT_TTL_DAYS`` from now.
        """
        approvers = validate_approval(requested_by, approvers, reason)
        expires_at = resolve_expiry(expires_at)
        created_at = datetime.now(timezone.utc).isoformat()
        with _connect(self._db) as conn:
            cur = conn.execute(
                "INSERT INTO alert_suppressions (wallet, reason, created_at, expires_at, requested_by, approvers) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (wallet, reason, created_at, expires_at, requested_by, ",".join(approvers)),
            )
            rule_id = cur.lastrowid
            after = self._get(conn, rule_id)
            record_audit(
                conn, target_type="suppression", target_id=str(rule_id), wallet=wallet, action="add",
                requester=requested_by, approvers=approvers, justification=reason, before=None, after=after,
            )
            conn.commit()
        logger.info(
            "Suppression rule added: id=%d wallet=%s reason=%s expires_at=%s approvers=%s",
            rule_id, wallet, reason, expires_at, approvers,
        )
        return after

    def renew(
        self,
        rule_id: int,
        renewed_by: str,
        approvers: list[str] | None,
        justification: str,
        expires_at: str | None = None,
    ) -> dict | None:
        """Extend a rule's expiry. Requires the same dual approval as adding one."""
        approvers = validate_approval(renewed_by, approvers, justification)
        expires_at = resolve_expiry(expires_at)
        with _connect(self._db) as conn:
            before = self._get(conn, rule_id)
            if before is None:
                return None
            conn.execute(
                "UPDATE alert_suppressions SET expires_at = ?, approvers = ? WHERE id = ?",
                (expires_at, ",".join(approvers), rule_id),
            )
            after = self._get(conn, rule_id)
            record_audit(
                conn, target_type="suppression", target_id=str(rule_id), wallet=before["wallet"], action="renew",
                requester=renewed_by, approvers=approvers, justification=justification, before=before, after=after,
            )
            conn.commit()
        return after

    def list_active(self) -> list[dict]:
        """Return all suppression rules that are not yet expired."""
        now = datetime.now(timezone.utc).isoformat()
        with _connect(self._db) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM alert_suppressions WHERE expires_at > ?",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, rule_id: int, deleted_by: str = "unknown") -> bool:
        """Remove a suppression rule by ID. Returns True if a row was deleted."""
        with _connect(self._db) as conn:
            before = self._get(conn, rule_id)
            cur = conn.execute("DELETE FROM alert_suppressions WHERE id = ?", (rule_id,))
            if before is not None:
                # Removal restores alerting, so it needs no second approver, but it is still audited.
                record_audit(
                    conn, target_type="suppression", target_id=str(rule_id), wallet=before["wallet"],
                    action="delete", requester=deleted_by, approvers=[], justification="deleted",
                    before=before, after=None,
                )
            conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("Suppression rule deleted: id=%d", rule_id)
        return deleted

    def list_audit(self, wallet: str | None = None) -> list[dict]:
        """Return the audit trail for suppression rules, optionally for one wallet."""
        with _connect(self._db) as conn:
            return list_audit_records(conn, target_type="suppression", wallet=wallet)

    def is_suppressed(self, wallet: str) -> dict | None:
        """Return the active suppression rule for *wallet*, or None if not suppressed.

        A rule is active while its ``expires_at`` is still in the future.
        Returns the first matching rule so callers can log the rule ID and reason.
        """
        now = datetime.now(timezone.utc).isoformat()
        with _connect(self._db) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM alert_suppressions "
                "WHERE wallet = ? AND expires_at > ? LIMIT 1",
                (wallet, now),
            ).fetchone()
        return dict(row) if row else None


# Module-level cache of SuppressionsStore instances by db_path
_stores: dict[str | None, SuppressionsStore] = {}


def get_store(db_path: str | None = None) -> SuppressionsStore:
    """Return a SuppressionsStore instance, cached by db_path.

    Each unique db_path gets its own cached instance. Passing db_path=None
    uses the default from settings.db_path.
    """
    if db_path not in _stores:
        _stores[db_path] = SuppressionsStore(db_path)
    return _stores[db_path]


def is_suppressed(wallet: str, db_path: str | None = None) -> dict | None:
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
