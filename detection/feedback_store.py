"""Persist per-model scoring feedback for Bayesian ensemble reweighting,
and analyst label corrections for active-learning retraining.

Records ground-truth labels against stored model predictions so that
:func:`detection.ensemble_reweighter.compute_updated_weights` can update
ensemble weights without a full retrain.

The :class:`AnalystFeedbackStore` extends this module with importance-weighted
analyst corrections that are merged into the training pipeline during retrain.
"""

import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

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
    conn = sqlite3.connect(db_path or settings.db_path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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


def get_recent_confirmed_labels(
    since: datetime,
    db_path: str | None = None,
) -> list[ScoringFeedback]:
    """Return feedback records with confirmed_at >= `since`."""
    cutoff = since.isoformat()
    with _connect(db_path) as conn:
        _init(conn)
        rows = conn.execute(
            "SELECT wallet, asset_pair, model_name, predicted_probability, "
            "ground_truth, scored_at, confirmed_at "
            "FROM scoring_feedback WHERE confirmed_at >= ? "
            "ORDER BY confirmed_at",
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


# ---------------------------------------------------------------------------
# Analyst correction feedback store (active learning)
# ---------------------------------------------------------------------------

FEEDBACK_DECAY_LAMBDA = 0.05

_ANALYST_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS analyst_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    asset_pair      TEXT NOT NULL,
    analyst_label   INTEGER NOT NULL CHECK(analyst_label IN (0, 1)),
    original_score  INTEGER NOT NULL CHECK(original_score BETWEEN 0 AND 100),
    confidence      REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    has_feature_vector INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_feedback_wallet ON analyst_feedback(wallet);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON analyst_feedback(created_at);
"""


@dataclass
class FeedbackRecord:
    id: Optional[int]
    wallet: str
    asset_pair: str
    analyst_label: int
    original_score: int
    confidence: float
    importance_weight: float
    has_feature_vector: bool
    created_at: datetime


class AnalystFeedbackStore:
    """Persist analyst label corrections and compute importance weights.

    At query time, importance weights are computed as:
        weight = confidence * exp(-decay_lambda * days_since_correction)
    ensuring recent corrections dominate training while old ones decay.
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or settings.db_path

    def _connect_and_init(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_ANALYST_CREATE_SQL)
        conn.commit()
        return conn

    def add_correction(
        self,
        wallet: str,
        asset_pair: str,
        analyst_label: int,
        original_score: int,
        confidence: float = 1.0,
    ) -> FeedbackRecord:
        """Persist an analyst correction.

        Returns the persisted FeedbackRecord with computed importance_weight.
        """
        if analyst_label not in (0, 1):
            raise ValueError("analyst_label must be 0 or 1")
        if not (0 <= original_score <= 100):
            raise ValueError("original_score must be 0-100")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError("confidence must be in [0.0, 1.0]")

        has_fv = self._check_feature_vector(wallet)
        now = datetime.now(timezone.utc)

        conn = self._connect_and_init()
        try:
            cursor = conn.execute(
                """INSERT INTO analyst_feedback
                   (wallet, asset_pair, analyst_label, original_score, confidence,
                    has_feature_vector, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (wallet, asset_pair, analyst_label, original_score,
                 confidence, int(has_fv), now.isoformat()),
            )
            conn.commit()
            row_id = cursor.lastrowid
        finally:
            conn.close()

        return FeedbackRecord(
            id=row_id,
            wallet=wallet,
            asset_pair=asset_pair,
            analyst_label=analyst_label,
            original_score=original_score,
            confidence=confidence,
            importance_weight=confidence,
            has_feature_vector=has_fv,
            created_at=now,
        )

    def get_weighted_corrections(
        self,
        since_days: int = 90,
        decay_lambda: float = FEEDBACK_DECAY_LAMBDA,
    ) -> list[tuple[str, int, float]]:
        """Return (wallet, label, weight) tuples for corrections with feature vectors.

        Only corrections with has_feature_vector=True and created_at within
        since_days are returned. Weight = confidence * exp(-lambda * days_elapsed).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        now = datetime.now(timezone.utc)

        conn = self._connect_and_init()
        try:
            rows = conn.execute(
                """SELECT wallet, analyst_label, confidence, created_at
                   FROM analyst_feedback
                   WHERE has_feature_vector = 1 AND created_at >= ?
                   ORDER BY created_at DESC""",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()

        results = []
        for wallet, label, conf, created_at_str in rows:
            created_at = datetime.fromisoformat(created_at_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            days_elapsed = (now - created_at).total_seconds() / 86400.0
            recency_factor = math.exp(-decay_lambda * days_elapsed)
            weight = conf * recency_factor
            results.append((wallet, label, weight))

        return results

    def get_corrections_paginated(
        self,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[FeedbackRecord], int]:
        """Return paginated correction history (most recent first)."""
        conn = self._connect_and_init()
        try:
            total = conn.execute("SELECT COUNT(*) FROM analyst_feedback").fetchone()[0]
            offset = (page - 1) * page_size
            rows = conn.execute(
                """SELECT id, wallet, asset_pair, analyst_label, original_score,
                          confidence, has_feature_vector, created_at
                   FROM analyst_feedback
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (page_size, offset),
            ).fetchall()
        finally:
            conn.close()

        now = datetime.now(timezone.utc)
        records = []
        for row in rows:
            created_at = datetime.fromisoformat(row[7])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            days_elapsed = (now - created_at).total_seconds() / 86400.0
            weight = row[5] * math.exp(-FEEDBACK_DECAY_LAMBDA * days_elapsed)
            records.append(FeedbackRecord(
                id=row[0],
                wallet=row[1],
                asset_pair=row[2],
                analyst_label=row[3],
                original_score=row[4],
                confidence=row[5],
                importance_weight=weight,
                has_feature_vector=bool(row[6]),
                created_at=created_at,
            ))

        return records, total

    def correction_count(self) -> int:
        """Total number of persisted corrections."""
        conn = self._connect_and_init()
        try:
            return conn.execute("SELECT COUNT(*) FROM analyst_feedback").fetchone()[0]
        finally:
            conn.close()

    def _check_feature_vector(self, wallet: str) -> bool:
        """Check if a feature vector exists for this wallet."""
        try:
            from detection.storage import get_feature_vector
            fv = get_feature_vector(wallet, db_path=self._db_path)
            return fv is not None and len(fv) > 0
        except Exception:
            return False
