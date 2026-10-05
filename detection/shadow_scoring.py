"""Shadow model scoring: run a candidate model in parallel with production.

When SHADOW_MODEL_VERSION is set, every scoring request computes both the
production and shadow scores. The shadow score never affects the API response;
instead, score divergence is logged to a Prometheus histogram and stored in
a SQLite table for offline analysis.

This enables data-driven promotion decisions based on real traffic before
committing to a hard model cutover.

Canary rollout
--------------
A configurable percentage of live traffic (CANARY_TRAFFIC_PERCENT) is routed
through both the candidate and production models. Over a configurable soak
period (CANARY_SOAK_SECONDS) score divergence and alert-rate divergence are
compared. If divergence exceeds CANARY_DIVERGENCE_THRESHOLD the canary is
automatically rolled back and an alert is emitted. Canary results are recorded
so they can be attached to the model's promotion audit trail.
"""

import logging
import math
import os
import random
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Optional


logger = logging.getLogger("ledgerlens.shadow_scoring")

# Normal range for mean absolute shadow-vs-production divergence; a sustained
# mean above this fires the ShadowScoreDivergenceHigh alert
# (monitoring/alerts.yml), which must be kept in sync with this default.
SHADOW_DIVERGENCE_ALERT_MAX = float(os.environ.get("SHADOW_DIVERGENCE_ALERT_MAX", "0.10"))

# Prometheus metrics (lazy import to avoid hard dependency)
_shadow_histogram = None
_shadow_delta_histogram = None


def _get_histogram():
    global _shadow_histogram
    if _shadow_histogram is not None:
        return _shadow_histogram
    try:
        from prometheus_client import Histogram

        _shadow_histogram = Histogram(
            "ledgerlens_shadow_score_divergence",
            "Absolute difference between production and shadow model scores",
            buckets=[0.01, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0],
        )
    except ImportError:
        _shadow_histogram = None
    return _shadow_histogram


def _get_delta_histogram():
    global _shadow_delta_histogram
    if _shadow_delta_histogram is not None:
        return _shadow_delta_histogram
    try:
        from prometheus_client import Histogram

        _shadow_delta_histogram = Histogram(
            "ledgerlens_shadow_score_delta",
            "Signed shadow minus production score delta (bias direction of the shadow model)",
            buckets=[-0.5, -0.2, -0.1, -0.05, -0.01, 0.0, 0.01, 0.05, 0.1, 0.2, 0.5, 1.0],
        )
    except ImportError:
        _shadow_delta_histogram = None
    return _shadow_delta_histogram


def compute_divergence_stats(
    production_scores: Sequence[float], shadow_scores: Sequence[float]
) -> dict:
    """Return the divergence distribution between paired production/shadow scores.

    ``mean_delta`` is signed (shadow - production); the other stats use the
    absolute divergence, matching ``ledgerlens_shadow_score_divergence``.
    """
    if len(production_scores) != len(shadow_scores):
        raise ValueError("production and shadow score sets must be the same length")
    if not production_scores:
        return {"count": 0, "mean_delta": 0.0, "mean_divergence": 0.0,
                "p95_divergence": 0.0, "max_divergence": 0.0}
    deltas = [s - p for p, s in zip(production_scores, shadow_scores)]
    divergences = sorted(abs(d) for d in deltas)
    return {
        "count": len(deltas),
        "mean_delta": sum(deltas) / len(deltas),
        "mean_divergence": sum(divergences) / len(divergences),
        "p95_divergence": _nearest_rank_percentile(divergences, 0.95),
        "max_divergence": divergences[-1],
    }


def divergence_out_of_range(
    window_means: Sequence[float],
    normal_max: float = SHADOW_DIVERGENCE_ALERT_MAX,
    sustained_windows: int = 3,
) -> bool:
    """Whether divergence has trended outside the normal range.

    *window_means* are successive mean-divergence samples (oldest first). The
    alert fires only when the last *sustained_windows* samples all exceed
    *normal_max*, mirroring the ``for:`` clause of the Prometheus alert so a
    single noisy window does not page.
    """
    if sustained_windows < 1 or len(window_means) < sustained_windows:
        return False
    return all(m > normal_max for m in window_means[-sustained_windows:])


def get_shadow_model_version() -> Optional[str]:
    return os.environ.get("SHADOW_MODEL_VERSION") or None


def get_canary_traffic_percent() -> float:
    """Percentage of live traffic routed through the canary comparison."""
    try:
        return float(os.environ.get("CANARY_TRAFFIC_PERCENT", "0"))
    except ValueError:
        return 0.0


def get_canary_soak_seconds() -> int:
    """Soak period a candidate must spend in canary before full cutover."""
    try:
        return int(os.environ.get("CANARY_SOAK_SECONDS", "3600"))
    except ValueError:
        return 3600


def get_canary_divergence_threshold() -> float:
    """Divergence above which the canary is automatically rolled back."""
    try:
        return float(os.environ.get("CANARY_DIVERGENCE_THRESHOLD", "0.20"))
    except ValueError:
        return 0.20


def should_route_to_canary() -> bool:
    """Return True if this request should be routed through the canary."""
    percent = get_canary_traffic_percent()
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    return random.random() * 100.0 < percent


def _init_shadow_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                asset_pair TEXT NOT NULL,
                production_score REAL NOT NULL,
                shadow_score REAL NOT NULL,
                divergence REAL NOT NULL,
                shadow_model_version TEXT NOT NULL,
                scored_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS canary_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_version TEXT NOT NULL,
                production_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                total_comparisons INTEGER NOT NULL DEFAULT 0,
                mean_divergence REAL NOT NULL DEFAULT 0,
                alert_rate_divergence REAL NOT NULL DEFAULT 0,
                threshold REAL NOT NULL,
                rollback_reason TEXT
            )
        """)


def store_shadow_score(
    db_path: str,
    wallet: str,
    asset_pair: str,
    production_score: float,
    shadow_score: float,
    shadow_model_version: str,
) -> float:
    """Store shadow score comparison and return divergence."""
    divergence = abs(production_score - shadow_score)

    _init_shadow_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO shadow_scores "
            "(wallet, asset_pair, production_score, shadow_score, divergence, "
            "shadow_model_version, scored_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                wallet,
                asset_pair,
                production_score,
                shadow_score,
                divergence,
                shadow_model_version,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    histogram = _get_histogram()
    if histogram is not None:
        histogram.observe(divergence)
    delta_histogram = _get_delta_histogram()
    if delta_histogram is not None:
        delta_histogram.observe(shadow_score - production_score)

    logger.debug(
        "Shadow score: wallet=%s prod=%.3f shadow=%.3f divergence=%.3f",
        wallet, production_score, shadow_score, divergence,
    )
    return divergence


def load_shadow_models(shadow_version: str, model_dir: str) -> dict:
    """Load shadow model artifacts for the given version."""
    from detection.model_inference import _load_models_base

    shadow_dir = os.path.join(model_dir, f"shadow_{shadow_version}")
    if not os.path.isdir(shadow_dir):
        shadow_dir = model_dir

    return _load_models_base(shadow_dir)


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    """Return a percentile from sorted values using nearest-rank semantics."""
    if not values:
        return 0.0
    index = max(0, math.ceil(len(values) * percentile) - 1)
    return values[min(index, len(values) - 1)]


def get_shadow_report(db_path: str, divergence_threshold: float = 0.20) -> dict:
    """Return shadow scoring report: mean divergence, p95, high-divergence wallets."""
    _init_shadow_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) as n, AVG(divergence) as mean_div FROM shadow_scores"
        ).fetchone()

        n = row["n"]
        mean_div = row["mean_div"] or 0.0

        divergences = conn.execute(
            "SELECT divergence FROM shadow_scores ORDER BY divergence"
        ).fetchall()
        if divergences:
            vals = [r["divergence"] for r in divergences]
            p95_div = _nearest_rank_percentile(vals, 0.95)
        else:
            p95_div = 0.0

        high_divergence_wallets = conn.execute(
            "SELECT wallet, asset_pair, production_score, shadow_score, divergence "
            "FROM shadow_scores WHERE divergence > ? "
            "ORDER BY divergence DESC LIMIT 50",
            (divergence_threshold,),
        ).fetchall()

    return {
        "total_comparisons": n,
        "mean_divergence": round(mean_div, 4),
        "p95_divergence": round(p95_div, 4),
        "high_divergence_wallets": [
            {
                "wallet": r["wallet"],
                "asset_pair": r["asset_pair"],
                "production_score": r["production_score"],
                "shadow_score": r["shadow_score"],
                "divergence": r["divergence"],
            }
            for r in high_divergence_wallets
        ],
    }


def start_canary(
    db_path: str,
    candidate_version: str,
    production_version: str,
    threshold: Optional[float] = None,
) -> int:
    """Begin a canary run and return its id."""
    _init_shadow_table(db_path)
    if threshold is None:
        threshold = get_canary_divergence_threshold()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO canary_runs "
            "(candidate_version, production_version, started_at, status, threshold) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                candidate_version,
                production_version,
                datetime.now(timezone.utc).isoformat(),
                "running",
                threshold,
            ),
        )
        return cursor.lastrowid


def evaluate_canary(
    db_path: str,
    canary_id: int,
    alert_rate_divergence: float = 0.0,
) -> dict:
    """Evaluate a canary run against its divergence threshold.

    Compares score divergence and alert-rate divergence over the soak period.
    If divergence exceeds the configured threshold the canary is automatically
    rolled back and an alert is emitted. Results are recorded on the canary run
    so they can be attached to the model's promotion audit trail.
    """
    _init_shadow_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM canary_runs WHERE id = ?", (canary_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"Unknown canary run: {canary_id}")

        threshold = run["threshold"]
        row = conn.execute(
            "SELECT COUNT(*) as n, AVG(divergence) as mean_div FROM shadow_scores"
        ).fetchone()
        total = row["n"]
        mean_div = row["mean_div"] or 0.0

        exceeded = mean_div > threshold or alert_rate_divergence > threshold
        status = "rolled_back" if exceeded else "passed"
        rollback_reason = None
        if exceeded:
            rollback_reason = (
                f"divergence exceeded threshold {threshold}: "
                f"mean_score_divergence={mean_div:.4f}, "
                f"alert_rate_divergence={alert_rate_divergence:.4f}"
            )

        conn.execute(
            "UPDATE canary_runs SET ended_at = ?, status = ?, "
            "total_comparisons = ?, mean_divergence = ?, "
            "alert_rate_divergence = ?, rollback_reason = ? WHERE id = ?",
            (
                datetime.now(timezone.utc).isoformat(),
                status,
                total,
                mean_div,
                alert_rate_divergence,
                rollback_reason,
                canary_id,
            ),
        )

    if exceeded:
        logger.error(
            "CANARY ROLLBACK: candidate=%s production=%s %s",
            run["candidate_version"], run["production_version"], rollback_reason,
        )

    return {
        "canary_id": canary_id,
        "candidate_version": run["candidate_version"],
        "production_version": run["production_version"],
        "status": status,
        "total_comparisons": total,
        "mean_divergence": round(mean_div, 4),
        "alert_rate_divergence": round(alert_rate_divergence, 4),
        "threshold": threshold,
        "rollback_reason": rollback_reason,
    }


def get_canary_results(db_path: str, canary_id: int) -> Optional[dict]:
    """Return recorded canary results for the promotion audit trail."""
    _init_shadow_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM canary_runs WHERE id = ?", (canary_id,)
        ).fetchone()
    if run is None:
        return None
    return {
        "canary_id": run["id"],
        "candidate_version": run["candidate_version"],
        "production_version": run["production_version"],
        "started_at": run["started_at"],
        "ended_at": run["ended_at"],
        "status": run["status"],
        "total_comparisons": run["total_comparisons"],
        "mean_divergence": run["mean_divergence"],
        "alert_rate_divergence": run["alert_rate_divergence"],
        "threshold": run["threshold"],
        "rollback_reason": run["rollback_reason"],
    }
