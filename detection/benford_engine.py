"""Benford's Law digit-distribution analysis for transaction amounts.

Computes the chi-square statistic, per-digit Z-scores, and Mean Absolute
Deviation (MAD) of the leading-digit distribution of a set of amounts,
relative to the theoretical Benford distribution.

Stratification
--------------
``stratified_benford_analysis()`` groups trades by canonical asset pair
(lexicographically ordered, e.g. ``USDC/XLM`` not ``XLM/USDC``) and computes
Benford statistics independently per stratum. Strata with fewer than 30
observations are marked ``valid=False`` (reason ``"insufficient_sample"``).
When *all* strata fall below the minimum, the engine falls back to a global
(unstratified) computation and sets ``fallback_global=True`` on the summary.

The univariate helpers (`compute_benford_metrics` etc.) score a single
`(wallet, asset_pair)` stream. A coordinated wash-trading syndicate can keep
each individual pair close to Benford while the *joint* cross-pair behaviour is
statistically impossible under independent trading. The multivariate helpers
(`joint_digit_matrix`, `benford_copula_statistic`, `cross_pair_sync_score`,
`multivariate_benford_score`) surface that coordination signal.

For wallets with fewer than BENFORD_BOOTSTRAP_THRESHOLD transactions the
asymptotic chi-square p-value is unreliable (the approximation requires
N * p_i >= 5 for every digit class). In that regime, bootstrap_chi_square_pvalue
generates 10,000 multinomial samples from the true Benford distribution and
derives an empirical p-value directly, eliminating false positives caused by
approximation breakdown at small N.
"""

from __future__ import annotations

import functools
import logging
import math
import os
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional

import numpy as np
import pandas as pd
from scipy.stats import chi2, norm

logger = logging.getLogger("ledgerlens.benford_engine")

DIGITS = list(range(1, 10))


def _parse_benford_windows(default: list[int]) -> list[int]:
    raw = os.getenv("BENFORD_WINDOWS", "")
    if not raw:
        return default
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default


@dataclass
class BenfordStats:
    chi_square: float
    mad: float
    z_scores: list[float]  # length-9, index 0 = digit 1
    n: int


class BenfordStreamCounter:
    """O(1) incremental Benford digit-frequency counter over configurable sliding windows.

    Each window uses a circular buffer of leading digits (``deque(maxlen=window)``).
    When a new digit enters a full window, the oldest digit is automatically evicted
    and the per-digit tally is updated in O(1).  ``window_stats()`` iterates over
    the 9-element tally in O(9) = O(1).

    Circular buffer rollover: Python's ``collections.deque(maxlen=N)`` handles the
    ring automatically — appending to a full deque evicts the leftmost element and
    returns it via the ``appendleft``/``append`` mechanics, but we need the evicted
    value to decrement the tally.  We store the raw deque and maintain a parallel
    ``counts`` array; on each ``update`` we read ``buf[0]`` before appending when
    the buffer is already full, then decrement that evicted digit.
    """

    BENFORD_EXPECTED = [math.log10(1 + 1 / d) for d in range(1, 10)]

    def __init__(self, windows: list[int] | None = None) -> None:
        self._windows: list[int] = _parse_benford_windows(windows or [100, 500, 1000])
        # per-window: circular buffer of raw digits (1-9), digit count array
        self._bufs: list[deque] = [deque(maxlen=w) for w in self._windows]
        self._counts: list[list[int]] = [[0] * 9 for _ in self._windows]
        # pre-built list of (buf, counts, maxlen) to avoid attribute lookups in update()
        self._slots: list[tuple[deque, list[int], int]] = [
            (buf, counts, w)
            for buf, counts, w in zip(self._bufs, self._counts, self._windows)
        ]

    # ------------------------------------------------------------------
    def update(self, amount: float) -> None:
        """O(len(windows)) — extract the leading digit and update all tallies."""
        if amount is None or not math.isfinite(amount) or amount <= 0:
            return
        # Fast leading-digit extraction via log10 — avoids the while-loop in first_digit()
        digit = int(10 ** (math.log10(amount) % 1))
        if digit == 0:
            digit = 1
        idx = digit - 1
        for buf, counts, maxlen in self._slots:
            if len(buf) == maxlen:
                counts[buf[0] - 1] -= 1
            buf.append(digit)
            counts[idx] += 1

    def window_stats(self, window: int) -> BenfordStats:
        """Return BenfordStats for the requested window size. O(9)."""
        try:
            wi = self._windows.index(window)
        except ValueError:
            raise ValueError(f"Window {window} not in configured windows {self._windows}")

        counts = self._counts[wi]
        n = sum(counts)
        if n == 0:
            return BenfordStats(chi_square=0.0, mad=0.0, z_scores=[0.0] * 9, n=0)

        expected = self.BENFORD_EXPECTED
        observed = [c / n for c in counts]

        chi_sq = sum(
            (o * n - e * n) ** 2 / (e * n)
            for o, e in zip(observed, expected)
            if e > 0
        )
        mad = float(sum(abs(o - e) for o, e in zip(observed, expected)) / 9)

        zs = []
        for o, e in zip(observed, expected):
            num = max(abs(o - e) - 1 / (2 * n), 0.0)
            denom = math.sqrt(e * (1 - e) / n)
            zs.append(num / denom if denom > 0 else 0.0)

        return BenfordStats(chi_square=float(chi_sq), mad=mad, z_scores=zs, n=n)


# P(d) = log10(1 + 1/d) for d in 1..9
BENFORD_EXPECTED: dict[int, float] = {d: math.log10(1 + 1 / d) for d in DIGITS}

# Entropy (nats) of the theoretical Benford leading-digit distribution.
BENFORD_ENTROPY: float = float(-sum(p * math.log(p) for p in BENFORD_EXPECTED.values()))

# Numpy array form of Benford probabilities for vectorised bootstrap operations.
# Index 0 → digit 1, index 8 → digit 9.
BENFORD_PROBS: np.ndarray = np.array([math.log10(1 + 1 / d) for d in DIGITS])
BENFORD_PROBS = BENFORD_PROBS / BENFORD_PROBS.sum()

# Bootstrap configuration — overridable via environment variables.
# Asymptotic chi-square p-values are valid only when N * p_min >= 5, where
# p_min = P(digit=9) ≈ 0.046. That requires N >= 5/0.046 ≈ 109. We use 100
# as a conservative threshold so the bootstrap kicks in well before the
# asymptotic approximation degrades noticeably.
BENFORD_BOOTSTRAP_THRESHOLD: int = int(os.getenv("BENFORD_BOOTSTRAP_THRESHOLD", "100"))
BENFORD_BOOTSTRAP_SAMPLES: int = int(os.getenv("BENFORD_BOOTSTRAP_SAMPLES", "10000"))


@dataclass
class BenfordWindowFeatures:
    """Typed Benford features for a single rolling time-window."""

    window_hours: int
    n_transactions: int
    chi_square_stat: float
    chi_square_pvalue: float
    chi_square_pvalue_method: Literal["asymptotic", "bootstrap"]
    mad: float
    z_scores: list[float]  # 9 values, index 0 = digit 1 … index 8 = digit 9
    benford_flag: bool


def first_digit(value: float) -> int | None:
    """Return the leading (most significant) decimal digit of `value`.

    Returns None for zero, negative, or non-finite values, which are
    excluded from Benford analysis.
    """
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    while value < 1:
        value *= 10
    while value >= 10:
        value /= 10
    return int(value)


def digit_distribution(amounts: list[float]) -> dict[int, float]:
    """Return the observed proportion of each leading digit 1-9 in `amounts`."""
    digits = [d for d in (first_digit(a) for a in amounts) if d is not None]
    n = len(digits)
    if n == 0:
        return {d: 0.0 for d in DIGITS}
    counts = {d: 0 for d in DIGITS}
    for d in digits:
        counts[d] += 1
    return {d: counts[d] / n for d in DIGITS}


def chi_square_statistic(observed: dict[int, float], n: int) -> float:
    """Chi-square goodness-of-fit statistic vs. the Benford distribution.

    `observed` is a digit -> proportion mapping (e.g. from `digit_distribution`).
    `n` is the number of observations the proportions were computed from.
    """
    if n == 0:
        return 0.0
    chi_sq = 0.0
    for d in DIGITS:
        expected_count = BENFORD_EXPECTED[d] * n
        observed_count = observed.get(d, 0.0) * n
        if expected_count > 0:
            chi_sq += (observed_count - expected_count) ** 2 / expected_count
    return chi_sq


def z_scores(observed: dict[int, float], n: int) -> dict[int, float]:
    """Per-digit Z-score of the observed proportion vs. Benford's expectation."""
    if n == 0:
        return {d: 0.0 for d in DIGITS}
    scores = {}
    for d in DIGITS:
        p = BENFORD_EXPECTED[d]
        observed_p = observed.get(d, 0.0)
        # continuity correction as commonly used in Benford forensic analysis
        numerator = abs(observed_p - p) - (1 / (2 * n))
        denominator = math.sqrt(p * (1 - p) / n)
        scores[d] = max(numerator, 0.0) / denominator if denominator > 0 else 0.0
    return scores


def mean_absolute_deviation(observed: dict[int, float]) -> float:
    """MAD between observed and expected digit distributions.

    Values above ~0.015 (for first-digit tests) are commonly treated as
    indicating non-conformity with Benford's Law.
    """
    deviations = [abs(observed.get(d, 0.0) - BENFORD_EXPECTED[d]) for d in DIGITS]
    return float(np.mean(deviations))


# --------------------------------------------------------------------------- #
# Bootstrap infrastructure
# --------------------------------------------------------------------------- #

def _chi_sq_from_counts(observed: np.ndarray, expected: np.ndarray) -> float:
    """Chi-square statistic for raw count arrays (not proportions).

    Uses a small epsilon in the denominator to avoid division by zero when
    expected counts are negligibly small.
    """
    return float(np.sum((observed - expected) ** 2 / (expected + 1e-9)))


def bootstrap_chi_square_pvalue(
    observed_counts: np.ndarray,
    n_bootstrap: int = 10_000,
    seed: Optional[int] = None,
) -> float:
    """Monte Carlo bootstrap p-value for the Benford chi-square test.

    Generates `n_bootstrap` multinomial samples of size N drawn from the
    theoretical Benford distribution, computes the chi-square statistic for
    each, and returns the fraction that equals or exceeds the observed
    statistic — this fraction is the empirical p-value.

    The result is valid for any sample size, unlike the asymptotic chi-square
    approximation which requires N * p_i >= 5 for every digit class i.

    Args:
        observed_counts: array of shape (9,) with leading-digit counts for
            digits 1–9 (index 0 = digit 1, index 8 = digit 9).
        n_bootstrap: number of bootstrap replicates (default 10,000).
        seed: RNG seed for reproducibility. Use an integer in tests; leave as
            None in production so each call draws fresh randomness.

    Returns:
        Empirical p-value in (0, 1]. Never returns exactly 0.0; the floor is
        1 / n_bootstrap so that zero p-values are not reported.
    """
    N = int(observed_counts.sum())
    if N == 0:
        return 1.0

    expected = BENFORD_PROBS * N
    observed_stat = _chi_sq_from_counts(observed_counts, expected)

    rng = np.random.default_rng(seed)
    # Vectorised: all n_bootstrap samples in one call → shape (n_bootstrap, 9)
    bootstrap_samples = rng.multinomial(N, BENFORD_PROBS, size=n_bootstrap)

    bootstrap_stats = np.sum(
        (bootstrap_samples - expected) ** 2 / (expected + 1e-9),
        axis=1,
    )

    p_value = max(float((bootstrap_stats >= observed_stat).mean()), 1.0 / n_bootstrap)
    return p_value


@lru_cache(maxsize=512)
def _cached_bootstrap_pvalue(
    counts_tuple: tuple,
    n_bootstrap: int,
    seed: Optional[int],
) -> float:
    """LRU-cached wrapper around bootstrap_chi_square_pvalue.

    Cache key is (counts_tuple, n_bootstrap, seed). In production seed=None,
    so all calls with the same counts and n_bootstrap share one cache entry
    within a process lifetime — intentional, since repeated scoring of the
    same wallet window should return the same p-value.
    """
    counts = np.array(counts_tuple)
    return bootstrap_chi_square_pvalue(counts, n_bootstrap, seed)


def compute_chi_square_pvalue(counts: np.ndarray, N: int) -> tuple[float, str]:
    """Return (p_value, method) for the Benford chi-square test.

    Selects bootstrap when N < BENFORD_BOOTSTRAP_THRESHOLD (asymptotic
    approximation is unreliable for small samples), otherwise uses the
    asymptotic chi-square survival function with 8 degrees of freedom.

    Args:
        counts: array of shape (9,) with raw digit counts (not proportions).
        N: total number of observations (typically counts.sum(); passed
           explicitly to avoid recomputation and handle edge cases cleanly).

    Returns:
        Tuple of (p_value, method) where method is "bootstrap" or "asymptotic".
    """
    if N < BENFORD_BOOTSTRAP_THRESHOLD:
        p = _cached_bootstrap_pvalue(
            tuple(int(c) for c in counts),
            BENFORD_BOOTSTRAP_SAMPLES,
            None,
        )
        return p, "bootstrap"

    expected = BENFORD_PROBS * N
    stat = _chi_sq_from_counts(counts, expected)
    p = float(chi2.sf(stat, df=8))
    return p, "asymptotic"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def compute_benford_metrics(amounts: list[float]) -> dict:
    """Compute the full set of Benford metrics for a list of transaction amounts.

    Returns a dict with:
      - ``chi_square``: raw chi-square statistic
      - ``chi_square_pvalue``: p-value (bootstrap or asymptotic, see below)
      - ``pvalue_method``: ``"bootstrap"`` when N < BENFORD_BOOTSTRAP_THRESHOLD,
        ``"asymptotic"`` otherwise
      - ``mad``: Mean Absolute Deviation
      - ``z_scores``: per-digit Z-scores (dict[int, float])
      - ``observed_distribution``: digit -> proportion mapping
      - ``sample_size``: number of valid (positive, finite) amounts
    """
    observed = digit_distribution(amounts)
    n = sum(1 for a in amounts if first_digit(a) is not None)

    # With no valid samples the observed distribution is all-zeros, which
    # produces MAD ≈ 0.248 (mean of Benford expected values) even though
    # there is nothing to analyse.  Return 0.0 to stay consistent with
    # chi_square_statistic, which already short-circuits to 0.0 when n=0.
    if n == 0:
        return {
            "chi_square": 0.0,
            "mad": 0.0,
            "z_scores": {d: 0.0 for d in DIGITS},
            "observed_distribution": observed,
            "sample_size": 0,
        }

    return {
        "chi_square": chi_square_statistic(observed, n),
        "chi_square_pvalue": p_value,
        "pvalue_method": p_method,
        "mad": mean_absolute_deviation(observed),
        "z_scores": z_scores(observed, n),
        "observed_distribution": observed,
        "sample_size": n,
    }


def is_anomalous(metrics: dict, mad_threshold: float = 0.015) -> bool:
    """Whether a `compute_benford_metrics` result exceeds the MAD threshold."""
    return metrics["mad"] > mad_threshold


# ---------------------------------------------------------------------------
# Kolmogorov-Smirnov and Kuiper tests for Benford conformity
# ---------------------------------------------------------------------------

# Benford CDF: F(d) = sum_{k=1}^{d} log10(1 + 1/k) for d = 1..9
_BENFORD_CDF = np.cumsum([BENFORD_EXPECTED[d] for d in DIGITS])

KS_MIN_N = 5


@dataclass
class KSResult:
    """Result of a one-sample KS test against the Benford CDF."""
    d_statistic: float = float("nan")
    p_value: float = float("nan")
    ks_flag: bool = False
    valid: bool = False


@dataclass
class KuiperResult:
    """Result of a Kuiper test against the Benford CDF."""
    v_statistic: float = float("nan")
    p_value: float = float("nan")
    kuiper_flag: bool = False
    valid: bool = False


def compute_ks_statistic(digit_counts: np.ndarray) -> KSResult:
    """One-sample KS test of observed digit distribution against Benford CDF.

    ``digit_counts`` must be a length-9 array of non-negative integers (counts
    for digits 1-9). Valid for N >= 5; returns NaN statistics for smaller samples.

    The KS D-statistic is D = max_d |F_observed(d) - F_benford(d)|.
    Critical value at alpha=0.05: D_crit = 1.358 / sqrt(N).
    """
    digit_counts = np.asarray(digit_counts, dtype=float)
    if digit_counts.shape != (9,) or np.any(digit_counts < 0):
        return KSResult()

    n = digit_counts.sum()
    if n < KS_MIN_N:
        return KSResult()

    observed_cdf = np.cumsum(digit_counts) / n
    differences = np.abs(observed_cdf - _BENFORD_CDF)
    d_stat = float(np.max(differences))
    d_crit = 1.358 / math.sqrt(n)

    # Approximate p-value using Kolmogorov distribution
    lam = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d_stat
    p_value = _ks_pvalue(lam)

    return KSResult(
        d_statistic=d_stat,
        p_value=p_value,
        ks_flag=d_stat > d_crit,
        valid=True,
    )


def _ks_pvalue(lam: float) -> float:
    """Approximate survival function of the Kolmogorov distribution."""
    if lam <= 0:
        return 1.0
    p = 0.0
    for j in range(1, 101):
        term = ((-1) ** (j - 1)) * math.exp(-2.0 * j * j * lam * lam)
        p += term
    p = max(0.0, min(1.0, 2.0 * p))
    return p


def compute_kuiper_statistic(digit_counts: np.ndarray) -> KuiperResult:
    """Kuiper test of observed digit distribution against Benford CDF.

    The Kuiper V-statistic is V = D_plus + D_minus where
    D_plus = max(F_obs - F_benford) and D_minus = max(F_benford - F_obs).
    Rotation-invariant, making it more sensitive to tail deviations (digits 1
    and 9) where wash-trading bots using round lot sizes tend to deviate.

    Valid for N >= 5.
    """
    digit_counts = np.asarray(digit_counts, dtype=float)
    if digit_counts.shape != (9,) or np.any(digit_counts < 0):
        return KuiperResult()

    n = digit_counts.sum()
    if n < KS_MIN_N:
        return KuiperResult()

    observed_cdf = np.cumsum(digit_counts) / n
    d_plus = float(np.max(observed_cdf - _BENFORD_CDF))
    d_minus = float(np.max(_BENFORD_CDF - observed_cdf))
    v_stat = d_plus + d_minus

    p_value = _kuiper_pvalue(v_stat, int(n))

    return KuiperResult(
        v_statistic=v_stat,
        p_value=p_value,
        kuiper_flag=p_value < 0.05,
        valid=True,
    )


def _kuiper_pvalue(V: float, N: int) -> float:
    """Approximate Kuiper survival function P(V > v).

    Uses the series expansion from Press et al., Numerical Recipes, ch. 14.3:
    P(V > v) = 2 * sum_{j=1}^{100} (4j^2 v^2 - 1) * exp(-2 j^2 v^2)
    where v = V * (sqrt(N) + 0.155 + 0.24/sqrt(N)).
    """
    if V <= 0 or N < 1:
        return 1.0
    sqrt_n = math.sqrt(N)
    v = V * (sqrt_n + 0.155 + 0.24 / sqrt_n)
    if v <= 0:
        return 1.0

    p = 0.0
    for j in range(1, 101):
        j2v2 = j * j * v * v
        exp_val = -2.0 * j2v2
        if exp_val < -300:
            break
        term = (4.0 * j2v2 - 1.0) * math.exp(exp_val)
        p += term

    p = max(0.0, min(1.0, 2.0 * p))
    return p


def compute_benford_ks_kuiper(amounts: List[float]) -> dict:
    """Compute KS and Kuiper statistics for a list of trade amounts.

    Returns a dict with ks_stat, ks_pval, ks_flag, kuiper_stat, kuiper_pval,
    kuiper_flag keys.
    """
    counts, n = _extract_digit_histogram(amounts)

    ks = compute_ks_statistic(counts)
    kuiper = compute_kuiper_statistic(counts)

    return {
        "ks_stat": ks.d_statistic,
        "ks_pval": ks.p_value,
        "ks_flag": ks.ks_flag,
        "kuiper_stat": kuiper.v_statistic,
        "kuiper_pval": kuiper.p_value,
        "kuiper_flag": kuiper.kuiper_flag,
    }


# ---------------------------------------------------------------------------
# Stratified Benford analysis per asset pair
# ---------------------------------------------------------------------------

_ASSET_PAIR_RE = re.compile(r"^[A-Z0-9/.:\-]{1,30}$")
CHI2_CRITICAL_005 = 15.507  # df=8, alpha=0.05
MIN_STRATUM_SIZE = 30


@dataclass
class BenfordResult:
    """Per-stratum Benford analysis result."""

    asset_pair: str
    chi_square: float = 0.0
    mad: float = 0.0
    z_scores: Dict[int, float] = field(default_factory=dict)
    flagged_digits: List[int] = field(default_factory=list)
    benford_flag: bool = False
    sample_size: int = 0
    valid: bool = True
    reason: str = ""
    observed_distribution: Dict[int, float] = field(default_factory=dict)


@dataclass
class StratifiedBenfordSummary:
    """Aggregated summary across all per-stratum Benford results."""

    stratum_results: Dict[str, BenfordResult] = field(default_factory=dict)
    max_stratum_chi2: float = 0.0
    max_stratum_MAD: float = 0.0
    mean_stratum_MAD: float = 0.0
    n_strata_above_0015: int = 0
    n_flagged_strata: int = 0
    fallback_global: bool = False


def _sanitize_asset_pair(pair: str) -> str | None:
    if not pair or len(pair) > 30:
        return None
    if not _ASSET_PAIR_RE.match(pair.upper()):
        return None
    return pair


def _canonical_asset_pair(base: str, counter: str) -> str:
    parts = sorted([base, counter])
    return f"{parts[0]}/{parts[1]}"


def _extract_digit_histogram(amounts: List[float]) -> Tuple[np.ndarray, int]:
    """Extract a 9-element digit frequency histogram from amounts.

    Returns (histogram, n_valid). Caches only the histogram, not raw amounts.
    """
    counts = np.zeros(9, dtype=int)
    n = 0
    for a in amounts:
        if a is None or not math.isfinite(a) or a <= 0:
            continue
        d = first_digit(a)
        if d is not None:
            counts[d - 1] += 1
            n += 1
    return counts, n


def compute_stratum_benford(asset_pair: str, amounts: List[float]) -> BenfordResult:
    """Compute Benford statistics for a single asset-pair stratum."""
    counts, n = _extract_digit_histogram(amounts)

    if n < MIN_STRATUM_SIZE:
        return BenfordResult(
            asset_pair=asset_pair,
            sample_size=n,
            valid=False,
            reason="insufficient_sample",
        )

    observed = {d: counts[d - 1] / n for d in DIGITS}
    chi_sq = chi_square_statistic(observed, n)
    zs = z_scores(observed, n)
    mad_val = mean_absolute_deviation(observed)
    flagged = [d for d in DIGITS if abs(zs.get(d, 0.0)) > 1.96]

    return BenfordResult(
        asset_pair=asset_pair,
        chi_square=chi_sq,
        mad=mad_val,
        z_scores=zs,
        flagged_digits=flagged,
        benford_flag=chi_sq > CHI2_CRITICAL_005,
        sample_size=n,
        valid=True,
        observed_distribution=observed,
    )


def stratified_benford_analysis(
    trades: List | pd.DataFrame,
    min_stratum_size: int = MIN_STRATUM_SIZE,
) -> StratifiedBenfordSummary:
    """Group trades by canonical asset pair and compute per-stratum Benford stats.

    Accepts either a list of Trade objects (from ingestion.data_models) or a
    DataFrame with ``base_asset``, ``counter_asset``, and ``base_amount`` columns.
    """
    if isinstance(trades, pd.DataFrame):
        grouped = _group_trades_df(trades)
    else:
        grouped = _group_trades_list(trades)

    if not grouped:
        return StratifiedBenfordSummary(fallback_global=True)

    results: Dict[str, BenfordResult] = {}
    for pair, amounts in grouped.items():
        results[pair] = compute_stratum_benford(pair, amounts)

    valid_results = [r for r in results.values() if r.valid]

    if not valid_results:
        all_amounts: List[float] = []
        for amounts in grouped.values():
            all_amounts.extend(amounts)
        global_result = compute_stratum_benford("__global__", all_amounts)
        results["__global__"] = global_result
        valid_results = [global_result] if global_result.valid else []
        return StratifiedBenfordSummary(
            stratum_results=results,
            max_stratum_chi2=global_result.chi_square if global_result.valid else 0.0,
            max_stratum_MAD=global_result.mad if global_result.valid else 0.0,
            mean_stratum_MAD=global_result.mad if global_result.valid else 0.0,
            n_strata_above_0015=1 if global_result.valid and global_result.mad > 0.015 else 0,
            n_flagged_strata=1 if global_result.valid and global_result.benford_flag else 0,
            fallback_global=True,
        )

    max_chi2 = max(r.chi_square for r in valid_results)
    mads = [r.mad for r in valid_results]
    max_mad = max(mads)
    mean_mad = sum(mads) / len(mads)
    n_above = sum(1 for m in mads if m > 0.015)
    n_flagged = sum(1 for r in valid_results if r.benford_flag)

    return StratifiedBenfordSummary(
        stratum_results=results,
        max_stratum_chi2=max_chi2,
        max_stratum_MAD=max_mad,
        mean_stratum_MAD=mean_mad,
        n_strata_above_0015=n_above,
        n_flagged_strata=n_flagged,
        fallback_global=False,
    )


def _group_trades_df(trades: pd.DataFrame) -> Dict[str, List[float]]:
    """Group a trades DataFrame by canonical asset pair."""
    grouped: Dict[str, List[float]] = {}
    if trades.empty:
        return grouped

    for _, row in trades.iterrows():
        base_asset = row.get("base_asset")
        counter_asset = row.get("counter_asset")
        amount = row.get("base_amount")

        if base_asset is None or counter_asset is None:
            continue

        if isinstance(base_asset, dict):
            base_sym = base_asset.get("code", "")
        else:
            base_sym = getattr(base_asset, "pair_symbol", str(base_asset))

        if isinstance(counter_asset, dict):
            counter_sym = counter_asset.get("code", "")
        else:
            counter_sym = getattr(counter_asset, "pair_symbol", str(counter_asset))

        pair = _canonical_asset_pair(base_sym, counter_sym)
        if _sanitize_asset_pair(pair) is None:
            logger.debug("Skipping unsanitizable asset pair: %s", pair)
            continue

        grouped.setdefault(pair, []).append(float(amount))

    return grouped


def _group_trades_list(trades: list) -> Dict[str, List[float]]:
    """Group a list of Trade model objects by canonical asset pair."""
    grouped: Dict[str, List[float]] = {}
    for trade in trades:
        base_sym = trade.base_asset.pair_symbol
        counter_sym = trade.counter_asset.pair_symbol
        pair = _canonical_asset_pair(base_sym, counter_sym)
        if _sanitize_asset_pair(pair) is None:
            logger.debug("Skipping unsanitizable asset pair: %s", pair)
            continue
        grouped.setdefault(pair, []).append(float(trade.base_amount))
    return grouped


# ---------------------------------------------------------------------------
# Adaptive window sizing
# ---------------------------------------------------------------------------

@dataclass
class BenfordWindowResult:
    """Result of an adaptive Benford window fit for a single target window.

    Attributes
    ----------
    trades:
        The trade amounts used for Benford analysis (sliced to the effective
        window, possibly wider than the target).
    effective_width:
        The actual window duration used (a ``pd.Timedelta``); may be wider
        than the target when the sample count was below ``min_sample_count``.
    valid:
        ``True`` when ``len(trades) >= min_sample_count`` so chi-square and
        MAD statistics are statistically reliable.
    expanded:
        ``True`` when the window had to be widened beyond the target to meet
        the minimum sample count.
    merged:
        ``True`` when two adjacent windows were merged because neither alone
        reached the minimum sample count.
    label:
        The original target window label (e.g. ``"1h"``).
    """

    trades: list
    effective_width: "pd.Timedelta"
    valid: bool
    expanded: bool
    merged: bool
    label: str


class AdaptiveBenfordWindow:
    """Adaptive rolling-window selector for Benford's Law analysis.

    Ensures each window contains at least ``min_sample_count`` trades before
    computing chi-square / MAD statistics. If the target window is too narrow,
    the window is doubled up to ``max_window_days`` days. If even the maximum
    window has too few trades, adjacent windows are merged.

    Parameters
    ----------
    min_sample_count:
        Minimum number of valid (positive, finite) trade amounts required for
        statistically reliable Benford analysis. Default: 30.
    max_window_days:
        Maximum window width in days before falling back to merging. Default: 90.
    """

    def __init__(self, min_sample_count: int = 30, max_window_days: int = 90) -> None:
        self.min_sample_count = min_sample_count
        self.max_window_days = max_window_days

    def _count_valid(self, amounts: list) -> int:
        return sum(1 for a in amounts if first_digit(a) is not None)

    def _slice_amounts(
        self, trades: pd.DataFrame, as_of: pd.Timestamp, width: "pd.Timedelta"
    ) -> list:
        start = as_of - width
        mask = (trades["ledger_close_time"] > start) & (trades["ledger_close_time"] <= as_of)
        return trades.loc[mask, "base_amount"].tolist()

    def fit(
        self,
        trades: pd.DataFrame,
        target_window_label: str,
        as_of: pd.Timestamp,
        window_map: "dict[str, pd.Timedelta]",
    ) -> BenfordWindowResult:
        """Fit the adaptive window for ``target_window_label``.

        Expands the window by doubling until either ``min_sample_count`` is
        reached or ``max_window_days`` is exceeded. When neither single-window
        expansion reaches the threshold, the *all-time* slice up to ``as_of``
        is used and the result is marked ``merged=True``.

        Parameters
        ----------
        trades:
            DataFrame with ``ledger_close_time`` and ``base_amount`` columns.
        target_window_label:
            One of the keys in ``window_map`` (e.g. ``"1h"``).
        as_of:
            The reference timestamp (end of all windows).
        window_map:
            Mapping from window label to ``pd.Timedelta``; used to determine
            the starting width.
        """
        max_width = pd.Timedelta(days=self.max_window_days)
        target_width = window_map[target_window_label]

        width = target_width
        expanded = False
        while True:
            amounts = self._slice_amounts(trades, as_of, width)
            if self._count_valid(amounts) >= self.min_sample_count:
                if width > target_width:
                    logger.warning(
                        "Benford window '%s' expanded from %s to %s (sample count was below %d)",
                        target_window_label, target_width, width, self.min_sample_count,
                    )
                    expanded = True
                return BenfordWindowResult(
                    trades=amounts,
                    effective_width=width,
                    valid=True,
                    expanded=expanded,
                    merged=False,
                    label=target_window_label,
                )
            if width >= max_width:
                break
            width = min(width * 2, max_width)

        # Neither expansion nor max_width was sufficient; use all available trades.
        all_amounts = trades["base_amount"].tolist() if not trades.empty else []
        valid = self._count_valid(all_amounts) >= self.min_sample_count
        if not valid:
            logger.warning(
                "Benford window '%s': only %d valid trades available (min_sample_count=%d). "
                "Statistics may be unreliable.",
                target_window_label, self._count_valid(all_amounts), self.min_sample_count,
            )
        else:
            logger.warning(
                "Benford window '%s' merged to full trade history (%d trades) "
                "because no single expansion reached %d samples.",
                target_window_label, self._count_valid(all_amounts), self.min_sample_count,
            )
        return BenfordWindowResult(
            trades=all_amounts,
            effective_width=max_width,
            valid=valid,
            expanded=True,
            merged=True,
            label=target_window_label,
        )


# ---------------------------------------------------------------------------
# Multivariate (cross-pair) Benford analysis
#
# A syndicate that splits wash volume evenly across N pairs keeps each pair's
# marginal digit distribution near Benford, so the univariate MAD/chi-square
# tests above see nothing. The coordination only shows up in the *joint*
# distribution: the pairs deviate from Benford in the same way at the same time.
# ---------------------------------------------------------------------------

_EXPECTED_VECTOR = np.array([BENFORD_EXPECTED[d] for d in DIGITS])


def _pair_series(trades: pd.DataFrame) -> pd.Series:
    """Return a per-row asset-pair label for `trades`.

    Uses an explicit `asset_pair` column when present, otherwise derives the
    pair from the `base_asset`/`counter_asset` dict columns.
    """
    if "asset_pair" in trades.columns:
        return trades["asset_pair"]

    def _symbol(asset: dict) -> str:
        code = asset["code"]
        issuer = asset.get("issuer")
        return code if issuer is None else f"{code}:{issuer}"

    return trades.apply(
        lambda r: f"{_symbol(r['base_asset'])}/{_symbol(r['counter_asset'])}", axis=1
    )


def joint_digit_matrix(
    trades: pd.DataFrame,
    pairs: list[str],
    window: pd.Timedelta | None = None,
) -> np.ndarray:
    """Build the joint leading-digit frequency matrix across `pairs`.

    Returns an array of shape ``(K, 9)`` where ``K = len(pairs)`` and row ``k``
    is the observed leading-digit frequency vector (digits 1-9) of pair ``k``'s
    `base_amount`s. When `window` is given and `trades` carries a
    `ledger_close_time` column, only trades within `window` of the most recent
    trade are used. Pairs with no trades contribute an all-zero row.
    """
    if trades is None or trades.empty:
        return np.zeros((len(pairs), 9))

    df = trades
    if window is not None and "ledger_close_time" in df.columns:
        times = pd.to_datetime(df["ledger_close_time"])
        cutoff = times.max() - window
        df = df.loc[times > cutoff]

    pair_labels = _pair_series(df)
    matrix = np.zeros((len(pairs), 9))
    for k, pair in enumerate(pairs):
        amounts = df.loc[pair_labels == pair, "base_amount"].tolist()
        dist = digit_distribution(amounts)
        matrix[k] = [dist[d] for d in DIGITS]
    return matrix


def _normal_scores(row: np.ndarray) -> np.ndarray:
    """Van der Waerden normal-score (Gaussian copula) transform of a vector."""
    order = row.argsort()
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(row) + 1)
    return norm.ppf(ranks / (len(row) + 1))


def benford_copula_statistic(digit_matrix: np.ndarray) -> tuple[float, float]:
    """Test for coordinated cross-pair digit manipulation via a Gaussian copula.

    Each pair's deviation-from-Benford vector is mapped to Gaussian-copula
    pseudo-observations (normal scores), and the cross-pair correlation matrix is
    formed treating each pair as a variable observed over the 9 digits. Under the
    null — rows are i.i.d. Benford draws with zero copula correlation — the
    scaled sum of squared off-diagonal correlations is ``chi2`` distributed with
    ``C(K, 2)`` degrees of freedom. Coordinated pairs deviate from Benford in the
    same digit pattern, inflating the correlations and the statistic.

    Returns ``(statistic, p_value)``. A small p-value => coordinated manipulation.
    """
    matrix = np.asarray(digit_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 2:
        return 0.0, 1.0

    # Drop pairs with no observations; a zero-row has no Benford signal.
    active = matrix[matrix.sum(axis=1) > 0]
    if active.shape[0] < 2:
        return 0.0, 1.0

    deviations = active - _EXPECTED_VECTOR
    scored = np.vstack([_normal_scores(row) for row in deviations])

    corr = np.corrcoef(scored)
    corr = np.nan_to_num(corr, nan=0.0)

    k = active.shape[0]
    dof_per_corr = active.shape[1] - 1  # 9 digits, 1 lost to the copula transform
    upper = corr[np.triu_indices(k, k=1)]
    statistic = float(dof_per_corr * np.sum(upper**2))
    df = len(upper)
    p_value = float(chi2.sf(statistic, df)) if df > 0 else 1.0
    return statistic, p_value


def cross_pair_sync_score(
    trades: pd.DataFrame,
    pairs: list[str],
    window: pd.Timedelta = pd.Timedelta(minutes=1),
    z_threshold: float = 2.5,
    min_pairs: int = 3,
) -> float:
    """Fraction of time windows with simultaneous cross-pair digit anomalies.

    Buckets `trades` into `window`-sized bins; within each active bin a pair is
    "anomalous" when its maximum per-digit Benford Z-score exceeds `z_threshold`.
    A bin is *synchronised* when at least `min_pairs` pairs are simultaneously
    anomalous. Returns the fraction of active bins that are synchronised — high
    values indicate the pairs are being manipulated in concert.
    """
    if trades is None or trades.empty or "ledger_close_time" not in trades.columns:
        return 0.0

    df = trades.copy()
    df["ledger_close_time"] = pd.to_datetime(df["ledger_close_time"])
    df = df.assign(_pair=_pair_series(df).to_numpy())
    df = df[df["_pair"].isin(pairs)]
    if df.empty:
        return 0.0

    df = df.assign(
        _digit=df["base_amount"].map(first_digit),
        _bucket=df["ledger_close_time"].dt.floor(window),
    ).dropna(subset=["_digit"])
    if df.empty:
        return 0.0
    df["_digit"] = df["_digit"].astype(int)

    # Counts per (bucket, pair, digit) -> a (groups x 9) matrix, then a fully
    # vectorised per-group Benford Z-score (matching `z_scores`).
    counts_df = (
        df.groupby(["_bucket", "_pair", "_digit"]).size().unstack("_digit", fill_value=0)
    )
    counts_df = counts_df.reindex(columns=DIGITS, fill_value=0)
    counts = counts_df.to_numpy(dtype=float)
    n = counts.sum(axis=1, keepdims=True)

    with np.errstate(invalid="ignore", divide="ignore"):
        observed = np.divide(counts, n, out=np.zeros_like(counts), where=n > 0)
        numerator = np.clip(np.abs(observed - _EXPECTED_VECTOR) - 1.0 / (2.0 * n), 0.0, None)
        denominator = np.sqrt(_EXPECTED_VECTOR * (1.0 - _EXPECTED_VECTOR) / n)
        z = np.where(denominator > 0, numerator / denominator, 0.0)
    max_z = z.max(axis=1)

    anomalous_per_bucket = (
        pd.Series(max_z > z_threshold, index=counts_df.index.get_level_values("_bucket"))
        .groupby(level=0)
        .sum()
    )
    active_bins = len(anomalous_per_bucket)
    sync_bins = int((anomalous_per_bucket >= min_pairs).sum())
    return float(sync_bins / active_bins) if active_bins else 0.0


def digit_entropy_delta(digit_matrix: np.ndarray) -> float:
    """Observed-minus-expected leading-digit entropy of the pooled distribution.

    The rows of `digit_matrix` are averaged into a single joint digit
    distribution whose Shannon entropy (nats) is compared to Benford's. A
    negative delta means the joint distribution is more concentrated than
    Benford predicts — the hallmark of coordinated round-number wash volume.
    """
    matrix = np.asarray(digit_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.size == 0:
        return 0.0

    pooled = matrix.mean(axis=0)
    total = pooled.sum()
    if total <= 0:
        return 0.0
    pooled = pooled / total

    observed_entropy = float(-sum(p * math.log(p) for p in pooled if p > 0))
    return observed_entropy - BENFORD_ENTROPY


def multivariate_benford_score(
    trades: pd.DataFrame,
    wallet_pairs: list[tuple[str, str]],
    window: pd.Timedelta = pd.Timedelta(hours=24),
) -> dict:
    """Multivariate Benford entry point for a set of `(wallet, pair)` combinations.

    Restricts `trades` to rows where one of the listed wallets traded one of the
    listed pairs, then computes the cross-pair copula statistic, the synchrony
    ratio, and the joint digit-entropy delta. Returns a dict with
    `copula_statistic`, `copula_pval`, `sync_ratio`, `digit_entropy_delta`, and
    the active `pairs`.
    """
    pairs = sorted({p for _, p in wallet_pairs})
    zero = {
        "copula_statistic": 0.0,
        "copula_pval": 1.0,
        "sync_ratio": 0.0,
        "digit_entropy_delta": 0.0,
        "pairs": pairs,
    }
    if trades is None or trades.empty or len(pairs) < 2:
        return zero

    wallets = {w for w, _ in wallet_pairs}
    df = trades
    base = df["base_account"] if "base_account" in df.columns else pd.Series(index=df.index, dtype=object)
    counter = df["counter_account"] if "counter_account" in df.columns else pd.Series(index=df.index, dtype=object)
    df = df[base.isin(wallets) | counter.isin(wallets)]
    if df.empty:
        return zero

    matrix = joint_digit_matrix(df, pairs, window)
    statistic, pval = benford_copula_statistic(matrix)
    return {
        "copula_statistic": statistic,
        "copula_pval": pval,
        "sync_ratio": cross_pair_sync_score(df, pairs),
        "digit_entropy_delta": digit_entropy_delta(matrix),
        "pairs": pairs,
    }
