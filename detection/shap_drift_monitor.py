from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import sqlite3
import logging
import numpy as np
from pathlib import Path

from detection.drift_monitor import compute_psi

logger = logging.getLogger("ledgerlens.shap_drift_monitor")

SHAP_DRIFT_PSI_THRESHOLD = 0.20
SHAP_DRIFT_MIN_FLAGGED_FEATURES = 3
SHAP_SNAPSHOT_SAMPLE_RATE = 0.1

_SHAP_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS shap_value_history (
    id INTEGER PRIMARY KEY,
    wallet TEXT,
    asset_pair TEXT,
    model_name TEXT,
    model_version TEXT,
    feature_name TEXT,
    shap_value REAL,
    recorded_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shap_history_feature_version
    ON shap_value_history (feature_name, model_version);
"""


def _init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SHAP_HISTORY_DDL)
    conn.commit()
    conn.close()


def record_shap_snapshot(
    wallet: str,
    asset_pair: str,
    model_name: str,
    model_version: str,
    shap_values: dict[str, float],
    db_path: str | None = None,
) -> None:
    from config.settings import settings
    db_path = db_path or settings.db_path
    _init_db(db_path)

    import random
    if random.random() > SHAP_SNAPSHOT_SAMPLE_RATE:
        return

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    rows = [(wallet, asset_pair, model_name, model_version, fname, float(val), now) for fname, val in shap_values.items()]
    conn.executemany(
        "INSERT INTO shap_value_history (wallet, asset_pair, model_name, model_version, feature_name, shap_value, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _load_shap_distribution(feature_name: str, model_version: str, db_path: str) -> np.ndarray:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT shap_value FROM shap_value_history WHERE feature_name = ? AND model_version = ?",
        (feature_name, model_version),
    ).fetchall()
    conn.close()
    return np.array([r[0] for r in rows], dtype=float)


def compute_shap_psi(feature_name: str, reference_version: str, current_version: str, db_path: str) -> float:
    ref = _load_shap_distribution(feature_name, reference_version, db_path)
    cur = _load_shap_distribution(feature_name, current_version, db_path)
    if len(ref) == 0 or len(cur) == 0:
        return 0.0
    return compute_psi(ref, cur)


@dataclass
class KSResult:
    statistic: float
    p_value: float
    significant: bool


def compute_shap_ks_test(feature_name: str, reference_version: str, current_version: str, db_path: str) -> KSResult:
    from scipy.stats import ks_2samp
    ref = _load_shap_distribution(feature_name, reference_version, db_path)
    cur = _load_shap_distribution(feature_name, current_version, db_path)
    if len(ref) == 0 or len(cur) == 0:
        return KSResult(statistic=0.0, p_value=1.0, significant=False)
    stat, p = ks_2samp(ref, cur)
    return KSResult(statistic=float(stat), p_value=float(p), significant=p < 0.05)


@dataclass
class ShapDriftFinding:
    feature_name: str
    shap_psi: float
    ks_result: KSResult
    input_feature_psi: float
    input_stable_but_shap_drifted: bool
    flagged: bool


@dataclass
class ShapDriftReport:
    model_name: str
    reference_version: str
    current_version: str
    findings: list[ShapDriftFinding]
    explainability_drift_detected: bool
    computed_at: datetime


def compute_shap_drift_report(
    model_name: str,
    reference_version: str,
    current_version: str,
    db_path: str | None = None,
    input_psi_dict: dict[str, float] | None = None,
) -> ShapDriftReport:
    from config.settings import settings
    from detection.feature_engineering import FEATURE_NAMES

    db_path = db_path or settings.db_path
    _init_db(db_path)

    findings: list[ShapDriftFinding] = []
    for feature_name in FEATURE_NAMES:
        shap_psi = compute_shap_psi(feature_name, reference_version, current_version, db_path)
        ks_result = compute_shap_ks_test(feature_name, reference_version, current_version, db_path)
        input_psi = (input_psi_dict or {}).get(feature_name, 0.0)
        input_stable_but_shap_drifted = input_psi < 0.10 and shap_psi >= SHAP_DRIFT_PSI_THRESHOLD
        flagged = shap_psi >= SHAP_DRIFT_PSI_THRESHOLD

        findings.append(ShapDriftFinding(
            feature_name=feature_name,
            shap_psi=shap_psi,
            ks_result=ks_result,
            input_feature_psi=input_psi,
            input_stable_but_shap_drifted=input_stable_but_shap_drifted,
            flagged=flagged,
        ))

    n_flagged = sum(1 for f in findings if f.flagged)
    explainability_drift_detected = n_flagged >= SHAP_DRIFT_MIN_FLAGGED_FEATURES

    return ShapDriftReport(
        model_name=model_name,
        reference_version=reference_version,
        current_version=current_version,
        findings=findings,
        explainability_drift_detected=explainability_drift_detected,
        computed_at=datetime.now(timezone.utc),
    )
