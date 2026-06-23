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
