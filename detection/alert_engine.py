"""Alert deduplication engine — Issue #177.

Tracks per-wallet alert state in SQLite so that a wallet with a persistently
high risk score generates exactly one ``alert.opened`` event rather than one
per scoring cycle.  Three event types are emitted:

- ``alert.opened``    — wallet score first crosses the threshold from below.
- ``alert.escalated`` — score increases by > 10 points while alert is active.
- ``alert.resolved``  — score has been below the threshold for
                        ``RESOLVE_STREAK`` consecutive scoring cycles
                        (hysteresis guard).

State survives server restarts: all state is written to the ``alert_states``
SQLite table after every call to :meth:`AlertDeduplicator.process`.
"""

import logging
from datetime import datetime, timezone

from detection.storage import get_alert_state, save_alert_state

logger = logging.getLogger("ledgerlens.alert_engine")

# Number of consecutive below-threshold cycles required before resolving.
RESOLVE_STREAK = 3
# Minimum score increase (while active) to emit alert.escalated.
ESCALATION_DELTA = 10


class AlertDeduplicator:
    """Stateful deduplicator that suppresses redundant high-risk notifications.

    Args:
        threshold: Risk score at or above which an alert is considered active.
        db_path:   Optional SQLite path; uses ``settings.db_path`` when omitted.
    """

    def __init__(self, threshold: int = 70, db_path: str | None = None) -> None:
        self.threshold = threshold
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def process(self, wallet: str, score: int) -> str | None:
        """Evaluate ``score`` for ``wallet`` and return an event name or None.

        Returns one of:
        - ``"alert.opened"``    — new alert created.
        - ``"alert.escalated"`` — existing alert escalated.
        - ``"alert.resolved"``  — alert closed after hysteresis window.
        - ``None``              — no state transition (emit nothing).

        Side-effect: persists updated state to SQLite.
        """
        state = get_alert_state(wallet, db_path=self.db_path) or self._new_state(wallet)
        now = datetime.now(timezone.utc).isoformat()
        event: str | None = None

        if score >= self.threshold:
            if not state["alert_active"]:
                # Transition: inactive → active
                state["alert_active"] = True
                state["opened_at"] = now
                state["resolved_at"] = None
                state["below_threshold_streak"] = 0
                event = "alert.opened"
                logger.info("alert.opened wallet=%s score=%d", wallet, score)
            else:
                # Already active — check for escalation
                delta = score - state["last_score"]
                if delta > ESCALATION_DELTA:
                    state["last_escalated_at"] = now
                    event = "alert.escalated"
                    logger.info(
                        "alert.escalated wallet=%s score=%d delta=%d",
                        wallet,
                        score,
                        delta,
                    )
                # Reset streak since we're above threshold
                state["below_threshold_streak"] = 0
        else:
            if state["alert_active"]:
                state["below_threshold_streak"] = state["below_threshold_streak"] + 1
                if state["below_threshold_streak"] >= RESOLVE_STREAK:
                    state["alert_active"] = False
                    state["resolved_at"] = now
                    state["below_threshold_streak"] = 0
                    event = "alert.resolved"
                    logger.info("alert.resolved wallet=%s score=%d", wallet, score)
            # If not active, nothing changes

        state["last_score"] = score
        state["updated_at"] = now
        save_alert_state(state, db_path=self.db_path)
        return event

    def get_state(self, wallet: str) -> dict | None:
        """Return the current deduplication state for ``wallet``, or None."""
        return get_alert_state(wallet, db_path=self.db_path)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _new_state(wallet: str) -> dict:
        return {
            "wallet": wallet,
            "alert_active": False,
            "last_score": 0,
            "below_threshold_streak": 0,
            "opened_at": None,
            "last_escalated_at": None,
            "resolved_at": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
