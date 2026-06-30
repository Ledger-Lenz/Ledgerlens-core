"""Shadow model scoring: run a candidate model in parallel with production.

When SHADOW_MODEL_VERSION is set, every scoring request computes both the
production and shadow scores. The shadow score never affects the API response;
instead, score divergence is logged to a Prometheus histogram and stored in
a SQLite table for offline analysis.

This enables data-driven promotion decisions based on real traffic before
committing to a hard model cutover.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional


logger = logging.getLogger("ledgerlens.shadow_scoring")

# Prometheus metric (lazy import to avoid hard dependency)
_shadow_histogram = None


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


def get_shadow_model_version() -> Optional[str]:
    return os.environ.get("SHADOW_MODEL_VERSION") or None


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
            p95_idx = int(len(vals) * 0.95)
            p95_div = vals[min(p95_idx, len(vals) - 1)]
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
