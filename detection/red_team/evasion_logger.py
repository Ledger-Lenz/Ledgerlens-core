"""SQLite persistence for red team evasion events, plus the hardening trigger.

Every attack the runner mounts against the live model is recorded here.  A
``MODEL_EVASION_DETECTED`` webhook and an automated retraining run fire once
``N_EVASION_TRIGGER`` successful evasions accumulate, closing the feedback loop
between the attacker and the defender.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config.settings import settings
from detection.red_team import EVASION_THRESHOLD, N_EVASION_TRIGGER

logger = logging.getLogger("ledgerlens.red_team.evasion")

MODEL_EVASION_EVENT = "MODEL_EVASION_DETECTED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evasion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_features_json TEXT NOT NULL,
    evasion_features_json TEXT NOT NULL,
    original_score REAL NOT NULL,
    evasion_score REAL NOT NULL,
    attacker_generation INTEGER NOT NULL,
    is_evasion INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evasion_events_created_at ON evasion_events (created_at);
CREATE INDEX IF NOT EXISTS idx_evasion_events_is_evasion ON evasion_events (is_evasion);
"""


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_evasion_db(db_path: str | None = None) -> None:
    """Create the ``evasion_events`` table if it does not yet exist."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_evasion(
    original_features: dict,
    evasion_features: dict,
    original_score: float,
    evasion_score: float,
    attacker_generation: int,
    threshold: float = EVASION_THRESHOLD,
    db_path: str | None = None,
) -> int:
    """Persist a single evasion event and return its row id.

    ``is_evasion`` is derived as ``evasion_score < threshold`` so the log keeps
    both successful evasions and near-misses for later analysis.
    """
    init_evasion_db(db_path)
    is_evasion = int(float(evasion_score) < float(threshold))
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO evasion_events
                (original_features_json, evasion_features_json, original_score,
                 evasion_score, attacker_generation, is_evasion, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                json.dumps(original_features),
                json.dumps(evasion_features),
                float(original_score),
                float(evasion_score),
                int(attacker_generation),
                is_evasion,
                _now_iso(),
            ),
        )
        conn.commit()
        return cursor.lastrowid


def _row_to_event(row: tuple) -> dict:
    return {
        "id": row[0],
        "original_features": json.loads(row[1]),
        "evasion_features": json.loads(row[2]),
        "original_score": row[3],
        "evasion_score": row[4],
        "attacker_generation": row[5],
        "is_evasion": bool(row[6]),
        "created_at": row[7],
    }


def get_evasion_events(
    since: str | None = None,
    only_evasions: bool = False,
    limit: int | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """Return stored evasion events, most recent first, with optional filters."""
    init_evasion_db(db_path)
    conditions: list[str] = []
    params: list = []
    if since is not None:
        conditions.append("created_at >= ?")
        params.append(since)
    if only_evasions:
        conditions.append("is_evasion = 1")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
        SELECT id, original_features_json, evasion_features_json, original_score,
               evasion_score, attacker_generation, is_evasion, created_at
        FROM evasion_events
        {where}
        ORDER BY created_at DESC, id DESC
    """
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_row_to_event(row) for row in rows]


def count_evasions(
    threshold: float = EVASION_THRESHOLD,
    since: str | None = None,
    db_path: str | None = None,
) -> int:
    """Count successful evasions (``evasion_score < threshold``)."""
    init_evasion_db(db_path)
    conditions = ["evasion_score < ?"]
    params: list = [float(threshold)]
    if since is not None:
        conditions.append("created_at >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(conditions)
    with _connect(db_path) as conn:
        (count,) = conn.execute(
            f"SELECT COUNT(*) FROM evasion_events {where}", tuple(params)
        ).fetchone()
    return int(count)


def _emit_evasion_webhook(payload: dict, db_path: str | None = None) -> int:
    """Enqueue a ``MODEL_EVASION_DETECTED`` payload to every active subscriber.

    Returns the number of subscribers the event was enqueued for.  Best-effort:
    delivery failures are handled downstream by the webhook worker.
    """
    from detection.webhook_queue import enqueue, init_db as init_queue
    from detection.webhook_registry import init_db as init_registry, list_subscribers

    init_registry(db_path)
    init_queue(db_path)
    subscribers = list_subscribers(active_only=True, db_path=db_path)
    for sub in subscribers:
        enqueue(sub.subscriber_id, payload, db_path=db_path)
    return len(subscribers)


def maybe_trigger_hardening(
    n_trigger: int = N_EVASION_TRIGGER,
    threshold: float = EVASION_THRESHOLD,
    retrain_callback=None,
    db_path: str | None = None,
) -> bool:
    """Fire the automated hardening cycle once enough evasions accumulate.

    When at least ``n_trigger`` successful evasions are present, this emits a
    ``MODEL_EVASION_DETECTED`` webhook and — if ``retrain_callback`` is given —
    invokes it with the list of evasion events so the model can be retrained on
    the hard examples.  Returns ``True`` iff the trigger fired.
    """
    count = count_evasions(threshold=threshold, db_path=db_path)
    if count < n_trigger:
        return False

    payload = {
        "event_type": MODEL_EVASION_EVENT,
        "evasion_count": count,
        "threshold": threshold,
        "detected_at": _now_iso(),
    }
    try:
        _emit_evasion_webhook(payload, db_path=db_path)
    except Exception:  # pragma: no cover - best-effort notification
        logger.exception("Failed to emit MODEL_EVASION_DETECTED webhook")

    if retrain_callback is not None:
        try:
            retrain_callback(get_evasion_events(only_evasions=True, db_path=db_path))
        except Exception:  # pragma: no cover - retrain is best-effort
            logger.exception("Evasion retrain callback failed")

    return True
