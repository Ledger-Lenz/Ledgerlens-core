"""Adversarial feature defense layer for wash-trading detection.

This module implements a lightweight anomaly detector for feature-space
manipulation (e.g. amount camouflage, timing jitter, and graph
fragmentation). It uses an Isolation Forest trained on confirmed-clean
wallet features and a set of hand-crafted consistency rules that flag
internally contradictory feature profiles. The resulting composite score is
used as a conservative risk boost for suspicious wallets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import entropy
from sklearn.ensemble import IsolationForest

ADVERSARIAL_FEATURE_NAMES = [
    "benford_conformity_suspicion",
    "temporal_regularity_score",
    "counterparty_rotation_index",
    "decoy_trade_signature",
    "jitter_fingerprint",
    "evasion_composite_score",
]

# Expected Benford digit probabilities for digits 1-9
_BENFORD_PROBS = np.array([np.log10(1 + 1 / d) for d in range(1, 10)])


class AdversarialAlertType(str, Enum):
    ISOLATION_FOREST = "isolation_forest"
    CONSISTENCY_FAIL = "consistency_fail"
    HIGH_VOLUME_CLEAN = "high_volume_clean"


@dataclass
class AdversarialAlert:
    wallet: str
    alert_type: AdversarialAlertType
    isolation_score: float
    inconsistency_flags: list[str]
    adversarial_feature_score: float
    detected_at: datetime = field(default_factory=datetime.utcnow)


class AdversarialFeatureDetector:
    """Isolation Forest detector over the clean-wallet feature distribution."""

    def __init__(self, contamination: float = 0.05, n_estimators: int = 200, random_state: int = 42):
        self.contamination = contamination
        self.n_estimators = n_estimators
        self.random_state = random_state
        self._forest: Optional[IsolationForest] = None
        self._fitted = False

    def fit(self, clean_feature_matrix: np.ndarray) -> None:
        self._forest = IsolationForest(
            contamination=self.contamination,
            n_estimators=self.n_estimators,
            random_state=self.random_state,
        )
        self._forest.fit(clean_feature_matrix)
        self._fitted = True

    def score(self, feature_vector: np.ndarray) -> float:
        if not self._fitted or self._forest is None:
            return 0.0
        raw = self._forest.score_samples(feature_vector.reshape(1, -1))[0]
        return float(max(0.0, min(1.0, 1.0 + raw)))


CONSISTENCY_RULES = [
    lambda fv: "high_volume_low_counterparty" if (
        fv.get("volume_to_unique_counterparty_ratio", 0.0) > 100
        and fv.get("counterparty_concentration_ratio", 0.0) < 0.05
    ) else None,
    lambda fv: "ring_member_no_round_trips" if (
        fv.get("wash_ring_membership", 0.0) > 0.5
        and fv.get("round_trip_trade_frequency", 0.0) < 0.01
    ) else None,
    lambda fv: "chi_sq_mad_contradiction" if (
        fv.get("chi_sq_24h", 0.0) > 50
        and fv.get("mad_24h", 0.0) < 0.001
    ) else None,
    lambda fv: "new_account_high_centrality" if (
        fv.get("account_age_days", 999.0) < 7
        and fv.get("network_centrality", 0.0) > 0.3
    ) else None,
]


class FeatureConsistencyChecker:
    """Validate internally contradictory feature combinations."""

    def check(self, feature_dict: dict[str, float]) -> list[str]:
        flags = []
        for rule in CONSISTENCY_RULES:
            flag = rule(feature_dict)
            if flag is not None:
                flags.append(flag)
        return flags


def compute_adversarial_feature_score(
    isolation_score: float,
    n_consistency_flags: int,
    base_risk_score: int,
) -> float:
    score = 0.5 * isolation_score
    score += 0.3 * min(n_consistency_flags / 3.0, 1.0)
    if base_risk_score < 30 and isolation_score > 0.7:
        score += 0.2
    return float(max(0.0, min(1.0, score)))


def apply_adversarial_boost(
    base_score: int,
    adversarial_score: float,
    boost_threshold: float = 0.6,
    max_boost: int = 20,
) -> int:
    if adversarial_score < boost_threshold:
        return int(base_score)
    boost = int(max_boost * (adversarial_score - boost_threshold) / (1 - boost_threshold))
    return int(max(0, min(100, base_score + boost)))


def _account_trades(trades: pd.DataFrame, account: str) -> pd.DataFrame:
    return trades[(trades["base_account"] == account) | (trades["counter_account"] == account)]


def _counterparties(account_trades: pd.DataFrame, account: str) -> pd.Series:
    return account_trades.apply(
        lambda r: r["counter_account"] if r["base_account"] == account else r["base_account"],
        axis=1,
    )


def benford_conformity_suspicion(trades: pd.DataFrame, account: str) -> float:
    """KL divergence from a lognormal fit to the account's own amounts.

    High when amounts are *too* Benford-conforming relative to the
    lognormal distribution that natural trades follow — a sign of
    deliberate digit-distribution engineering.

    Returns 0.0 when fewer than 5 trades are available.
    """
    acc_trades = _account_trades(trades, account)
    amounts = acc_trades["base_amount"].dropna().values
    if len(amounts) < 5:
        return 0.0

    # Leading-digit observed distribution
    leading_digits = np.array([int(str(abs(a)).lstrip("0.")[0]) for a in amounts if a > 0])
    if len(leading_digits) == 0:
        return 0.0
    observed = np.bincount(leading_digits, minlength=10)[1:10].astype(float)
    if observed.sum() == 0:
        return 0.0
    observed /= observed.sum()

    # KL divergence from lognormal-based expectation (fitted to the data)
    mu, sigma = np.log(amounts[amounts > 0]).mean(), np.log(amounts[amounts > 0]).std()
    sigma = max(sigma, 1e-6)
    # Approximate lognormal leading-digit distribution via sampling
    sample = np.exp(np.random.default_rng(0).normal(mu, sigma, 5000))
    sample_leading = np.array([int(str(abs(a)).lstrip("0.")[0]) for a in sample if a > 0])
    expected = np.bincount(sample_leading, minlength=10)[1:10].astype(float)
    expected = np.clip(expected / expected.sum(), 1e-9, None)

    # High KL = amounts more Benford-like than lognormal predicts → suspicion
    kl = float(entropy(np.clip(observed, 1e-9, None), expected))
    # Invert: closeness to ideal Benford (low KL vs _BENFORD_PROBS) is suspicious
    benford_kl = float(entropy(np.clip(observed, 1e-9, None), _BENFORD_PROBS))
    # Return how much closer to Benford than to lognormal (clipped to [0, 1])
    result = float(np.clip(1.0 - benford_kl / (kl + 1e-9), 0.0, 1.0))
    return result if np.isfinite(result) else 0.0


def temporal_regularity_score(trades: pd.DataFrame, account: str) -> float:
    """Lag-1 autocorrelation of inter-trade intervals (seconds).

    Bots produce highly regular spacing (autocorrelation near 1);
    human traders produce irregular intervals (autocorrelation near 0
    or negative).  Returns 0.0 for < 3 trades.
    """
    acc_trades = _account_trades(trades, account).sort_values("ledger_close_time")
    if len(acc_trades) < 3:
        return 0.0
    intervals = acc_trades["ledger_close_time"].diff().dt.total_seconds().dropna().values
    if len(intervals) < 2:
        return 0.0
    if intervals.std() == 0:
        return 1.0  # perfectly uniform spacing → maximally bot-like
    autocorr = float(pd.Series(intervals).autocorr(lag=1))
    return float(np.clip((autocorr + 1) / 2, 0.0, 1.0))  # map [-1,1] -> [0,1]


def counterparty_rotation_index(trades: pd.DataFrame, account: str) -> float:
    """Rate of unique counterparty introduction over time.

    Defined as the fraction of time-windows in which at least one *new*
    counterparty appears.  High values indicate deliberate rotation.
    Returns 0.0 when < 2 trades exist.
    """
    acc_trades = _account_trades(trades, account).sort_values("ledger_close_time").reset_index(drop=True)
    if len(acc_trades) < 2:
        return 0.0
    counterparties = _counterparties(acc_trades, account)
    seen: set[str] = set()
    new_counts = 0
    for cp in counterparties:
        if cp not in seen:
            new_counts += 1
            seen.add(cp)
    # Normalise by total trades so high churn → high score
    return float(new_counts / len(acc_trades))


def decoy_trade_signature(trades: pd.DataFrame, account: str) -> float:
    """Fraction of low-value trades immediately preceding high-value round-trips.

    Detects the decoy-trade evasion strategy: small trades inserted before
    large wash pairs.  Threshold: a trade is "low-value" if its amount is
    below the account's 25th percentile; a subsequent trade is
    "high-value" if above the 75th percentile.
    Returns 0.0 for < 4 trades.
    """
    acc_trades = _account_trades(trades, account).sort_values("ledger_close_time").reset_index(drop=True)
    if len(acc_trades) < 4:
        return 0.0
    amounts = acc_trades["base_amount"].values
    low_thresh = np.percentile(amounts, 25)
    high_thresh = np.percentile(amounts, 75)
    if low_thresh >= high_thresh:
        return 0.0
    hits = 0
    for i in range(len(amounts) - 1):
        if amounts[i] <= low_thresh and amounts[i + 1] >= high_thresh:
            hits += 1
    return float(hits / (len(amounts) - 1))


def jitter_fingerprint(trades: pd.DataFrame, account: str) -> float:
    """Lag-1 autocorrelation of ALL inter-trade intervals for the account.

    Unlike ``temporal_regularity_score`` (which measures regularity of
    spacing), this captures whether the jitter itself has a periodic
    structure — bots adding random jitter often draw from a fixed range,
    producing uniformly-spaced *jitter values*.  Returns 0.0 for < 4 trades.
    """
    acc_trades = _account_trades(trades, account).sort_values("ledger_close_time")
    if len(acc_trades) < 4:
        return 0.0
    intervals = acc_trades["ledger_close_time"].diff().dt.total_seconds().dropna().values
    if len(intervals) < 3:
        return 0.0
    # Second-order differences reveal structure in the jitter itself
    jitter = np.diff(intervals)
    if len(jitter) < 2 or jitter.std() == 0:
        return 0.0
    autocorr = float(pd.Series(jitter).autocorr(lag=1))
    return float(np.clip((autocorr + 1) / 2, 0.0, 1.0))


def evasion_composite_score(feature_dict: dict) -> float:
    """Weighted combination of the five evasion signals into a single 0–1 indicator."""
    weights = {
        "benford_conformity_suspicion": 0.20,
        "temporal_regularity_score": 0.25,
        "counterparty_rotation_index": 0.20,
        "decoy_trade_signature": 0.15,
        "jitter_fingerprint": 0.20,
    }
    return float(sum(weights[k] * feature_dict.get(k, 0.0) for k in weights))


def compute_adversarial_features(trades: pd.DataFrame, account: str) -> dict:
    """Compute all adversarial meta-features for ``account``."""
    feats: dict = {
        "benford_conformity_suspicion": benford_conformity_suspicion(trades, account),
        "temporal_regularity_score": temporal_regularity_score(trades, account),
        "counterparty_rotation_index": counterparty_rotation_index(trades, account),
        "decoy_trade_signature": decoy_trade_signature(trades, account),
        "jitter_fingerprint": jitter_fingerprint(trades, account),
    }
    feats["evasion_composite_score"] = evasion_composite_score(feats)
    return feats
