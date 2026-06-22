"""Persist per-model scoring feedback for Bayesian ensemble reweighting.

Records ground-truth labels against stored model predictions so that
:func:`detection.ensemble_reweighter.compute_updated_weights` can update
ensemble weights without a full retrain.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from config.settings import settings

_MODEL_NAMES = frozenset({"random_forest", "xgboost", "lightgbm"})

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS scoring_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet      TEXT    NOT NULL,
    asset_pair  TEXT    NOT NULL,
    model_name  TEXT    NOT NULL,
    predicted_probability REAL NOT NULL,
    ground_truth INTEGER NOT NULL,
    scored_at   TEXT    NOT NULL,
    confirmed_at TEXT   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_model_scored_at
    ON scoring_feedback (model_name, scored_at);
"""


class ScoringFeedback(BaseModel):
    wallet: str
    asset_pair: str
    model_name: str  # "random_forest" | "xgboost" | "lightgbm"
    predicted_probability: float
    ground_truth: int  # 1 = confirmed wash, 0 = confirmed clean
    scored_at: datetime
    confirmed_at: datetime


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(_CREATE_SQL)
    conn.commit()


def record_feedback(feedback: ScoringFeedback, db_path: str | None = None) -> None:
    """Persist a single :class:`ScoringFeedback` record to SQLite."""
    with _connect(db_path) as conn:
        _init(conn)
        conn.execute(
            """
            INSERT INTO scoring_feedback
                (wallet, asset_pair, model_name, predicted_probability,
                 ground_truth, scored_at, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback.wallet,
                feedback.asset_pair,
                feedback.model_name,
                feedback.predicted_probability,
                feedback.ground_truth,
                feedback.scored_at.isoformat(),
                feedback.confirmed_at.isoformat(),
            ),
        )
        conn.commit()


def get_recent_feedback(
    days_back: int = 7,
    model_name: str | None = None,
    db_path: str | None = None,
) -> list[ScoringFeedback]:
    """Return feedback records from the last *days_back* days.

    Args:
        days_back: Window size in days (inclusive).
        model_name: When provided, restrict to records for this model.
        db_path: Override the default SQLite path (for testing).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    with _connect(db_path) as conn:
        _init(conn)
        if model_name:
            rows = conn.execute(
                "SELECT wallet, asset_pair, model_name, predicted_probability, "
                "ground_truth, scored_at, confirmed_at "
                "FROM scoring_feedback WHERE model_name = ? AND scored_at >= ? "
                "ORDER BY scored_at",
                (model_name, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT wallet, asset_pair, model_name, predicted_probability, "
                "ground_truth, scored_at, confirmed_at "
                "FROM scoring_feedback WHERE scored_at >= ? "
                "ORDER BY scored_at",
                (cutoff,),
            ).fetchall()

    return [
        ScoringFeedback(
            wallet=r[0],
            asset_pair=r[1],
            model_name=r[2],
            predicted_probability=r[3],
            ground_truth=r[4],
            scored_at=datetime.fromisoformat(r[5]),
            confirmed_at=datetime.fromisoformat(r[6]),
        )
        for r in rows
    ]
