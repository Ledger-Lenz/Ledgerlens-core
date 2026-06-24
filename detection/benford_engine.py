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

import math

import numpy as np
import pandas as pd
from scipy.stats import chi2, norm

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
# Kolmogorov-Smirnov and Kuiper tests for small-sample Benford conformity
# ---------------------------------------------------------------------------

# Benford CDF: F(d) = sum_{k=1}^{d} log10(1 + 1/k) for d = 1..9
_BENFORD_CDF = np.cumsum([BENFORD_EXPECTED[d] for d in DIGITS])


def compute_ks_statistic(digit_counts: np.ndarray) -> dict:
    """One-sample KS test against the Benford CDF.

    ``digit_counts`` must be a length-9 array of non-negative integer
    counts for digits 1–9.  Valid for N >= 5; returns NaN for N < 5.

    Returns ``{"ks_stat": float, "ks_pval": float, "ks_flag": bool}``.
    """
    digit_counts = np.asarray(digit_counts, dtype=float)
    if len(digit_counts) != 9 or (digit_counts < 0).any():
        return {"ks_stat": float("nan"), "ks_pval": float("nan"), "ks_flag": False}

    n = digit_counts.sum()
    if n < 5:
        return {"ks_stat": float("nan"), "ks_pval": float("nan"), "ks_flag": False}

    observed_cdf = np.cumsum(digit_counts) / n
    d_stat = float(np.max(np.abs(observed_cdf - _BENFORD_CDF)))
    d_crit = 1.358 / math.sqrt(n)
    flag = d_stat > d_crit
    # Approximate p-value via Kolmogorov distribution
    lam = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d_stat
    p_value = max(0.0, min(1.0, 2.0 * sum(
        ((-1) ** (k - 1)) * math.exp(-2.0 * k * k * lam * lam)
        for k in range(1, 101)
    )))
    return {"ks_stat": d_stat, "ks_pval": p_value, "ks_flag": flag}


def _kuiper_pvalue(v: float, n: int) -> float:
    """Kuiper V-statistic p-value via series expansion (Press et al.)."""
    lam = v * (math.sqrt(n) + 0.155 + 0.24 / math.sqrt(n))
    if lam < 0.01:
        return 1.0
    p = 0.0
    for j in range(1, 101):
        term = (4.0 * j * j * lam * lam - 1.0) * math.exp(-2.0 * j * j * lam * lam)
        if abs(term) < 1e-300:
            break
        p += term
    return max(0.0, min(1.0, 2.0 * p))


def _digit_counts_from_amounts(amounts: list[float]) -> np.ndarray:
    """Extract a length-9 digit-count array from trade amounts."""
    counts = np.zeros(9)
    for a in amounts:
        d = first_digit(a)
        if d is not None:
            counts[d - 1] += 1
    return counts


def compute_kuiper_statistic(digit_counts: np.ndarray) -> dict:
    """Kuiper V-test against the Benford CDF.

    Rotation-invariant variant of KS, more sensitive to tail deviations
    (digits 1 and 9) where wash-trading bots using round lots tend to
    deviate.  Valid for N >= 5; returns NaN for N < 5.

    Returns ``{"kuiper_stat": float, "kuiper_pval": float, "kuiper_flag": bool}``.
    """
    digit_counts = np.asarray(digit_counts, dtype=float)
    if len(digit_counts) != 9 or (digit_counts < 0).any():
        return {"kuiper_stat": float("nan"), "kuiper_pval": float("nan"), "kuiper_flag": False}

    n = digit_counts.sum()
    if n < 5:
        return {"kuiper_stat": float("nan"), "kuiper_pval": float("nan"), "kuiper_flag": False}

    observed_cdf = np.cumsum(digit_counts) / n
    diff = observed_cdf - _BENFORD_CDF
    d_plus = float(np.max(diff))
    d_minus = float(np.max(-diff))
    v_stat = d_plus + d_minus
    p_value = _kuiper_pvalue(v_stat, int(n))
    flag = p_value < 0.05
    return {"kuiper_stat": v_stat, "kuiper_pval": p_value, "kuiper_flag": flag}


# ---------------------------------------------------------------------------
# Asset-pair stratified Benford analysis
# ---------------------------------------------------------------------------

import re

_VALID_PAIR_RE = re.compile(r"^[A-Za-z0-9/.\-:]{1,30}$")


def _normalize_asset_pair(base: str, counter: str) -> str:
    """Canonical lexicographically-ordered pair string."""
    pair = f"{base}/{counter}" if base <= counter else f"{counter}/{base}"
    return pair


def stratified_benford_analysis(
    trades: "list | pd.DataFrame",
    min_stratum_size: int = 30,
) -> dict:
    """Compute per-stratum Benford metrics grouped by asset pair.

    Accepts a list of Trade objects (with ``.base_asset``, ``.counter_asset``,
    ``base_amount`` attributes) or a DataFrame with ``base_asset``,
    ``counter_asset``, and ``base_amount`` columns.

    Returns ``{"strata": {pair: BenfordResult, ...}, "summary": {...},
    "fallback_global": bool}``.
    """
    import pandas as pd

    if isinstance(trades, pd.DataFrame):
        df = trades
    else:
        df = pd.DataFrame([{
            "base_asset": getattr(t, "base_asset", {}),
            "counter_asset": getattr(t, "counter_asset", {}),
            "base_amount": getattr(t, "base_amount", 0.0),
        } for t in trades])

    if df.empty:
        return {"strata": {}, "summary": _empty_stratum_summary(), "fallback_global": True}

    def _asset_label(asset) -> str:
        if isinstance(asset, dict):
            code = asset.get("code", "unknown")
            issuer = asset.get("issuer")
            return code if issuer is None else f"{code}:{issuer}"
        return str(asset)

    grouped: dict[str, list[float]] = {}
    for _, row in df.iterrows():
        base_label = _asset_label(row.get("base_asset", ""))
        counter_label = _asset_label(row.get("counter_asset", ""))
        pair = _normalize_asset_pair(base_label, counter_label)
        if not _VALID_PAIR_RE.match(pair.replace("/", "")):
            continue
        grouped.setdefault(pair, []).append(float(row["base_amount"]))

    strata: dict = {}
    valid_strata: list[dict] = []

    for pair, amounts in grouped.items():
        n = sum(1 for a in amounts if first_digit(a) is not None)
        if n < min_stratum_size:
            strata[pair] = {"valid": False, "reason": "insufficient_sample", "sample_size": n}
            continue
        metrics = compute_benford_metrics(amounts)
        metrics["valid"] = True
        metrics["benford_flag"] = metrics["chi_square"] > 15.507
        strata[pair] = metrics
        valid_strata.append(metrics)

    if not valid_strata:
        all_amounts = []
        for amounts in grouped.values():
            all_amounts.extend(amounts)
        global_metrics = compute_benford_metrics(all_amounts) if all_amounts else {}
        return {
            "strata": strata,
            "summary": _stratum_summary(valid_strata),
            "global_fallback_metrics": global_metrics,
            "fallback_global": True,
        }

    return {
        "strata": strata,
        "summary": _stratum_summary(valid_strata),
        "fallback_global": False,
    }


def _empty_stratum_summary() -> dict:
    return {
        "max_stratum_chi2": 0.0,
        "max_stratum_MAD": 0.0,
        "mean_stratum_MAD": 0.0,
        "n_flagged_strata": 0,
        "n_strata_above_0015": 0,
    }


def _stratum_summary(valid_strata: list[dict]) -> dict:
    if not valid_strata:
        return _empty_stratum_summary()
    chi2s = [s["chi_square"] for s in valid_strata]
    mads = [s["mad"] for s in valid_strata]
    return {
        "max_stratum_chi2": max(chi2s),
        "max_stratum_MAD": max(mads),
        "mean_stratum_MAD": sum(mads) / len(mads),
        "n_flagged_strata": sum(1 for s in valid_strata if s.get("benford_flag")),
        "n_strata_above_0015": sum(1 for m in mads if m > 0.015),
    }


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
