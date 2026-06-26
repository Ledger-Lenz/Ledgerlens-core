"""Monitor feature distribution drift and trigger retraining when needed.

Implements Population Stability Index (PSI) computation to detect when the
distribution of features in production scoring has shifted significantly from
the training distribution. Persists scored feature vectors to SQLite and
provides drift detection thresholds.

Per-feature PSI tracking: :func:`compute_psi_for_feature` computes PSI for a
single feature against a training reference distribution.
:func:`compute_per_feature_psi` returns a ``{feature_name: psi}`` dict for
all features. :func:`record_psi_snapshot` persists per-feature PSI values
to the ``feature_psi_history`` table. :func:`check_psi_and_alert` fires a
``feature_drift`` alert when 3+ features exceed PSI > 0.20 simultaneously.
:func:`export_psi_heatmap` renders a colour-coded (feature x date) heatmap.

Performance monitoring (Issue-110): :class:`PerformanceMonitor` collects
ground-truth analyst labels and computes rolling precision/recall/F1 to detect
model degradation over time. When F1 drops more than 5 percentage points from
the training baseline, ``ModelDegradationAlert`` is raised and retraining is
triggered automatically.

Feedback collection loop architecture:
  1. Analyst submits label via ``POST /performance/feedback``
  2. Label written to ``feedback_labels`` table via :meth:`PerformanceMonitor.record_feedback`
  3. ``cli.py retrain-check`` calls :meth:`PerformanceMonitor.check_degradation`
  4. Degradation triggers retraining and fires a webhook ``model_degradation`` event
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import numpy as np
import pandas as pd

logger = logging.getLogger("ledgerlens.drift_monitor")

PSI_THRESHOLD: float = 0.20
PSI_MIN_DRIFTED_FEATURES: int = 3
PSI_ALERT_COOLDOWN_HOURS: int = 24

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

    scored_names = set(scored_df["feature_name"].unique())
    feature_names = [
        f for f in FEATURE_NAMES if f in training_df.columns and f in scored_names
    ]

    # Compute PSI for each feature
    report = {}
    for feature_name in feature_names:
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


# ---------------------------------------------------------------------------
# Per-feature PSI tracking
# ---------------------------------------------------------------------------

_FEATURE_PSI_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS feature_psi_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_name TEXT NOT NULL,
    psi_value REAL NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 30,
    n_reference_samples INTEGER,
    n_current_samples INTEGER,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_psi_history_feature ON feature_psi_history(feature_name, computed_at);
"""


def _init_psi_tables(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_FEATURE_PSI_HISTORY_DDL)
    try:
        conn.execute("ALTER TABLE degradation_alerts ADD COLUMN alert_type TEXT DEFAULT 'model_degradation'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def compute_psi_for_feature(
    reference: np.ndarray,
    current: np.ndarray,
    n_bins: int = 10,
    epsilon: float = 1e-6,
) -> float:
    """Compute PSI between reference and current distributions for a single feature."""
    reference = reference[~np.isnan(reference)]
    current = current[~np.isnan(current)]

    if len(reference) == 0 or len(current) == 0:
        return 0.0

    percentile_bins = np.percentile(reference, np.linspace(0, 100, n_bins + 1))
    percentile_bins = np.unique(percentile_bins)
    if len(percentile_bins) < 3:
        return 0.0

    ref_counts, _ = np.histogram(reference, bins=percentile_bins)
    cur_counts, _ = np.histogram(current, bins=percentile_bins)

    ref_pct = ref_counts / (len(reference) + epsilon)
    cur_pct = cur_counts / (len(current) + epsilon)

    ref_pct = np.clip(ref_pct, epsilon, None)
    cur_pct = np.clip(cur_pct, epsilon, None)

    psi = np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct))
    return float(psi)


def compute_per_feature_psi(
    training_dataset_path: str,
    db_path: str | None = None,
    window_days: int = 30,
    n_bins: int = 10,
) -> dict[str, float]:
    """Compute PSI for each feature comparing training reference to recent production data."""
    from config.settings import settings
    from detection.feature_engineering import FEATURE_NAMES

    db_path = db_path or settings.db_path
    _init_db(db_path)

    try:
        training_df = pd.read_csv(training_dataset_path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Training reference file not found: {training_dataset_path}. "
            "Run 'cli.py train' to generate it."
        )

    cutoff_time = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    conn = sqlite3.connect(db_path)
    scored_df = pd.read_sql_query(
        "SELECT feature_name, feature_value FROM feature_distribution_snapshots WHERE recorded_at >= ?",
        conn,
        params=(cutoff_time,),
    )
    conn.close()

    if scored_df.empty:
        logger.warning("No scored features found in the last %d days; returning all-zero PSI", window_days)
        return {f: 0.0 for f in FEATURE_NAMES if f in training_df.columns}

    scored_names = set(scored_df["feature_name"].unique())
    feature_names = [f for f in FEATURE_NAMES if f in training_df.columns]

    psi_dict: dict[str, float] = {}
    for feature_name in feature_names:
        training_dist = training_df[feature_name].dropna().values.astype(float)
        if feature_name in scored_names:
            current_dist = scored_df[scored_df["feature_name"] == feature_name]["feature_value"].values.astype(float)
        else:
            current_dist = np.array([])

        if len(training_dist) == 0 or len(current_dist) == 0:
            psi_dict[feature_name] = 0.0
        else:
            psi_dict[feature_name] = compute_psi_for_feature(training_dist, current_dist, n_bins=n_bins)

    return psi_dict


def record_psi_snapshot(
    psi_dict: dict[str, float],
    window_days: int = 30,
    n_reference_samples: int | None = None,
    n_current_samples: int | None = None,
    db_path: str | None = None,
) -> None:
    """Persist per-feature PSI values to the feature_psi_history table."""
    from config.settings import settings

    db_path = db_path or settings.db_path
    _init_psi_tables(db_path)

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    rows = [
        (fname, psi_val, window_days, n_reference_samples, n_current_samples, now)
        for fname, psi_val in psi_dict.items()
    ]
    conn.executemany(
        """INSERT INTO feature_psi_history
           (feature_name, psi_value, window_days, n_reference_samples, n_current_samples, computed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()
    logger.info("Recorded PSI snapshot for %d features", len(rows))


def check_psi_and_alert(
    psi_dict: dict[str, float],
    psi_threshold: float = PSI_THRESHOLD,
    min_drifted_features: int = PSI_MIN_DRIFTED_FEATURES,
    cooldown_hours: int = PSI_ALERT_COOLDOWN_HOURS,
    db_path: str | None = None,
) -> bool:
    """Check per-feature PSI and fire a feature_drift alert if thresholds exceeded.

    Returns True if an alert was triggered.
    """
    from config.settings import settings

    db_path = db_path or settings.db_path

    drifted = {fname: psi for fname, psi in psi_dict.items() if psi > psi_threshold}
    n_drifted = len(drifted)

    if n_drifted < min_drifted_features:
        logger.info(
            "PSI alert check: %d features drifted (threshold %d); no alert",
            n_drifted, min_drifted_features,
        )
        return False

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_FEATURE_PSI_HISTORY_DDL)
    except Exception:
        pass

    cooldown_cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM degradation_alerts WHERE alert_type = 'feature_drift' AND alert_timestamp > ?",
        (cooldown_cutoff,),
    ).fetchone()

    if row and row[0] > 0:
        logger.info("PSI alert suppressed: cooldown period (%dh) not elapsed", cooldown_hours)
        conn.close()
        return False

    now = datetime.now(timezone.utc).isoformat()
    affected_features = sorted(drifted.keys())
    conn.execute(
        """INSERT INTO degradation_alerts
           (alert_timestamp, alert_type, baseline_f1, current_f1, f1_drop,
            precision_current, recall_current, n_feedback_samples, model_version, retrain_triggered)
           VALUES (?, 'feature_drift', NULL, NULL, NULL, NULL, NULL, ?, NULL, 0)""",
        (now, n_drifted),
    )
    conn.commit()
    conn.close()

    logger.warning(
        "Feature drift alert: %d features exceed PSI %.2f: %s",
        n_drifted, psi_threshold, affected_features,
    )

    try:
        from detection.webhook_registry import get_matching_subscribers
        from detection.webhook_queue import enqueue

        event_payload = {
            "event_type": "feature_drift",
            "n_drifted": n_drifted,
            "drifted_features": affected_features,
            "psi_values": {f: round(v, 4) for f, v in drifted.items()},
            "timestamp": now,
        }
        for sub in get_matching_subscribers(None):
            enqueue(sub.subscriber_id, event_payload)
    except Exception as exc:
        logger.debug("Webhook dispatch for feature_drift skipped: %s", exc)

    return True


def load_psi_history(days_back: int = 90, db_path: str | None = None) -> pd.DataFrame:
    """Load PSI history from the database for heatmap generation."""
    from config.settings import settings

    db_path = db_path or settings.db_path
    _init_psi_tables(db_path)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        """SELECT feature_name, psi_value, computed_at
           FROM feature_psi_history
           WHERE computed_at >= ?
           ORDER BY computed_at""",
        conn,
        params=(cutoff,),
    )
    conn.close()

    if not df.empty:
        df["computed_at_date"] = pd.to_datetime(df["computed_at"]).dt.date.astype(str)

    return df


def export_psi_heatmap(output_path: Path, days_back: int = 90, db_path: str | None = None) -> Path:
    """Generate a (n_features x n_dates) heatmap of PSI values as a PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    df = load_psi_history(days_back=days_back, db_path=db_path)
    if df.empty:
        logger.warning("No PSI history available for heatmap; creating empty plot")
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.set_title(f"Feature PSI Heatmap (last {days_back} days) — No Data")
        plt.tight_layout()
        plt.savefig(str(output_path), dpi=150, bbox_inches="tight")
        plt.close()
        return output_path

    pivot = df.pivot_table(
        index="feature_name", columns="computed_at_date", values="psi_value", aggfunc="mean",
    )
    pivot = pivot.fillna(0.0)

    fig, ax = plt.subplots(
        figsize=(max(8, len(pivot.columns) * 0.5), max(6, len(pivot.index) * 0.3))
    )
    cmap = LinearSegmentedColormap.from_list("psi", ["white", "yellow", "orange", "red"], N=256)

    im = ax.imshow(pivot.values, aspect="auto", cmap=cmap, vmin=0.0, vmax=0.30, interpolation="nearest")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=7)
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, rotation=45, ha="right", fontsize=7)
    fig.colorbar(im, ax=ax, label="PSI")
    ax.set_title(f"Feature PSI Heatmap (last {days_back} days)")
    plt.tight_layout()
    plt.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close()
    return output_path


# ---------------------------------------------------------------------------
# Performance monitoring — Issue-110
# ---------------------------------------------------------------------------

_VALID_EVIDENCE_URL_SCHEMES = frozenset({"https"})
_MAX_EVIDENCE_URL_LENGTH = 500


def _validate_evidence_url(url: str) -> None:
    """Raise ValueError for non-HTTPS or oversized evidence URLs (SSRF guard)."""
    if len(url) > _MAX_EVIDENCE_URL_LENGTH:
        raise ValueError(f"evidence_url exceeds {_MAX_EVIDENCE_URL_LENGTH} characters")
    parsed = urlparse(url)
    if parsed.scheme not in _VALID_EVIDENCE_URL_SCHEMES:
        raise ValueError(
            f"evidence_url must use HTTPS; got scheme '{parsed.scheme}'"
        )


class ModelDegradationAlert(Exception):
    """Raised when F1 drops more than the configured threshold from the baseline."""


@dataclass
class PerformanceReport:
    """Snapshot of model performance on analyst-labelled feedback samples."""

    precision: float
    recall: float
    f1: float
    n_samples: int
    n_positive_labels: int
    n_negative_labels: int
    window_days: int
    computed_at: datetime
    degradation_detected: bool
    f1_drop: Optional[float]


_FEEDBACK_LABELS_DDL = """
CREATE TABLE IF NOT EXISTS feedback_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    asset_pair TEXT NOT NULL,
    predicted_score INTEGER NOT NULL,
    true_label INTEGER NOT NULL CHECK(true_label IN (0, 1)),
    submitted_by TEXT,
    evidence_url TEXT,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_recorded_at
    ON feedback_labels(recorded_at);
"""

_DEGRADATION_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS degradation_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    baseline_f1 REAL,
    current_f1 REAL,
    f1_drop REAL,
    precision_current REAL,
    recall_current REAL,
    n_feedback_samples INTEGER,
    model_version TEXT,
    retrain_triggered INTEGER DEFAULT 0
);
"""


class PerformanceMonitor:
    """Collect analyst feedback and detect model performance degradation.

    Records ground-truth labels from human analysts, computes rolling
    precision / recall / F1 against those labels, and raises
    :class:`ModelDegradationAlert` when F1 drops by more than
    ``f1_threshold_drop`` from the training baseline.

    The baseline F1 is read from ``models/training_metadata.json``
    (key: ``val_f1_score``).

    Args:
        db_path: SQLite database path. Defaults to ``settings.db_path``.
        risk_score_threshold: Binary classification threshold (default 70).
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        risk_score_threshold: int = 70,
    ) -> None:
        from config.settings import settings as _settings

        self.db_path = db_path or _settings.db_path
        self.risk_score_threshold = risk_score_threshold
        self._init_tables()

    def _init_tables(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_FEEDBACK_LABELS_DDL)
        conn.executescript(_DEGRADATION_ALERTS_DDL)
        conn.commit()
        conn.close()

    def record_feedback(
        self,
        wallet: str,
        asset_pair: str,
        predicted_score: int,
        true_label: int,
        submitted_by: Optional[str] = "local_api",
        evidence_url: Optional[str] = None,
        score_version: Optional[str] = None,
    ) -> int:
        """Insert an analyst feedback label into ``feedback_labels``.

        Args:
            wallet: Stellar wallet address.
            asset_pair: Asset pair string (e.g. ``"XLM/USDC"``).
            predicted_score: 0-100 risk score produced by the model.
            true_label: Analyst ground-truth (0 = clean, 1 = wash).
            submitted_by: Analyst ID or ``"local_api"``; never user-supplied.
            evidence_url: Optional HTTPS URL to supporting evidence.
            score_version: Model version string that produced the score.

        Returns:
            Newly inserted row ID.

        Raises:
            ValueError: if ``true_label`` not in {0, 1} or evidence_url is invalid.
        """
        if true_label not in (0, 1):
            raise ValueError(f"true_label must be 0 or 1, got {true_label!r}")
        if evidence_url is not None:
            _validate_evidence_url(evidence_url)

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """
            INSERT INTO feedback_labels
                (wallet, asset_pair, predicted_score, true_label,
                 submitted_by, evidence_url, recorded_at, score_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, asset_pair, predicted_score, true_label,
             submitted_by, evidence_url, now, score_version),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id

    def compute_performance_metrics(self, days: int = 30) -> PerformanceReport:
        """Compute precision / recall / F1 on feedback labels from the last ``days`` days.

        Uses :attr:`risk_score_threshold` to binarise ``predicted_score``.

        Returns:
            :class:`PerformanceReport` with computed metrics.
        """
        from config.settings import settings as _settings

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT predicted_score, true_label FROM feedback_labels WHERE recorded_at >= ?",
            (cutoff,),
        ).fetchall()
        conn.close()

        n = len(rows)
        n_positive = sum(1 for _, lbl in rows if lbl == 1)
        n_negative = n - n_positive
        threshold = self.risk_score_threshold

        min_samples = _settings.performance_min_feedback_samples
        degradation_detected = False
        f1_drop = None

        if n < min_samples:
            logger.warning(
                "Only %d feedback samples available (< %d minimum); "
                "skipping degradation alert check",
                n, min_samples,
            )
            return PerformanceReport(
                precision=0.0, recall=0.0, f1=0.0,
                n_samples=n, n_positive_labels=n_positive, n_negative_labels=n_negative,
                window_days=days, computed_at=datetime.now(timezone.utc),
                degradation_detected=False, f1_drop=None,
            )

        tp = sum(1 for score, lbl in rows if score >= threshold and lbl == 1)
        fp = sum(1 for score, lbl in rows if score >= threshold and lbl == 0)
        fn = sum(1 for score, lbl in rows if score < threshold and lbl == 1)

        if tp + fp == 0:
            precision = 1.0
        else:
            precision = tp / (tp + fp)

        if tp + fn == 0:
            recall = 0.0
        else:
            recall = tp / (tp + fn)

        if precision + recall == 0:
            logger.warning("No positive predictions in feedback window; F1 = 0.0")
            f1 = 0.0
        else:
            f1 = 2.0 * precision * recall / (precision + recall)

        return PerformanceReport(
            precision=precision, recall=recall, f1=f1,
            n_samples=n, n_positive_labels=n_positive, n_negative_labels=n_negative,
            window_days=days, computed_at=datetime.now(timezone.utc),
            degradation_detected=degradation_detected, f1_drop=f1_drop,
        )

    def check_degradation(
        self,
        baseline_f1: float,
        f1_threshold_drop: float = 0.05,
        days: Optional[int] = None,
    ) -> bool:
        """Compute current F1 and raise :class:`ModelDegradationAlert` if degraded.

        Args:
            baseline_f1: F1 score recorded at training time.
            f1_threshold_drop: Alert if current_f1 < baseline_f1 - this value.
            days: Rolling window in days (defaults to settings value).

        Returns:
            True if degradation detected, False otherwise.

        Raises:
            ModelDegradationAlert: when degradation exceeds the threshold.
        """
        from config.settings import settings as _settings

        window = days or _settings.performance_monitoring_window_days
        report = self.compute_performance_metrics(days=window)

        if report.n_samples < _settings.performance_min_feedback_samples:
            return False

        f1_drop = baseline_f1 - report.f1
        if f1_drop > f1_threshold_drop:
            self._save_degradation_alert(baseline_f1, report, f1_drop)
            raise ModelDegradationAlert(
                f"F1 degraded by {f1_drop:.4f} "
                f"(baseline={baseline_f1:.4f}, current={report.f1:.4f})"
            )

        logger.info(
            "Performance check: F1=%.4f, baseline=%.4f, drop=%.4f (threshold=%.4f) — OK",
            report.f1, baseline_f1, f1_drop, f1_threshold_drop,
        )
        return False

    def _save_degradation_alert(
        self,
        baseline_f1: float,
        report: PerformanceReport,
        f1_drop: float,
        model_version: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO degradation_alerts
                (alert_timestamp, baseline_f1, current_f1, f1_drop,
                 precision_current, recall_current, n_feedback_samples,
                 model_version, retrain_triggered)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (now, baseline_f1, report.f1, f1_drop,
             report.precision, report.recall, report.n_samples, model_version),
        )
        conn.commit()
        conn.close()
        logger.warning(
            "ModelDegradationAlert saved: baseline_f1=%.4f current_f1=%.4f drop=%.4f",
            baseline_f1, report.f1, f1_drop,
        )

    def get_latest_degradation_alerts(self, limit: int = 10) -> list[dict]:
        """Return the most recent degradation alert records."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """
            SELECT id, alert_timestamp, baseline_f1, current_f1, f1_drop,
                   precision_current, recall_current, n_feedback_samples,
                   model_version, retrain_triggered
            FROM degradation_alerts
            ORDER BY alert_timestamp DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        return [
            {
                "id": r[0], "alert_timestamp": r[1], "baseline_f1": r[2],
                "current_f1": r[3], "f1_drop": r[4], "precision_current": r[5],
                "recall_current": r[6], "n_feedback_samples": r[7],
                "model_version": r[8], "retrain_triggered": bool(r[9]),
            }
            for r in rows
        ]
