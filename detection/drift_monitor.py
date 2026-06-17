"""Monitor feature distribution drift and trigger retraining when needed.

Implements Population Stability Index (PSI) computation to detect when the
distribution of features in production scoring has shifted significantly from
the training distribution. Persists scored feature vectors to SQLite and
provides drift detection thresholds.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("ledgerlens.drift_monitor")

MAX_SNAPSHOT_ROWS = 500_000
MIN_SNAPSHOT_ROWS_AFTER_PRUNE = 450_000


def _init_db(db_path: str) -> None:
    """Initialize feature_distribution_snapshots table if it doesn't exist."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature_distribution_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            asset_pair TEXT NOT NULL,
            feature_name TEXT NOT NULL,
            feature_value REAL NOT NULL,
            recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feature_recorded_at ON feature_distribution_snapshots(feature_name, recorded_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recorded_at ON feature_distribution_snapshots(recorded_at)"
    )
    conn.commit()
    conn.close()


def record_scored_features(
    feature_vectors: list[dict],
    wallet_ids: list[str] | None = None,
    asset_pairs: list[str] | None = None,
    db_path: str | None = None,
) -> None:
    """Persist a batch of feature vectors to the distribution snapshots table.

    Args:
        feature_vectors: List of feature dicts (keys are feature names, values are floats).
        wallet_ids: Wallet IDs corresponding to each feature vector (optional).
        asset_pairs: Asset pair strings corresponding to each feature vector (optional).
        db_path: SQLite database path. Defaults to settings.db_path.
    """
    from config.settings import settings

    db_path = db_path or settings.db_path
    _init_db(db_path)

    if not feature_vectors:
        return

    wallet_ids = wallet_ids or ["unknown"] * len(feature_vectors)
    asset_pairs = asset_pairs or ["unknown"] * len(feature_vectors)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()
    rows = []

    for fv, wallet, pair in zip(feature_vectors, wallet_ids, asset_pairs):
        for feature_name, feature_value in fv.items():
            if isinstance(feature_value, (int, float)) and not np.isnan(feature_value):
                rows.append((wallet, pair, feature_name, float(feature_value), now))

    cursor.executemany(
        """
        INSERT INTO feature_distribution_snapshots (wallet, asset_pair, feature_name, feature_value, recorded_at)
        VALUES (?, ?, ?, ?, ?)
    """,
        rows,
    )
    conn.commit()

    # Enforce hard row cap: if exceeded, prune oldest rows.
    cursor.execute("SELECT COUNT(*) FROM feature_distribution_snapshots")
    count = cursor.fetchone()[0]

    if count > MAX_SNAPSHOT_ROWS:
        logger.warning(
            "Feature distribution snapshots exceeded hard cap (%d > %d); pruning oldest rows",
            count,
            MAX_SNAPSHOT_ROWS,
        )
        cursor.execute(
            f"""
            DELETE FROM feature_distribution_snapshots
            WHERE recorded_at NOT IN (
                SELECT recorded_at FROM feature_distribution_snapshots
                ORDER BY recorded_at DESC
                LIMIT {MIN_SNAPSHOT_ROWS_AFTER_PRUNE}
            )
        """
        )
        conn.commit()

    conn.close()


def compute_psi(
    training_ref: np.ndarray,
    current: np.ndarray,
    bins: int = 10,
    epsilon: float = 1e-10,
) -> float:
    """Compute Population Stability Index (PSI) between two distributions.

    PSI = sum((current_pct - reference_pct) * ln(current_pct / reference_pct))

    PSI = 0 means identical distributions.
    PSI > 0.20 conventionally signals significant drift.
    PSI > 0.25 is severe drift.

    Args:
        training_ref: Training reference distribution (1D array).
        current: Current production distribution (1D array).
        bins: Number of histogram bins.
        epsilon: Small constant to avoid log(0).

    Returns:
        PSI value (float >= 0).
    """
    # Handle empty or nan-filled arrays
    training_ref = training_ref[~np.isnan(training_ref)]
    current = current[~np.isnan(current)]

    if len(training_ref) == 0 or len(current) == 0:
        return 0.0

    # Compute histogram bins from the combined range to ensure consistent binning
    min_val = min(training_ref.min(), current.min())
    max_val = max(training_ref.max(), current.max())

    if min_val == max_val:
        return 0.0

    bin_edges = np.linspace(min_val, max_val, bins + 1)

    # Compute frequencies (avoid zero-count bins with epsilon)
    ref_counts, _ = np.histogram(training_ref, bins=bin_edges)
    curr_counts, _ = np.histogram(current, bins=bin_edges)

    ref_pct = (ref_counts + epsilon) / (ref_counts.sum() + epsilon * bins)
    curr_pct = (curr_counts + epsilon) / (curr_counts.sum() + epsilon * bins)

    # Compute PSI
    psi = np.sum((curr_pct - ref_pct) * np.log(curr_pct / ref_pct))

    return float(max(0.0, psi))


def run_drift_report(
    training_dataset_path: str,
    db_path: str | None = None,
    days_back: int = 30,
) -> dict[str, float]:
    """Compare training reference distribution with recent scored features.

    Loads the training dataset, computes reference distributions for all features,
    then compares with the last N days of scored features in the database.

    Args:
        training_dataset_path: Path to training CSV with feature columns.
        db_path: SQLite database path. Defaults to settings.db_path.
        days_back: Number of days of production data to compare.

    Returns:
        Dict mapping feature names to PSI values.
    """
    from config.settings import settings
    from detection.feature_engineering import FEATURE_NAMES

    db_path = db_path or settings.db_path
    _init_db(db_path)

    # Load training reference
    try:
        training_df = pd.read_csv(training_dataset_path)
    except FileNotFoundError:
        logger.warning("Training dataset not found at %s; returning empty report", training_dataset_path)
        return {}

    # Load recent scored features from database
    cutoff_time = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    conn = sqlite3.connect(db_path)
    scored_df = pd.read_sql_query(
        """
        SELECT feature_name, feature_value
        FROM feature_distribution_snapshots
        WHERE recorded_at >= ?
        """,
        conn,
        params=(cutoff_time,),
    )
    conn.close()

    if scored_df.empty:
        logger.info("No scored features found in the last %d days", days_back)
        return {}

    # Compute PSI for each feature
    report = {}
    for feature_name in FEATURE_NAMES:
        if feature_name not in training_df.columns:
            logger.warning("Feature %s not in training dataset", feature_name)
            continue

        training_dist = training_df[feature_name].dropna().values
        scored_dist = scored_df[scored_df["feature_name"] == feature_name]["feature_value"].values

        if len(training_dist) == 0 or len(scored_dist) == 0:
            report[feature_name] = 0.0
        else:
            psi = compute_psi(training_dist, scored_dist)
            report[feature_name] = psi

    return report


def is_drift_detected(
    report: dict[str, float],
    psi_threshold: float = 0.20,
    min_drifted_features: int = 3,
) -> bool:
    """Determine if drift is detected based on the drift report.

    Drift is detected if at least `min_drifted_features` features have
    PSI > `psi_threshold`.

    Args:
        report: Dict mapping feature names to PSI values.
        psi_threshold: PSI threshold above which a feature is considered drifted.
        min_drifted_features: Minimum number of drifted features to trigger retraining.

    Returns:
        True if drift is detected, False otherwise.
    """
    drifted_count = sum(1 for psi in report.values() if psi > psi_threshold)
    is_drifted = drifted_count >= min_drifted_features

    if is_drifted:
        logger.info(
            "Drift detected: %d features exceed PSI threshold (%.3f)",
            drifted_count,
            psi_threshold,
        )
    else:
        logger.info(
            "No drift detected: %d features exceed PSI threshold (%.3f)",
            drifted_count,
            psi_threshold,
        )

    return is_drifted
