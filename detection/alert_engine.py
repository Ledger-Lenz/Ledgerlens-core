"""Alert deduplication engine (Issue #177).

Tracks per-wallet alert state in SQLite so that a persistently high-scoring
wallet emits exactly one ``alert.opened`` event rather than one per scoring
cycle.  State survives server restarts because it is persisted to SQLite.

State machine
-------------
- INACTIVE  → score >= threshold            → emit ``alert.opened``,  → ACTIVE
- ACTIVE    → score increases by > 10 pts   → emit ``alert.escalated``
- ACTIVE    → score < threshold (1st cycle) → increment below_count to 1
- ACTIVE    → score < threshold (2nd cycle) → increment below_count to 2
- ACTIVE    → score < threshold (3rd cycle) → emit ``alert.resolved``, → INACTIVE
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings

logger = logging.getLogger("ledgerlens.alert_engine")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS alert_dedup_state (
    wallet TEXT PRIMARY KEY,
    alert_active INTEGER NOT NULL DEFAULT 0,
    last_score INTEGER NOT NULL DEFAULT 0,
    below_threshold_count INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT,
    last_updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_dedup_active ON alert_dedup_state (alert_active);
"""

_ESCALATION_DELTA = 10


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


class AlertDeduplicator:
    """Deduplicate high-risk wallet alerts using persistent SQLite state."""

    def __init__(self, threshold: int = 70) -> None:
        self.threshold = threshold
        _init_table()

    def _get_state(self, conn: sqlite3.Connection, wallet: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM alert_dedup_state WHERE wallet=?", (wallet,)
        ).fetchone()

    def _upsert_state(
        self,
        conn: sqlite3.Connection,
        wallet: str,
        alert_active: int,
        last_score: int,
        below_threshold_count: int,
        opened_at: Optional[str],
        now: str,
    ) -> None:
        conn.execute(
            """INSERT INTO alert_dedup_state
               (wallet, alert_active, last_score, below_threshold_count, opened_at, last_updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(wallet) DO UPDATE SET
                   alert_active=excluded.alert_active,
                   last_score=excluded.last_score,
                   below_threshold_count=excluded.below_threshold_count,
                   opened_at=excluded.opened_at,
                   last_updated=excluded.last_updated""",
            (wallet, alert_active, last_score, below_threshold_count, opened_at, now),
        )
        conn.commit()

    def process(self, wallet: str, score: int) -> Optional[dict]:
        """Evaluate a new score for ``wallet`` and return an alert event or None.

        Returns a dict with keys ``event``, ``wallet``, ``score``, ``timestamp``
        or ``None`` when no state transition occurred.
        """
        now = datetime.now(timezone.utc).isoformat()

        with _connect() as conn:
            state = self._get_state(conn, wallet)

            if state is None:
                alert_active = 0
                last_score = 0
                below_count = 0
                opened_at = None
            else:
                alert_active = state["alert_active"]
                last_score = state["last_score"]
                below_count = state["below_threshold_count"]
                opened_at = state["opened_at"]

            event = None

            if not alert_active:
                if score >= self.threshold:
                    event = {"event": "alert.opened", "wallet": wallet, "score": score, "timestamp": now}
                    self._upsert_state(conn, wallet, 1, score, 0, now, now)
                    logger.info("alert.opened wallet=%s score=%d", wallet, score)
                else:
                    self._upsert_state(conn, wallet, 0, score, 0, opened_at, now)
            else:
                if score >= self.threshold:
                    new_below = 0
                    if score - last_score > _ESCALATION_DELTA:
                        event = {"event": "alert.escalated", "wallet": wallet, "score": score,
                                 "previous_score": last_score, "timestamp": now}
                        logger.info("alert.escalated wallet=%s score=%d prev=%d", wallet, score, last_score)
                    self._upsert_state(conn, wallet, 1, score, new_below, opened_at, now)
                else:
                    new_below = below_count + 1
                    if new_below >= 3:
                        event = {"event": "alert.resolved", "wallet": wallet, "score": score, "timestamp": now}
                        self._upsert_state(conn, wallet, 0, score, 0, None, now)
                        logger.info("alert.resolved wallet=%s score=%d", wallet, score)
                    else:
                        self._upsert_state(conn, wallet, 1, score, new_below, opened_at, now)

        return event

    def get_state(self, wallet: str) -> Optional[dict]:
        """Return current dedup state for ``wallet`` or None if never seen."""
        with _connect() as conn:
            row = self._get_state(conn, wallet)
            return dict(row) if row else None

    def list_active_alerts(self) -> list[dict]:
        """Return all wallets currently in the ACTIVE alert state."""
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alert_dedup_state WHERE alert_active=1 ORDER BY opened_at"
            ).fetchall()
            return [dict(r) for r in rows]


_global_deduplicator: Optional[AlertDeduplicator] = None


def get_deduplicator(threshold: int = 70) -> AlertDeduplicator:
    global _global_deduplicator
    if _global_deduplicator is None:
        _global_deduplicator = AlertDeduplicator(threshold=threshold)
    return _global_deduplicator
