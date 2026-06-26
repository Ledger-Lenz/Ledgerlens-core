from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator

import numpy as np
from scipy.stats import beta as beta_dist

from config.settings import settings

logger = logging.getLogger("ledgerlens.adaptive_reweighter")

_CLASSIFIER_NAMES = ("random_forest", "xgboost", "lightgbm")
_UPDATE_INTERVAL_SECONDS = 900  # 15 minutes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bandit_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    classifier_names TEXT NOT NULL,
    alphas_json      TEXT NOT NULL,
    betas_json       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""


class ThompsonSamplingReweighter:
    def __init__(self, n_classifiers: int = 3):
        self.alphas = np.ones(n_classifiers, dtype=float)
        self.betas = np.ones(n_classifiers, dtype=float)

    def sample_weights(self) -> np.ndarray:
        samples = beta_dist.rvs(self.alphas, self.betas)
        total = samples.sum()
        return samples / total if total > 0 else np.ones(len(self.alphas)) / len(self.alphas)

    def update(self, classifier_idx: int, reward: float) -> None:
        reward = max(0.0, min(1.0, reward))
        self.alphas[classifier_idx] += reward
        self.betas[classifier_idx] += 1.0 - reward

    def mean_weights(self) -> np.ndarray:
        return self.alphas / (self.alphas + self.betas)

    def reset_priors(self) -> None:
        self.alphas = np.ones(len(self.alphas), dtype=float)
        self.betas = np.ones(len(self.betas), dtype=float)

    def current_weights(self) -> dict[str, float]:
        means = self.mean_weights()
        total = means.sum()
        normalised = means / total if total > 0 else means
        return {name: float(w) for name, w in zip(_CLASSIFIER_NAMES, normalised)}


def cusum_detect(errors: list[float], threshold: float = 5.0, slack: float = 0.1) -> bool:
    """One-sided CUSUM test for an upward shift in error rate.

    Uses the first quarter of `errors` as the in-control reference mean.
    Returns True when the cumulative sum exceeds `threshold`.
    """
    if len(errors) < 4:
        return False
    reference_n = max(1, len(errors) // 4)
    mu_0 = float(np.mean(errors[:reference_n]))
    cusum = 0.0
    for e in errors[reference_n:]:
        cusum = max(0.0, cusum + (e - mu_0 - slack))
        if cusum > threshold:
            return True
    return False


@contextmanager
def _connect(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path or settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


def save_state(reweighter: ThompsonSamplingReweighter, db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bandit_state (id, classifier_names, alphas_json, betas_json, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                classifier_names = excluded.classifier_names,
                alphas_json      = excluded.alphas_json,
                betas_json       = excluded.betas_json,
                updated_at       = excluded.updated_at
            """,
            (
                json.dumps(list(_CLASSIFIER_NAMES)),
                json.dumps(reweighter.alphas.tolist()),
                json.dumps(reweighter.betas.tolist()),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def load_state(db_path: str | None = None) -> ThompsonSamplingReweighter | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM bandit_state WHERE id = 1").fetchone()
    if row is None:
        return None
    rw = ThompsonSamplingReweighter(n_classifiers=len(_CLASSIFIER_NAMES))
    rw.alphas = np.array(json.loads(row["alphas_json"]), dtype=float)
    rw.betas = np.array(json.loads(row["betas_json"]), dtype=float)
    return rw


_global_reweighter: ThompsonSamplingReweighter | None = None
_global_lock = threading.Lock()


def get_global_reweighter() -> ThompsonSamplingReweighter | None:
    return _global_reweighter


def _brier_reward(predicted_probability: float, ground_truth: int) -> float:
    p = max(1e-9, min(1 - 1e-9, predicted_probability))
    return 1.0 - (p - ground_truth) ** 2


def _run_update_cycle(
    reweighter: ThompsonSamplingReweighter,
    since: datetime,
    db_path: str | None,
) -> datetime:
    from detection.feedback_store import get_recent_confirmed_labels

    records = get_recent_confirmed_labels(since=since, db_path=db_path)
    if not records:
        return since

    name_to_idx = {name: i for i, name in enumerate(_CLASSIFIER_NAMES)}
    error_series: list[float] = []

    for fb in records:
        idx = name_to_idx.get(fb.model_name)
        if idx is None:
            continue
        reward = _brier_reward(fb.predicted_probability, fb.ground_truth)
        reweighter.update(idx, reward)
        error_series.append(1.0 - reward)

    if cusum_detect(error_series):
        logger.warning("CUSUM regime change detected — resetting bandit priors to Beta(1,1)")
        reweighter.reset_priors()

    return datetime.now(timezone.utc)


def start_background_loop(
    interval_seconds: int = _UPDATE_INTERVAL_SECONDS,
    db_path: str | None = None,
) -> threading.Thread:
    global _global_reweighter

    with _global_lock:
        rw = load_state(db_path)
        if rw is None:
            rw = ThompsonSamplingReweighter(n_classifiers=len(_CLASSIFIER_NAMES))
        _global_reweighter = rw

    def _loop() -> None:
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        while True:
            try:
                with _global_lock:
                    since = _run_update_cycle(_global_reweighter, since, db_path)
                    save_state(_global_reweighter, db_path)
                logger.info("Bandit weights: %s", _global_reweighter.current_weights())
            except Exception:
                logger.exception("Adaptive reweighter update cycle failed")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, daemon=True, name="adaptive-reweighter")
    t.start()
    return t


# ---------------------------------------------------------------------------
# Analyst-feedback adaptive reweighter — Issue-136
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field
from typing import Literal

ModelName = Literal["random_forest", "xgboost", "lightgbm"]

_ENSEMBLE_WEIGHTS_DDL = """
CREATE TABLE IF NOT EXISTS label_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    asset_pair TEXT NOT NULL,
    true_label INTEGER NOT NULL,
    model_scores_json TEXT NOT NULL,
    ensemble_score INTEGER NOT NULL,
    analyst_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ensemble_weights_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weights_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""

_MODEL_NAMES_ORDER = ("random_forest", "xgboost", "lightgbm")
_MAX_DELTA_PER_DAY = 0.1
_MIN_WEIGHT = 0.1
_WINDOW_DAYS = 7


@dataclass
class LabelFeedback:
    wallet: str
    asset_pair: str
    true_label: int
    model_scores: dict[str, float]
    ensemble_score: int
    analyst_id: str
    confirmed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ModelPerformanceTracker:
    """Compute rolling precision, recall, and F1 per model from label feedback."""

    def __init__(self, window_days: int = _WINDOW_DAYS) -> None:
        self.window_days = window_days

    def compute(self, feedbacks: list[LabelFeedback], threshold: float = 0.5) -> dict[str, dict]:
        """Return {model_name: {precision, recall, f1}} for each model."""
        results: dict[str, dict] = {}
        for model in _MODEL_NAMES_ORDER:
            tp = fp = fn = 0
            for fb in feedbacks:
                score = fb.model_scores.get(model, 0.0) / 100.0
                pred = 1 if score >= threshold else 0
                if pred == 1 and fb.true_label == 1:
                    tp += 1
                elif pred == 1 and fb.true_label == 0:
                    fp += 1
                elif pred == 0 and fb.true_label == 1:
                    fn += 1
            precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
            results[model] = {"precision": precision, "recall": recall, "f1": f1}
        return results


class AdaptiveReweighter:
    """Update ensemble weights using EMA of per-model F1 with stability constraints.

    Weights are updated after each batch of confirmed labels.  A stability
    constraint prevents wild swings: no model's weight can change by more than
    ``max_delta_per_day`` per update, and the minimum weight for any model is
    ``min_weight`` (no model is fully suppressed).
    """

    def __init__(
        self,
        db_path: str | None = None,
        window_days: int = _WINDOW_DAYS,
        max_delta_per_day: float = _MAX_DELTA_PER_DAY,
        min_weight: float = _MIN_WEIGHT,
        ema_alpha: float = 0.3,
    ) -> None:
        from config.settings import settings as _s
        self.db_path = db_path or _s.db_path
        self.window_days = window_days
        self.max_delta = max_delta_per_day
        self.min_weight = min_weight
        self.ema_alpha = ema_alpha
        self._tracker = ModelPerformanceTracker(window_days)
        self._weights: dict[str, float] = {m: 1.0 / 3 for m in _MODEL_NAMES_ORDER}
        self._init_db()
        self._load_weights()

    def _init_db(self) -> None:
        from pathlib import Path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_ENSEMBLE_WEIGHTS_DDL)
        conn.commit()
        conn.close()

    def _load_weights(self) -> None:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT weights_json FROM ensemble_weights_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            loaded = json.loads(row[0])
            if set(loaded) == set(_MODEL_NAMES_ORDER):
                self._weights = loaded

    def current_weights(self) -> dict[str, float]:
        return dict(self._weights)

    def record_feedback(self, feedback: LabelFeedback) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO label_feedback (wallet, asset_pair, true_label, model_scores_json, ensemble_score, analyst_id, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feedback.wallet, feedback.asset_pair, feedback.true_label,
             json.dumps(feedback.model_scores), feedback.ensemble_score,
             feedback.analyst_id, now),
        )
        conn.commit()
        conn.close()

    def _get_recent_feedbacks(self) -> list[LabelFeedback]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.window_days)).isoformat()
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT wallet, asset_pair, true_label, model_scores_json, ensemble_score, analyst_id, confirmed_at FROM label_feedback WHERE confirmed_at >= ?",
            (cutoff,),
        ).fetchall()
        conn.close()
        return [
            LabelFeedback(
                wallet=r[0], asset_pair=r[1], true_label=r[2],
                model_scores=json.loads(r[3]), ensemble_score=r[4],
                analyst_id=r[5], confirmed_at=datetime.fromisoformat(r[6]),
            )
            for r in rows
        ]

    def _apply_stability(self, new_weights: dict[str, float]) -> dict[str, float]:
        """Clamp per-model weight changes to max_delta and enforce min_weight."""
        clamped: dict[str, float] = {}
        for model in _MODEL_NAMES_ORDER:
            old = self._weights[model]
            new = new_weights[model]
            delta = max(-self.max_delta, min(self.max_delta, new - old))
            clamped[model] = max(self.min_weight, old + delta)
        # Renormalise to sum=1
        total = sum(clamped.values())
        return {m: w / total for m, w in clamped.items()}

    def update_weights(self) -> dict[str, float]:
        """Recompute weights from recent feedback and persist to SQLite."""
        feedbacks = self._get_recent_feedbacks()
        if not feedbacks:
            return self._weights

        perf = self._tracker.compute(feedbacks)
        f1_scores = {m: perf[m]["f1"] for m in _MODEL_NAMES_ORDER}
        total_f1 = sum(f1_scores.values())

        if total_f1 == 0:
            target = {m: 1.0 / 3 for m in _MODEL_NAMES_ORDER}
        else:
            target = {m: f1_scores[m] / total_f1 for m in _MODEL_NAMES_ORDER}

        # EMA blend towards target
        ema = {
            m: self.ema_alpha * target[m] + (1 - self.ema_alpha) * self._weights[m]
            for m in _MODEL_NAMES_ORDER
        }
        self._weights = self._apply_stability(ema)

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO ensemble_weights_history (weights_json, recorded_at) VALUES (?, ?)",
            (json.dumps(self._weights), now),
        )
        conn.commit()
        conn.close()
        logger.info("Adaptive ensemble weights updated: %s", self._weights)
        return self._weights
