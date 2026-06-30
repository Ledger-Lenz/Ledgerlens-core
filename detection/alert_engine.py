"""Alert deduplication engine (Issue #177).

Tracks per-wallet alert state in SQLite and emits events only on transitions:
- alert.opened  — score first crosses threshold
- alert.escalated — score increases by > 10 points while alert is active
- alert.resolved  — score below threshold for 3 consecutive cycles (hysteresis)
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings

logger = logging.getLogger("ledgerlens.alert_engine")

_ESCALATION_DELTA = 10
_RESOLUTION_CONSECUTIVE = 3


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alert_states (
            wallet TEXT PRIMARY KEY,
            alert_active INTEGER NOT NULL DEFAULT 0,
            last_score REAL NOT NULL DEFAULT 0,
            below_threshold_streak INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            event_type TEXT NOT NULL,
            score REAL NOT NULL,
            previous_score REAL,
            emitted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alert_events_wallet ON alert_events (wallet);
        """
    )


def _emit_event(
    conn: sqlite3.Connection,
    wallet: str,
    event_type: str,
    score: float,
    previous_score: Optional[float],
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO alert_events (wallet, event_type, score, previous_score, emitted_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (wallet, event_type, score, previous_score, now),
    )
    event = {
        "event_type": event_type,
        "wallet": wallet,
        "score": score,
        "previous_score": previous_score,
        "emitted_at": now,
    }
    logger.info("Alert event: %s wallet=%s score=%.1f", event_type, wallet, score)
    return event


class AlertDeduplicator:
    """Deduplicate alerts by tracking per-wallet active state across scoring cycles."""

    def __init__(self, db_path: Optional[str] = None, threshold: Optional[int] = None):
        self._db_path = db_path or settings.db_path
        self._threshold = threshold if threshold is not None else settings.risk_score_threshold

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def process(self, wallet: str, score: float) -> list[dict]:
        """Process a new score observation for wallet. Returns list of emitted events.

        Returns an empty list without updating alert state when an active
        suppression rule exists for the wallet.
        """
        from detection.suppressions import is_suppressed
        rule = is_suppressed(wallet, db_path=self._db_path)
        if rule:
            logger.info(
                "Alert suppressed: wallet=%s rule_id=%d reason=%s",
                wallet,
                rule["id"],
                rule["reason"],
            )
            return []

        events: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            _ensure_tables(conn)

            row = conn.execute(
                "SELECT * FROM alert_states WHERE wallet = ?", (wallet,)
            ).fetchone()

            if row is None:
                alert_active = False
                last_score = 0.0
                below_streak = 0
                opened_at = None
            else:
                alert_active = bool(row["alert_active"])
                last_score = float(row["last_score"])
                below_streak = int(row["below_threshold_streak"])
                opened_at = row["opened_at"]

            above = score >= self._threshold

            if not alert_active:
                if above:
                    # Transition: closed → opened
                    opened_at = now
                    alert_active = True
                    below_streak = 0
                    events.append(_emit_event(conn, wallet, "alert.opened", score, None))
                else:
                    below_streak = 0
            else:
                if above:
                    below_streak = 0
                    if score > last_score + _ESCALATION_DELTA:
                        events.append(
                            _emit_event(conn, wallet, "alert.escalated", score, last_score)
                        )
                else:
                    below_streak += 1
                    if below_streak >= _RESOLUTION_CONSECUTIVE:
                        alert_active = False
                        below_streak = 0
                        opened_at = None
                        events.append(
                            _emit_event(conn, wallet, "alert.resolved", score, last_score)
                        )

            conn.execute(
                """
                INSERT INTO alert_states
                    (wallet, alert_active, last_score, below_threshold_streak, opened_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    alert_active = excluded.alert_active,
                    last_score = excluded.last_score,
                    below_threshold_streak = excluded.below_threshold_streak,
                    opened_at = excluded.opened_at,
                    updated_at = excluded.updated_at
                """,
                (wallet, int(alert_active), score, below_streak, opened_at, now),
            )

        return events

    def get_state(self, wallet: str) -> Optional[dict]:
        with self._connect() as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM alert_states WHERE wallet = ?", (wallet,)
            ).fetchone()
            return dict(row) if row else None

    def get_events(self, wallet: str, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT * FROM alert_events WHERE wallet = ? ORDER BY emitted_at DESC LIMIT ?",
                (wallet, limit),
            ).fetchall()
            return [dict(r) for r in rows]
