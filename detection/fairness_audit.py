"""Fairness and Bias Audit Framework Across Wallet Cohorts.

This module implements proxy-cohort fairness metrics for LedgerLens' ensemble
model. Since blockchain wallets are pseudonymous by design and the system never
collects demographic attributes, classic protected-class fairness metrics cannot
be computed directly. Instead, we define **proxy cohorts** derived purely from
existing on-chain observable features (account age, trading volume, network
centrality) and measure whether the model systematically over-flags or
under-flags any cohort.

Proxy cohorts (no new data sources, no demographic inference):
    - **Volume tier**: micro (< 1K), retail (1K–10K), active (10K–100K), whale (100K+)
    - **Account age tier**: new (< 7d), recent (7–30d), established (30–180d), veteran (180d+)
    - **Centrality tier**: isolated (0–0.25), peripheral (0.25–0.5), connected (0.5–0.75), hub (0.75–1.0)

Metrics (standard group-fairness definitions):
    - **Demographic parity gap**: difference in positive-flag rate across cohorts.
    - **Equalised odds gap**: difference in TPR and FPR across cohorts.
    - **Cold-start bias check**: whether wallets younger than a threshold are
      flagged disproportionately.

The audit is gated into the ``retrain-check`` promotion path in ``cli.py``,
alongside the existing AUC-ROC comparison and SHAP-stability gates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("ledgerlens.fairness_audit")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

DEFAULT_DISPARITY_THRESHOLD = 0.15
"""Maximum allowed gap in flag rate / TPR / FPR between cohorts."""

DEFAULT_COLD_START_AGE_DAYS = 7
"""Wallets younger than this (in days) are considered "cold-start"."""

MIN_COHORT_SAMPLES = 30
"""Cohorts with fewer than this many samples are excluded from gap computation."""

# ---------------------------------------------------------------------------
# Cohort bin definitions
# ---------------------------------------------------------------------------

VOLUME_TIER_BINS = [0, 1_000, 10_000, 100_000, float("inf")]
VOLUME_TIER_LABELS = ["micro", "retail", "active", "whale"]

ACCOUNT_AGE_BINS_DAYS = [0, 7, 30, 180, float("inf")]
ACCOUNT_AGE_LABELS = ["new", "recent", "established", "veteran"]

CENTRALITY_TIER_BINS = [0.0, 0.25, 0.5, 0.75, 1.0]
CENTRALITY_TIER_LABELS = ["isolated", "peripheral", "connected", "hub"]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FairnessFinding:
    """A single fairness metric result for one cohort dimension."""

    dimension: str
    """Cohort dimension name (e.g. "volume_tier", "account_age_tier", "centrality_tier")."""

    metric: str
    """Metric name (e.g. "demographic_parity", "equalized_odds_tpr", "cold_start")."""

    cohort_rates: dict[str, float]
    """Per-cohort rates (flag rate, TPR, or FPR)."""

    gap: float
    """Maximum gap between cohorts for this metric."""

    threshold: float
    """Threshold above which the gap is considered significant."""

    flagged: bool
    """Whether this finding exceeds the disparity threshold."""


@dataclass
class FairnessAuditReport:
    """Aggregated fairness audit result for one model version."""

    model_name: str
    model_version: str
    findings: list[FairnessFinding]
    significant_disparity: bool
    computed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "findings": [asdict(f) for f in self.findings],
            "significant_disparity": self.significant_disparity,
            "computed_at": self.computed_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Cohort assignment
# ---------------------------------------------------------------------------


def assign_cohorts(feature_df: pd.DataFrame) -> pd.DataFrame:
    """Add ``volume_tier``, ``account_age_tier``, and ``centrality_tier`` columns.

    Derived purely from existing ``detection.feature_engineering.FEATURE_NAMES``
    columns — no new data source, no demographic inference.

    Parameters
    ----------
    feature_df : pd.DataFrame
        Must contain columns ``account_age_days``, ``network_centrality``, and
        at least one volume-related column (``total_volume`` or any column whose
        name contains ``volume``) for binning.

    Returns
    -------
    pd.DataFrame
        The input DataFrame with three new categorical columns added.
    """
    df = feature_df.copy()

    # Volume tier: look for a volume column
    volume_col = _find_volume_column(df)
    if volume_col is not None:
        df["volume_tier"] = pd.cut(
            df[volume_col].fillna(0).clip(lower=0),
            bins=VOLUME_TIER_BINS,
            labels=VOLUME_TIER_LABELS,
            right=False,
        )
        # Ensure the categorical includes all labels even if some bins are empty
        df["volume_tier"] = df["volume_tier"].cat.add_categories(
            [l for l in VOLUME_TIER_LABELS if l not in df["volume_tier"].cat.categories]
        )
    else:
        # Fall back to a single "unknown" tier when no volume column is found
        df["volume_tier"] = pd.Categorical(["unknown"] * len(df), categories=VOLUME_TIER_LABELS + ["unknown"])

    # Account age tier
    if "account_age_days" in df.columns:
        df["account_age_tier"] = pd.cut(
            df["account_age_days"].fillna(0).clip(lower=0),
            bins=ACCOUNT_AGE_BINS_DAYS,
            labels=ACCOUNT_AGE_LABELS,
            right=False,
        )
        df["account_age_tier"] = df["account_age_tier"].cat.add_categories(
            [l for l in ACCOUNT_AGE_LABELS if l not in df["account_age_tier"].cat.categories]
        )
    else:
        df["account_age_tier"] = pd.Categorical(
            ["unknown"] * len(df), categories=ACCOUNT_AGE_LABELS + ["unknown"]
        )

    # Centrality tier
    if "network_centrality" in df.columns:
        df["centrality_tier"] = pd.cut(
            df["network_centrality"].fillna(0).clip(lower=0, upper=1.0),
            bins=CENTRALITY_TIER_BINS,
            labels=CENTRALITY_TIER_LABELS,
            right=False,
        )
        df["centrality_tier"] = df["centrality_tier"].cat.add_categories(
            [l for l in CENTRALITY_TIER_LABELS if l not in df["centrality_tier"].cat.categories]
        )
    else:
        df["centrality_tier"] = pd.Categorical(
            ["unknown"] * len(df), categories=CENTRALITY_TIER_LABELS + ["unknown"]
        )

    return df


def _find_volume_column(df: pd.DataFrame) -> str | None:
    """Find the best volume column in the DataFrame."""
    # Prefer exact matches
    for col in ["total_volume", "volume", "trade_volume"]:
        if col in df.columns:
            return col
    # Fall back to any column with "volume" in the name
    for col in df.columns:
        if "volume" in col.lower():
            return col
    return None


# ---------------------------------------------------------------------------
# Metric functions
# ---------------------------------------------------------------------------


def demographic_parity_gap(
    scores: np.ndarray,
    cohort_labels: np.ndarray,
    threshold: float = DEFAULT_DISPARITY_THRESHOLD,
    risk_score_threshold: int = 70,
) -> list[FairnessFinding]:
    """Compute demographic parity gap across cohorts.

    For each unique cohort, computes the positive flag rate (proportion of
    scores >= ``risk_score_threshold``). Returns the gap between the highest-
    and lowest-flagged cohort.

    Parameters
    ----------
    scores : np.ndarray
        Model risk scores (0–100 scale or 0–1 probabilities).
    cohort_labels : np.ndarray
        Per-sample cohort assignment (strings).
    threshold : float
        Gap threshold for flagging.
    risk_score_threshold : int
        Score threshold for "positive" classification.

    Returns
    -------
    list[FairnessFinding]
        One finding per cohort dimension (typically just one dimension per call).
    """
    scores = np.asarray(scores)
    cohort_labels = np.asarray(cohort_labels)

    if len(scores) == 0:
        return [
            FairnessFinding(
                dimension="unknown",
                metric="demographic_parity",
                cohort_rates={},
                gap=0.0,
                threshold=threshold,
                flagged=False,
            )
        ]

    # Determine if scores are probabilities (0–1) or risk scores (0–100)
    if scores.max() > 1.0:
        flags = (scores >= risk_score_threshold).astype(float)
    else:
        flags = (scores >= risk_score_threshold / 100.0).astype(float)

    unique_cohorts = np.unique(cohort_labels)
    cohort_rates: dict[str, float] = {}

    for cohort in unique_cohorts:
        mask = cohort_labels == cohort
        n_samples = mask.sum()
        if n_samples < MIN_COHORT_SAMPLES:
            continue
        cohort_rates[str(cohort)] = float(flags[mask].mean())

    if not cohort_rates:
        return [
            FairnessFinding(
                dimension="unknown",
                metric="demographic_parity",
                cohort_rates={},
                gap=0.0,
                threshold=threshold,
                flagged=False,
            )
        ]

    gap = max(cohort_rates.values()) - min(cohort_rates.values())
    flagged = gap > threshold

    return [
        FairnessFinding(
            dimension="aggregate",
            metric="demographic_parity",
            cohort_rates=cohort_rates,
            gap=round(gap, 4),
            threshold=threshold,
            flagged=flagged,
        )
    ]


def equalized_odds_gap(
    scores: np.ndarray,
    y_true: np.ndarray,
    cohort_labels: np.ndarray,
    threshold: float = DEFAULT_DISPARITY_THRESHOLD,
    risk_score_threshold: int = 70,
) -> list[FairnessFinding]:
    """Compute equalised odds gaps (TPR and FPR) across cohorts.

    For each unique cohort, computes the true-positive rate (TPR) and
    false-positive rate (FPR). Returns one finding per rate type.

    Parameters
    ----------
    scores : np.ndarray
        Model risk scores (0–100 scale or 0–1 probabilities).
    y_true : np.ndarray
        Ground-truth labels (0 = clean, 1 = wash-trading).
    cohort_labels : np.ndarray
        Per-sample cohort assignment (strings).
    threshold : float
        Gap threshold for flagging.
    risk_score_threshold : int
        Score threshold for "positive" classification.

    Returns
    -------
    list[FairnessFinding]
        Up to two findings: one for TPR gap, one for FPR gap.
    """
    scores = np.asarray(scores)
    y_true = np.asarray(y_true).astype(int)
    cohort_labels = np.asarray(cohort_labels)

    if len(scores) == 0:
        return [
            FairnessFinding(
                dimension="unknown",
                metric="equalized_odds_tpr",
                cohort_rates={},
                gap=0.0,
                threshold=threshold,
                flagged=False,
            ),
            FairnessFinding(
                dimension="unknown",
                metric="equalized_odds_fpr",
                cohort_rates={},
                gap=0.0,
                threshold=threshold,
                flagged=False,
            ),
        ]

    # Determine if scores are probabilities (0–1) or risk scores (0–100)
    if scores.max() > 1.0:
        predictions = (scores >= risk_score_threshold).astype(int)
    else:
        predictions = (scores >= risk_score_threshold / 100.0).astype(int)

    unique_cohorts = np.unique(cohort_labels)
    tprs: dict[str, float] = {}
    fprs: dict[str, float] = {}

    for cohort in unique_cohorts:
        mask = cohort_labels == cohort
        n_samples = mask.sum()
        if n_samples < MIN_COHORT_SAMPLES:
            continue

        y_cohort = y_true[mask]
        pred_cohort = predictions[mask]

        # TPR = TP / (TP + FN) — among actual positives
        pos_mask = y_cohort == 1
        if pos_mask.sum() > 0:
            tprs[str(cohort)] = float((pred_cohort[pos_mask] == 1).mean())
        else:
            tprs[str(cohort)] = 0.0

        # FPR = FP / (FP + TN) — among actual negatives
        neg_mask = y_cohort == 0
        if neg_mask.sum() > 0:
            fprs[str(cohort)] = float((pred_cohort[neg_mask] == 1).mean())
        else:
            fprs[str(cohort)] = 0.0

    findings: list[FairnessFinding] = []

    if tprs:
        tpr_gap = max(tprs.values()) - min(tprs.values())
        findings.append(
            FairnessFinding(
                dimension="aggregate",
                metric="equalized_odds_tpr",
                cohort_rates=tprs,
                gap=round(tpr_gap, 4),
                threshold=threshold,
                flagged=tpr_gap > threshold,
            )
        )

    if fprs:
        fpr_gap = max(fprs.values()) - min(fprs.values())
        findings.append(
            FairnessFinding(
                dimension="aggregate",
                metric="equalized_odds_fpr",
                cohort_rates=fprs,
                gap=round(fpr_gap, 4),
                threshold=threshold,
                flagged=fpr_gap > threshold,
            )
        )

    return findings


def cold_start_bias_check(
    scores: np.ndarray,
    account_age_days: np.ndarray,
    age_threshold_days: int = DEFAULT_COLD_START_AGE_DAYS,
    threshold: float = DEFAULT_DISPARITY_THRESHOLD,
    risk_score_threshold: int = 70,
) -> FairnessFinding:
    """Check for cold-start bias against young wallets.

    Compares the flag rate of wallets younger than ``age_threshold_days``
    against the flag rate of wallets at or above that age.

    Parameters
    ----------
    scores : np.ndarray
        Model risk scores (0–100 scale or 0–1 probabilities).
    account_age_days : np.ndarray
        Per-sample account age in days.
    age_threshold_days : int
        Age below which a wallet is considered "cold-start".
    threshold : float
        Gap threshold for flagging.
    risk_score_threshold : int
        Score threshold for "positive" classification.

    Returns
    -------
    FairnessFinding
        Result of the cold-start bias check.
    """
    scores = np.asarray(scores)
    account_age_days = np.asarray(account_age_days)

    if len(scores) == 0:
        return FairnessFinding(
            dimension="account_age_tier",
            metric="cold_start",
            cohort_rates={},
            gap=0.0,
            threshold=threshold,
            flagged=False,
        )

    # Determine if scores are probabilities (0–1) or risk scores (0–100)
    if scores.max() > 1.0:
        flags = (scores >= risk_score_threshold).astype(float)
    else:
        flags = (scores >= risk_score_threshold / 100.0).astype(float)

    young_mask = account_age_days < age_threshold_days
    mature_mask = ~young_mask

    if young_mask.sum() < MIN_COHORT_SAMPLES:
        logger.info(
            "Cold-start check: only %d young wallets (< %d days), minimum %d required. Skipping.",
            young_mask.sum(),
            age_threshold_days,
            MIN_COHORT_SAMPLES,
        )
        return FairnessFinding(
            dimension="account_age_tier",
            metric="cold_start",
            cohort_rates={"young (<%dd)" % age_threshold_days: 0.0, "mature (>=%dd)" % age_threshold_days: 0.0},
            gap=0.0,
            threshold=threshold,
            flagged=False,
        )

    young_rate = float(flags[young_mask].mean()) if young_mask.sum() > 0 else 0.0
    mature_rate = float(flags[mature_mask].mean()) if mature_mask.sum() > 0 else 0.0

    gap = young_rate - mature_rate
    flagged = gap > threshold

    return FairnessFinding(
        dimension="account_age_tier",
        metric="cold_start",
        cohort_rates={
            f"young (<{age_threshold_days}d)": round(young_rate, 4),
            f"mature (>={age_threshold_days}d)": round(mature_rate, 4),
        },
        gap=round(gap, 4),
        threshold=threshold,
        flagged=flagged,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_fairness_audit(
    scores: np.ndarray,
    y_true: np.ndarray | None,
    feature_df: pd.DataFrame,
    model_name: str = "ensemble",
    model_version: str = "unknown",
    disparity_threshold: float = DEFAULT_DISPARITY_THRESHOLD,
    cold_start_age_days: int = DEFAULT_COLD_START_AGE_DAYS,
    risk_score_threshold: int = 70,
) -> FairnessAuditReport:
    """Run the full fairness audit across all proxy-cohort dimensions.

    Parameters
    ----------
    scores : np.ndarray
        Model risk scores for each sample.
    y_true : np.ndarray or None
        Ground-truth labels (required for equalised odds, optional otherwise).
    feature_df : pd.DataFrame
        Feature DataFrame used to derive cohort assignments. Must contain
        ``account_age_days`` and/or ``network_centrality`` and a volume column.
    model_name : str
        Name of the model being audited.
    model_version : str
        Version string of the model being audited.
    disparity_threshold : float
        Maximum allowed gap for flagging a disparity.
    cold_start_age_days : int
        Age threshold for cold-start bias check.
    risk_score_threshold : int
        Score threshold for "positive" classification.

    Returns
    -------
    FairnessAuditReport
        Aggregated audit results.
    """
    # Assign cohorts from feature DataFrame
    cohort_df = assign_cohorts(feature_df)

    findings: list[FairnessFinding] = []

    # ── Demographic parity (flag rate parity) for each dimension ───────────

    for dim, col in [
        ("volume_tier", "volume_tier"),
        ("account_age_tier", "account_age_tier"),
        ("centrality_tier", "centrality_tier"),
    ]:
        if col in cohort_df.columns:
            cohort_labels = cohort_df[col].values.astype(str)
            dp_findings = demographic_parity_gap(
                scores, cohort_labels, threshold=disparity_threshold,
                risk_score_threshold=risk_score_threshold,
            )
            for f in dp_findings:
                f.dimension = dim
            findings.extend(dp_findings)

    # ── Equalised odds (TPR / FPR parity) for each dimension ──────────────

    if y_true is not None:
        for dim, col in [
            ("volume_tier", "volume_tier"),
            ("account_age_tier", "account_age_tier"),
            ("centrality_tier", "centrality_tier"),
        ]:
            if col in cohort_df.columns:
                cohort_labels = cohort_df[col].values.astype(str)
                eo_findings = equalized_odds_gap(
                    scores, y_true, cohort_labels, threshold=disparity_threshold,
                    risk_score_threshold=risk_score_threshold,
                )
                for f in eo_findings:
                    f.dimension = dim
                findings.extend(eo_findings)

    # ── Cold-start bias check ─────────────────────────────────────────────

    if "account_age_days" in cohort_df.columns:
        cs_finding = cold_start_bias_check(
            scores,
            cohort_df["account_age_days"].values,
            age_threshold_days=cold_start_age_days,
            threshold=disparity_threshold,
            risk_score_threshold=risk_score_threshold,
        )
        findings.append(cs_finding)

    # ── Aggregate ─────────────────────────────────────────────────────────

    significant_disparity = any(f.flagged for f in findings)

    return FairnessAuditReport(
        model_name=model_name,
        model_version=model_version,
        findings=findings,
        significant_disparity=significant_disparity,
        computed_at=datetime.now(timezone.utc),
    )
