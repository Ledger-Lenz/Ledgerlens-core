"""Benford's Law digit-distribution analysis for transaction amounts.

Computes the chi-square statistic, per-digit Z-scores, and Mean Absolute
Deviation (MAD) of the leading-digit distribution of a set of amounts,
relative to the theoretical Benford distribution.

The univariate helpers (`compute_benford_metrics` etc.) score a single
`(wallet, asset_pair)` stream. A coordinated wash-trading syndicate can keep
each individual pair close to Benford while the *joint* cross-pair behaviour is
statistically impossible under independent trading. The multivariate helpers
(`joint_digit_matrix`, `benford_copula_statistic`, `cross_pair_sync_score`,
`multivariate_benford_score`) surface that coordination signal.
"""

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import chi2, norm

logger = logging.getLogger("ledgerlens.benford_engine")

DIGITS = list(range(1, 10))

# P(d) = log10(1 + 1/d) for d in 1..9
BENFORD_EXPECTED: dict[int, float] = {d: math.log10(1 + 1 / d) for d in DIGITS}

# Entropy (nats) of the theoretical Benford leading-digit distribution.
BENFORD_ENTROPY: float = float(-sum(p * math.log(p) for p in BENFORD_EXPECTED.values()))


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


def compute_benford_metrics(amounts: list[float]) -> dict:
    """Compute the full set of Benford metrics for a list of transaction amounts.

    Returns a dict with `chi_square`, `mad`, `z_scores` (per digit), the
    `observed_distribution`, and `sample_size`.
    """
    observed = digit_distribution(amounts)
    n = sum(1 for a in amounts if first_digit(a) is not None)

    return {
        "chi_square": chi_square_statistic(observed, n),
        "mad": mean_absolute_deviation(observed),
        "z_scores": z_scores(observed, n),
        "observed_distribution": observed,
        "sample_size": n,
    }


def is_anomalous(metrics: dict, mad_threshold: float = 0.015) -> bool:
    """Whether a `compute_benford_metrics` result exceeds the MAD threshold."""
    return metrics["mad"] > mad_threshold


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

    deviations = matrix - _EXPECTED_VECTOR
    scored = np.vstack([_normal_scores(row) for row in deviations])

    corr = np.corrcoef(scored)
    corr = np.nan_to_num(corr, nan=0.0)

    k = matrix.shape[0]
    dof_per_corr = matrix.shape[1] - 1  # 9 digits, 1 lost to the copula transform
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
