"""Cross-asset correlation analysis for detecting coordinated wash-trading
across multiple SDEX asset pairs.

Uses Spearman rank correlation on 4-hour bucketed volume time series.
Spearman is chosen over Pearson because on-chain volume is heavy-tailed:
a single large wash trade inflates Pearson spuriously but cannot distort
rank order beyond its bucket, keeping false-positive rates low.
"""

from __future__ import annotations

import pandas as pd
import numpy as np
from scipy.stats import t as student_t


def _benjamini_hochberg(p_values: np.ndarray, alpha: float) -> np.ndarray:
    """Return which hypotheses survive the Benjamini-Hochberg FDR procedure."""
    if not 0.0 < alpha <= 1.0:
        raise ValueError("fdr_alpha must be in (0, 1]")
    if p_values.size == 0:
        return np.zeros(0, dtype=bool)

    order = np.argsort(p_values)
    thresholds = alpha * (np.arange(1, len(p_values) + 1) / len(p_values))
    passing = p_values[order] <= thresholds
    if not passing.any():
        return np.zeros(len(p_values), dtype=bool)
    largest_passing_rank = np.flatnonzero(passing)[-1]
    cutoff = p_values[order[largest_passing_rank]]
    return p_values <= cutoff


def build_volume_time_series(
    trades_by_pair: dict[str, pd.DataFrame],
    bucket: str = "4h",
    lookback_days: int = 30,
) -> pd.DataFrame:
    """Build a (time_bucket × asset_pair) volume matrix.

    Returns a DataFrame indexed by time bucket, one column per asset pair,
    values = total base_amount in that bucket. Missing buckets are filled
    with 0.0.

    Implementation note: all pair DataFrames are concatenated before a single
    groupby-resample so the expensive resample overhead is paid once rather
    than once-per-pair.
    """
    if not trades_by_pair:
        return pd.DataFrame()

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days)

    # Collect per-pair (times_us_int64, amounts) arrays.
    # We normalise everything to microsecond int64 (pandas 2.x default for tz-aware
    # datetimes) to avoid repeated datetime boxing while staying tz-safe.
    bucket_us = int(pd.Timedelta(bucket).value) // 1_000
    cutoff_us = int(cutoff.value) // 1_000

    pair_names: list[str] = []
    pair_times: list[np.ndarray] = []
    pair_amounts: list[np.ndarray] = []

    for pair, df in trades_by_pair.items():
        if df.empty:
            continue
        dti = pd.to_datetime(df["ledger_close_time"].values, utc=True)
        # .asi8 returns int64 in the array's native unit (us for datetime64[us, UTC])
        # Convert to microseconds explicitly via as_unit.
        t = dti.as_unit("us").asi8
        a = df["base_amount"].values.astype(np.float64)
        mask = t >= cutoff_us
        if not mask.any():
            continue
        pair_names.append(pair)
        pair_times.append(t[mask])
        pair_amounts.append(a[mask])

    if not pair_names:
        return pd.DataFrame()

    # Build the global bucket index once across all pairs
    all_bucketed = np.concatenate([t - (t % bucket_us) for t in pair_times])
    unique_buckets = np.unique(all_bucketed)
    bucket_to_idx = {b: i for i, b in enumerate(unique_buckets)}
    n_buckets = len(unique_buckets)

    matrix_data = np.zeros((n_buckets, len(pair_names)), dtype=np.float64)
    for col_i, (t_arr, a_arr) in enumerate(zip(pair_times, pair_amounts)):
        bucketed = t_arr - (t_arr % bucket_us)
        idxs = np.fromiter((bucket_to_idx[b] for b in bucketed), dtype=np.intp, count=len(bucketed))
        np.add.at(matrix_data[:, col_i], idxs, a_arr)

    # Reconstruct tz-aware DatetimeIndex from microsecond int64
    time_index = pd.to_datetime(unique_buckets * 1_000, unit="ns", utc=True)
    matrix = pd.DataFrame(matrix_data, index=time_index, columns=pair_names)
    matrix.index.name = "ledger_close_time"
    return matrix


def find_correlated_pairs(
    volume_matrix: pd.DataFrame,
    correlation_threshold: float = 0.75,
    min_active_buckets: int = 10,
    method: str = "spearman",
    fdr_alpha: float = 0.05,
) -> list[tuple[str, str, float]]:
    """Return (pair_A, pair_B, correlation_r) for pairs whose volume time
    series are correlated above `correlation_threshold`.

    Only pairs with at least `min_active_buckets` non-zero buckets are
    considered. Uses Spearman rank correlation by default to avoid false
    positives from heavy-tailed outlier trades.

    `method` must be "spearman" (default) or "pearson_winsorized". Pairwise
    significance tests are corrected with Benjamini-Hochberg at `fdr_alpha`
    before a pair is returned.
    """
    if volume_matrix.empty or volume_matrix.shape[1] < 2:
        return []
    if not 0.0 < fdr_alpha <= 1.0:
        raise ValueError("fdr_alpha must be in (0, 1]")

    active_pairs = [
        col for col in volume_matrix.columns
        if (volume_matrix[col] > 0).sum() >= min_active_buckets
    ]
    if len(active_pairs) < 2:
        return []

    mat = volume_matrix[active_pairs]
    X = mat.values.astype(np.float64)

    if method == "pearson_winsorized":
        p1 = np.percentile(X, 1, axis=0)
        p99 = np.percentile(X, 99, axis=0)
        X = np.clip(X, p1, p99)
    else:
        # Spearman via rank transform — use numpy argsort twice (avoids scipy import)
        # and a single matrix multiply instead of pandas per-column correlation.
        order = np.argsort(X, axis=0)
        ranks = np.empty_like(order, dtype=np.float64)
        np.put_along_axis(ranks, order, np.arange(1, X.shape[0] + 1, dtype=np.float64)[:, None] * np.ones((1, X.shape[1])), axis=0)
        X = ranks

    # Pearson correlation via normalised dot product (vectorised, single BLAS call)
    X -= X.mean(axis=0)
    norms = np.linalg.norm(X, axis=0)
    norms[norms == 0] = 1.0
    X /= norms
    C = X.T @ X  # shape (P, P)

    pair_indices: list[tuple[int, int]] = []
    correlations: list[float] = []
    n = len(active_pairs)
    for i in range(n):
        for j in range(i + 1, n):
            r = float(C[i, j])
            pair_indices.append((i, j))
            correlations.append(r)

    # Test every pair before correction so adding unrelated pairs cannot
    # silently inflate the number of uncorrected discoveries.
    n_observations = X.shape[0]
    correlations_array = np.asarray(correlations)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_statistics = correlations_array * np.sqrt(
            (n_observations - 2) / np.maximum(1.0 - correlations_array**2, np.finfo(float).eps)
        )
    p_values = 2.0 * student_t.sf(np.abs(t_statistics), max(n_observations - 2, 1))
    significant = _benjamini_hochberg(p_values, fdr_alpha)

    results: list[tuple[str, str, float]] = []
    for (i, j), r, is_significant in zip(pair_indices, correlations, significant):
        if r >= correlation_threshold and is_significant:
            results.append((active_pairs[i], active_pairs[j], r))

    return results


def find_cross_pair_wallets(
    trades_by_pair: dict[str, pd.DataFrame],
    correlated_pairs: list[tuple[str, str, float]],
    time_window_minutes: int = 10,
) -> dict[str, list[str]]:
    """For each correlated pair combination, find wallets that trade in both
    pairs within the same `time_window_minutes` window.

    Returns {wallet: [pair_A, pair_B, ...]} for wallets appearing in
    synchronised cross-pair volume bursts.
    """
    if not correlated_pairs:
        return {}

    wallet_pairs: dict[str, set[str]] = {}
    window = pd.Timedelta(minutes=time_window_minutes)

    for pair_a, pair_b, _ in correlated_pairs:
        df_a = trades_by_pair.get(pair_a)
        df_b = trades_by_pair.get(pair_b)
        if df_a is None or df_b is None or df_a.empty or df_b.empty:
            continue

        # Pool trades carry counter_account=None (the pool has no wallet); drop it
        # so two unrelated pairs that both trade against pools don't appear to
        # share a "wallet" called None.
        wallets_a = set(df_a["base_account"]) | set(df_a["counter_account"].dropna())
        wallets_b = set(df_b["base_account"]) | set(df_b["counter_account"].dropna())
        shared_wallets = wallets_a & wallets_b

        for wallet in shared_wallets:
            times_a = df_a[
                (df_a["base_account"] == wallet) | (df_a["counter_account"] == wallet)
            ]["ledger_close_time"].sort_values().values

            times_b = df_b[
                (df_b["base_account"] == wallet) | (df_b["counter_account"] == wallet)
            ]["ledger_close_time"].sort_values().values

            if len(times_a) == 0 or len(times_b) == 0:
                continue

            # Normalise both arrays to microsecond int64 for a unit-safe comparison.
            window_us = window.value // 1_000
            t_a_us = pd.to_datetime(times_a, utc=True).as_unit("us").asi8
            t_b_us = pd.to_datetime(times_b, utc=True).as_unit("us").asi8

            found_overlap = False
            for ta in t_a_us:
                if np.any(np.abs(t_b_us - ta) <= window_us):
                    found_overlap = True
                    break

            if found_overlap:
                if wallet not in wallet_pairs:
                    wallet_pairs[wallet] = set()
                wallet_pairs[wallet].add(pair_a)
                wallet_pairs[wallet].add(pair_b)

    return {w: sorted(pairs) for w, pairs in wallet_pairs.items()}
