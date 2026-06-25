"""On-chain feature extraction for the wash-trading ML ensemble.

Builds the feature set described in the README's "Machine Learning Layer"
section: Benford features (per rolling window), trade pattern features,
volume/timing features, and wallet graph features. Trade input is a
`Trade`-shaped DataFrame as produced by
`ingestion.historical_loader.load_historical_trades`. Order-book and
account-metadata inputs are optional and come from
`ingestion.operations_loader` / `ingestion.account_loader` respectively.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from detection.amm_engine import pool_round_trip_ratio, pool_share_concentration
from detection.benford_engine import AdaptiveBenfordWindow, compute_benford_metrics
from detection.causal_engine import estimate_pdc  # noqa: F401
from detection.path_payment_engine import detect_atomic_circular_routes
from detection.sandwich_engine import detect_sandwich_candidates
from ingestion.data_models import LiquidityPool, PathPayment, TradeType

if TYPE_CHECKING:
    from detection.cross_chain_linker import CrossChainLinker

ROLLING_WINDOWS = {
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "24h": pd.Timedelta(hours=24),
    "7d": pd.Timedelta(days=7),
    "30d": pd.Timedelta(days=30),
}

# Hours (UTC) treated as "off hours" for off_hours_activity_ratio.
DEFAULT_OFF_HOURS = frozenset(range(0, 6))

BENFORD_FEATURE_NAMES = [
    f"benford_{metric}_{window}"
    for window in ROLLING_WINDOWS
    for metric in ("chi_square", "mad", "max_zscore")
]

# Boolean flags indicating that the corresponding Benford window was
# adaptively expanded or merged due to insufficient sample count.
BENFORD_WINDOW_EXPANDED_FEATURE_NAMES = [
    f"benford_window_expanded_{window}" for window in ROLLING_WINDOWS
]

TRADE_PATTERN_FEATURE_NAMES = [
    "counterparty_concentration_ratio",
    "round_trip_trade_frequency",
    "self_matching_rate",
    "order_cancellation_rate",
]

VOLUME_TIMING_FEATURE_NAMES = [
    "volume_to_unique_counterparty_ratio",
    "intra_minute_clustering_coefficient",
    "off_hours_activity_ratio",
    "volume_spike_frequency",
]

WALLET_GRAPH_FEATURE_NAMES = [
    "funding_source_similarity_score",
    "network_centrality",
    "account_age_days",
    "wash_ring_membership",
    "wash_ring_size",
    "cycle_volume_ratio",
    "timing_tightness_score",
]

CROSS_PAIR_FEATURE_NAMES = [
    "cross_pair_activity_count",
    "cross_pair_synchrony_score",
    "cross_pair_burst_overlap_ratio",
    "shared_wallet_cluster_size",
    "cross_pair_volume_concentration",
]

AMM_FEATURE_NAMES = [
    "pool_trade_ratio",  # fraction of an account's volume that is pool, not orderbook
    "pool_round_trip_ratio",
    "pool_share_concentration",
]

PATH_PAYMENT_FEATURE_NAMES = [
    "atomic_self_payment_ratio",  # fraction of an account's path payments where source==destination
    "avg_path_hop_count",
    "path_cycle_volume_ratio",  # fraction of volume in source-asset == destination-asset cycles
    "path_payment_frequency",   # fraction of all trades that originated from path payments
]

PATH_PAYMENT_CYCLE_FEATURE_NAMES = [
    "path_cycle_count_24h",  # closed multi-hop cycles this account is part of in 24h
    "path_cycle_xlm_volume_24h",  # total XLM value of cyclic path-payment volume in 24h
    "max_cycle_length",  # longest detected cycle (longer == more sophisticated evasion)
    "cycle_asset_diversity",  # distinct intermediate assets across this account's cycles
]

# Hop-graph cycle features from PathCycleDetector (issue #121)
HOP_CYCLE_FEATURE_NAMES = [
    "path_cycle_count",           # total confirmed round-trip cycles for this wallet
    "path_cycle_recovery_ratio",  # max recovery ratio across all cycles
]

SANDWICH_FEATURE_NAMES = [
    "sandwich_ratio",  # fraction of an account's pool trades that are attacker legs of a sandwich
    "sandwich_profit_xlm_30d",  # XLM the account extracted as a sandwich attacker over the last 30d
]

CROSS_CHAIN_FEATURE_NAMES = [
    "has_evm_link",
    "evm_round_trip_frequency",
    "evm_benford_mad_30d",
    "evm_counterparty_concentration",
    "bridge_volume_ratio",
    "cross_chain_time_lag_median_h",
]

CAUSAL_FEATURE_NAMES = [
    "pdc_5m",
    "pdc_1h",
]

MULTIVARIATE_BENFORD_FEATURE_NAMES = [
    "benford_copula_pval",
    "cross_pair_sync_ratio",
    "digit_entropy_delta",
]

FEATURE_NAMES = (
    BENFORD_FEATURE_NAMES
    + TRADE_PATTERN_FEATURE_NAMES
    + VOLUME_TIMING_FEATURE_NAMES
    + WALLET_GRAPH_FEATURE_NAMES
    + CROSS_PAIR_FEATURE_NAMES
    + AMM_FEATURE_NAMES
    + PATH_PAYMENT_FEATURE_NAMES
    + SANDWICH_FEATURE_NAMES
    + CAUSAL_FEATURE_NAMES
    + MULTIVARIATE_BENFORD_FEATURE_NAMES
    + BENFORD_WINDOW_EXPANDED_FEATURE_NAMES
)

# Adversarial meta-features are appended after the baseline features so that
# existing model checkpoints remain loadable (new features default to 0.0
# during inference against old models).
try:
    from detection.adversarial_features import ADVERSARIAL_FEATURE_NAMES, compute_adversarial_features as _compute_adv
    FEATURE_NAMES = FEATURE_NAMES + ADVERSARIAL_FEATURE_NAMES  # type: ignore[assignment]
    _HAS_ADVERSARIAL = True
except ImportError:  # pragma: no cover
    _HAS_ADVERSARIAL = False

# Cross-chain features are appended last so existing model checkpoints remain
# loadable — old models see 0.0 for these features during inference.
FEATURE_NAMES = FEATURE_NAMES + CROSS_CHAIN_FEATURE_NAMES  # type: ignore[assignment]

# Multivariate-Benford and causal (PDC) features are appended after
# cross-chain features for the same checkpoint-compatibility reason.
FEATURE_NAMES = FEATURE_NAMES + MULTIVARIATE_BENFORD_FEATURE_NAMES + CAUSAL_FEATURE_NAMES  # type: ignore[assignment]

# Hop-graph cycle features (issue #121) appended for checkpoint compatibility.
FEATURE_NAMES = FEATURE_NAMES + HOP_CYCLE_FEATURE_NAMES  # type: ignore[assignment]


def _window_slice(trades: pd.DataFrame, as_of: pd.Timestamp, window: pd.Timedelta) -> pd.DataFrame:
    start = as_of - window
    mask = (trades["ledger_close_time"] > start) & (trades["ledger_close_time"] <= as_of)
    return trades.loc[mask]


def _account_trades(trades: pd.DataFrame, account: str) -> pd.DataFrame:
    return trades[(trades["base_account"] == account) | (trades["counter_account"] == account)]


def _counterparties(account_trades: pd.DataFrame, account: str) -> pd.Series:
    return account_trades.apply(
        lambda r: r["counter_account"] if r["base_account"] == account else r["base_account"],
        axis=1,
    )


def _asset_symbol(asset: dict) -> str:
    code = asset["code"]
    issuer = asset.get("issuer")
    return code if issuer is None else f"{code}:{issuer}"


def benford_features(
    trades: pd.DataFrame,
    as_of: pd.Timestamp,
    adaptive_window: AdaptiveBenfordWindow | None = None,
) -> dict:
    """Chi-square, MAD, and max Z-score for `base_amount` across each rolling window.

    When ``adaptive_window`` is provided the window is expanded (or merged) as
    needed to reach the configured minimum sample count, and a boolean
    ``benford_window_expanded_{label}`` flag is set for each window that was
    widened beyond the nominal target.
    """
    features = {}
    for label, window in ROLLING_WINDOWS.items():
        if adaptive_window is not None:
            result = adaptive_window.fit(trades, label, as_of, ROLLING_WINDOWS)
            amounts = result.trades
            features[f"benford_window_expanded_{label}"] = float(result.expanded or result.merged)
        else:
            subset = _window_slice(trades, as_of, window)
            amounts = subset["base_amount"].tolist()
            features[f"benford_window_expanded_{label}"] = 0.0
        metrics = compute_benford_metrics(amounts)
        features[f"benford_chi_square_{label}"] = metrics["chi_square"]
        features[f"benford_mad_{label}"] = metrics["mad"]
        features[f"benford_max_zscore_{label}"] = max(metrics["z_scores"].values(), default=0.0)
    return features


def counterparty_concentration_ratio(trades: pd.DataFrame, account: str) -> float:
    """Fraction of `account`'s volume traded against its single largest counterparty."""
    account_trades = _account_trades(trades, account)
    if account_trades.empty:
        return 0.0

    counterparties = _counterparties(account_trades, account)
    volumes = account_trades["base_amount"].groupby(counterparties).sum()
    total = volumes.sum()
    return float(volumes.max() / total) if total > 0 else 0.0


def round_trip_trade_frequency(trades: pd.DataFrame, account: str, max_trades: int = 5) -> float:
    """Fraction of `account`'s trades that are reversed within the next `max_trades` trades.

    A trade is considered "reversed" if a later trade sends back the asset
    `account` just received in exchange for the asset it just gave up
    (regardless of amount) — a hallmark of circular wash-trading routes.

    Simplification: the asset `account` gives up / receives is taken as
    `base_asset`/`counter_asset` when `account` is the base side, and
    vice versa when it is the counter side (Horizon's `base_is_seller`
    flag is not consulted). This is sufficient to flag circular routing
    between the same two assets.
    """
    account_trades = _account_trades(trades, account).sort_values("ledger_close_time").reset_index(drop=True)
    n = len(account_trades)
    if n < 2:
        return 0.0

    legs = []
    for _, row in account_trades.iterrows():
        if row["base_account"] == account:
            legs.append((_asset_symbol(row["base_asset"]), _asset_symbol(row["counter_asset"])))
        else:
            legs.append((_asset_symbol(row["counter_asset"]), _asset_symbol(row["base_asset"])))

    round_trips = 0
    for i in range(n):
        gave_i, got_i = legs[i]
        for j in range(i + 1, min(i + 1 + max_trades, n)):
            gave_j, got_j = legs[j]
            if gave_j == got_i and got_j == gave_i:
                round_trips += 1
                break

    return round_trips / n


def self_matching_rate(trades: pd.DataFrame) -> float:
    """Fraction of trades where the base and counter accounts are identical."""
    if trades.empty:
        return 0.0
    return float((trades["base_account"] == trades["counter_account"]).mean())


def order_cancellation_rate(events: pd.DataFrame, account: str) -> float:
    """Fraction of `account`'s order-book events (see `OrderBookEvent`) that are cancellations."""
    if events.empty:
        return 0.0
    account_events = events[events["account"] == account]
    if account_events.empty:
        return 0.0
    return float((account_events["event_type"] == "cancelled").mean())


def volume_to_unique_counterparty_ratio(trades: pd.DataFrame, account: str) -> float:
    """Total volume for `account` divided by the number of distinct counterparties."""
    account_trades = _account_trades(trades, account)
    if account_trades.empty:
        return 0.0
    counterparties = _counterparties(account_trades, account)
    unique_counterparties = counterparties.nunique()
    total_volume = account_trades["base_amount"].sum()
    return float(total_volume / unique_counterparties) if unique_counterparties else 0.0


def intra_minute_clustering_coefficient(trades: pd.DataFrame) -> float:
    """Fraction of trades that share the same calendar minute as another trade."""
    if trades.empty:
        return 0.0
    minute_buckets = trades["ledger_close_time"].dt.floor("min")
    counts = minute_buckets.value_counts()
    clustered = counts[counts > 1].sum()
    return float(clustered / len(trades))


def off_hours_activity_ratio(trades: pd.DataFrame, off_hours: frozenset[int] = DEFAULT_OFF_HOURS) -> float:
    """Fraction of trades occurring during `off_hours` (UTC hour-of-day, default 00:00-05:59)."""
    if trades.empty:
        return 0.0
    hours = trades["ledger_close_time"].dt.hour
    return float(hours.isin(off_hours).mean())


def volume_spike_frequency(
    trades: pd.DataFrame,
    as_of: pd.Timestamp,
    bucket: str = "1h",
    baseline_window: pd.Timedelta = ROLLING_WINDOWS["24h"],
    spike_threshold: float = 2.0,
) -> float:
    """Fraction of `bucket`-sized time buckets within `baseline_window` whose volume
    exceeds the mean bucket volume by more than `spike_threshold` standard deviations.
    """
    subset = _window_slice(trades, as_of, baseline_window)
    if subset.empty:
        return 0.0

    bucketed = subset.set_index("ledger_close_time")["base_amount"].resample(bucket).sum()
    if len(bucketed) < 2 or bucketed.std() == 0:
        return 0.0

    threshold = bucketed.mean() + spike_threshold * bucketed.std()
    return float((bucketed > threshold).mean())


def funding_source_similarity_score(trades: pd.DataFrame, account: str, account_metadata: dict[str, dict]) -> float:
    """Fraction of `account`'s counterparties funded by the same source account as `account`.

    `account_metadata` maps account -> `{"funding_source": str | None, "created_at": datetime | None}`,
    as returned by `ingestion.account_loader.load_account_metadata`.
    """
    funding_source = account_metadata.get(account, {}).get("funding_source")
    if funding_source is None:
        return 0.0

    account_trades = _account_trades(trades, account)
    if account_trades.empty:
        return 0.0

    counterparties = _counterparties(account_trades, account)
    matches = counterparties.map(lambda cp: account_metadata.get(cp, {}).get("funding_source") == funding_source)
    return float(matches.mean())


def network_centrality(trades: pd.DataFrame, account: str) -> float:
    """Degree centrality of `account` within the trading graph induced by `trades`.

    Defined as `account`'s unique counterparties divided by `(total accounts - 1)`.
    Pool trades have no counterparty wallet (`counter_account` is `None`) and
    are excluded from the account universe rather than counted as a node.
    """
    all_accounts = pd.unique(trades[["base_account", "counter_account"]].values.ravel())
    all_accounts = all_accounts[pd.notna(all_accounts)]
    if len(all_accounts) <= 1:
        return 0.0

    account_trades = _account_trades(trades, account)
    if account_trades.empty:
        return 0.0

    unique_counterparties = _counterparties(account_trades, account).nunique()
    return float(unique_counterparties / (len(all_accounts) - 1))


def account_age_days(account: str, as_of: pd.Timestamp, account_metadata: dict[str, dict]) -> float:
    """Age of `account` in days as of `as_of`, or `0.0` if its creation time is unknown."""
    created_at = account_metadata.get(account, {}).get("created_at")
    if created_at is None:
        return 0.0
    return float((as_of - pd.Timestamp(created_at)).total_seconds() / 86400)


def graph_ring_features(account: str, ring_membership: dict[str, dict] | None) -> dict:
    """Compute graph-structural wash-ring features for `account`."""
    zero = {
        "wash_ring_membership": 0.0,
        "wash_ring_size": 0.0,
        "cycle_volume_ratio": 0.0,
        "timing_tightness_score": 0.0,
    }
    if not ring_membership:
        return zero

    metadata = ring_membership.get(account)
    if metadata is None:
        return zero

    return {
        "wash_ring_membership": 1.0,
        "wash_ring_size": float(metadata.get("wash_ring_size", metadata.get("ring_size", 0.0))),
        "cycle_volume_ratio": float(metadata.get("cycle_volume_ratio", 0.0)),
        "timing_tightness_score": float(metadata.get("timing_tightness_score", 0.0)),
    }


def cross_pair_features(
    account: str,
    trades_by_pair: dict[str, pd.DataFrame] | None,
    correlated_pairs: list[tuple[str, str, float]] | None,
    cross_pair_wallets: dict[str, list[str]] | None,
) -> dict:
    """Compute the five cross-pair features for `account`.

    All inputs are optional; omitting any yields 0.0 for all five features.
    """
    zero = {name: 0.0 for name in CROSS_PAIR_FEATURE_NAMES}

    if not trades_by_pair or not correlated_pairs or not cross_pair_wallets:
        return zero

    wallet_active_pairs = cross_pair_wallets.get(account)
    if not wallet_active_pairs:
        return zero

    active_count = len(wallet_active_pairs)

    # Maximum Spearman r between the wallet's two most-correlated pairs
    pair_set = set(wallet_active_pairs)
    max_r = 0.0
    for pa, pb, r in correlated_pairs:
        if pa in pair_set and pb in pair_set:
            max_r = max(max_r, r)

    # Fraction of wallet trades that fall in cross-pair burst windows (10-min)
    total_trades = 0
    burst_trades = 0
    window = pd.Timedelta(minutes=10)
    window_us = window.value // 1_000  # microseconds
    for pair in wallet_active_pairs:
        df = trades_by_pair.get(pair, pd.DataFrame())
        if df.empty:
            continue
        acct_df = df[(df["base_account"] == account) | (df["counter_account"] == account)]
        total_trades += len(acct_df)
        other_pairs = [p for p in wallet_active_pairs if p != pair]
        for other_pair in other_pairs:
            df_other = trades_by_pair.get(other_pair, pd.DataFrame())
            if df_other.empty:
                continue
            times_other_us = pd.to_datetime(
                df_other["ledger_close_time"].values, utc=True
            ).as_unit("us").asi8
            for _, row in acct_df.iterrows():
                t_us = int(pd.Timestamp(row["ledger_close_time"]).value // 1_000)
                if any(abs(int(to) - t_us) <= window_us for to in times_other_us):
                    burst_trades += 1
                    break

    burst_overlap = burst_trades / total_trades if total_trades > 0 else 0.0

    # Number of other wallets in the same correlated burst cluster
    shared_cluster_size = sum(
        1 for w, w_pairs in cross_pair_wallets.items()
        if w != account and set(w_pairs) & pair_set
    )

    # Wallet's fraction of total cross-pair burst volume
    wallet_burst_vol = 0.0
    total_burst_vol = 0.0
    for pair in wallet_active_pairs:
        df = trades_by_pair.get(pair, pd.DataFrame())
        if df.empty:
            continue
        acct_vol = df[
            (df["base_account"] == account) | (df["counter_account"] == account)
        ]["base_amount"].sum()
        total_vol = df["base_amount"].sum()
        wallet_burst_vol += acct_vol
        total_burst_vol += total_vol

    vol_concentration = wallet_burst_vol / total_burst_vol if total_burst_vol > 0 else 0.0

    return {
        "cross_pair_activity_count": float(active_count),
        "cross_pair_synchrony_score": float(max_r),
        "cross_pair_burst_overlap_ratio": float(burst_overlap),
        "shared_wallet_cluster_size": float(shared_cluster_size),
        "cross_pair_volume_concentration": float(vol_concentration),
    }


def amm_features(
    trades: pd.DataFrame,
    account: str,
    liquidity_pools: dict[str, LiquidityPool] | None = None,
    pool_deposits: dict[str, pd.DataFrame] | None = None,
) -> dict:
    """Compute the three AMM pool features for `account`.

    `pool_trade_ratio` and `pool_round_trip_ratio` are derived from `trades`
    alone (rows with `trade_type == LIQUIDITY_POOL`). `pool_share_concentration`
    additionally needs `liquidity_pools` (id -> `LiquidityPool`) and
    `pool_deposits` (id -> deposit/withdraw DataFrame); omitting either yields
    `0.0` for that feature.
    """
    zero = {name: 0.0 for name in AMM_FEATURE_NAMES}
    if trades.empty or "trade_type" not in trades.columns:
        return zero

    account_trades = _account_trades(trades, account)
    if account_trades.empty:
        return zero

    pool_trades = account_trades.loc[account_trades["trade_type"] == TradeType.LIQUIDITY_POOL]
    if pool_trades.empty:
        return zero

    total_volume = account_trades["base_amount"].sum()
    pool_trade_ratio = float(pool_trades["base_amount"].sum() / total_volume) if total_volume > 0 else 0.0

    pool_ids = [pid for pid in pool_trades["liquidity_pool_id"].dropna().unique()]
    round_trip_ratios = [pool_round_trip_ratio(trades, account, pid) for pid in pool_ids]
    avg_round_trip = float(sum(round_trip_ratios) / len(round_trip_ratios)) if round_trip_ratios else 0.0

    concentrations = []
    if liquidity_pools and pool_deposits:
        for pid in pool_ids:
            pool = liquidity_pools.get(pid)
            deposits = pool_deposits.get(pid)
            if pool is not None and deposits is not None:
                concentrations.append(pool_share_concentration(pool, deposits))
    avg_concentration = float(sum(concentrations) / len(concentrations)) if concentrations else 0.0

    return {
        "pool_trade_ratio": pool_trade_ratio,
        "pool_round_trip_ratio": avg_round_trip,
        "pool_share_concentration": avg_concentration,
    }


def path_payment_features(path_payments: list[PathPayment] | None, account: str, trades: "pd.DataFrame | None" = None) -> dict:
    """Compute the path-payment features for `account`'s own payments
    (`source_account == account`). Omitting `path_payments` yields `0.0`.

    Args:
        path_payments: List of PathPayment records for the account.
        account: The wallet address being scored.
        trades: Optional DataFrame of all trades for the account, used to
            compute ``path_payment_frequency`` (fraction of trades from path payments).
    """
    zero = {name: 0.0 for name in PATH_PAYMENT_FEATURE_NAMES}
    if not path_payments:
        return zero

    own_payments = [p for p in path_payments if p.source_account == account]
    if not own_payments:
        return zero

    routes = detect_atomic_circular_routes(own_payments)
    self_payment_count = sum(1 for r in routes if r["is_atomic_self_payment"])
    cycle_volume = sum(r["cycle_volume"] for r in routes if not r["is_atomic_self_payment"])

    total_volume = sum(p.source_amount for p in own_payments)
    hop_counts = [len(p.path) + 1 for p in own_payments]

    # path_payment_frequency: fraction of all trades that came from path payments
    pp_frequency = 0.0
    if trades is not None and len(trades) > 0:
        pp_col = "path_payment_id"
        if pp_col in trades.columns:
            pp_count = trades[pp_col].notna().sum()
            pp_frequency = float(pp_count / len(trades))

    return {
        "atomic_self_payment_ratio": float(self_payment_count / len(own_payments)),
        "avg_path_hop_count": float(sum(hop_counts) / len(hop_counts)),
        "path_cycle_volume_ratio": float(cycle_volume / total_volume) if total_volume > 0 else 0.0,
        "path_payment_frequency": pp_frequency,
    }


def path_payment_cycle_features(
    path_payments: list[PathPayment] | None,
    path_cycles: list[dict] | None,
    account: str,
) -> dict:
    """Compute the four multi-hop path-payment cycle features for `account`.

    `path_cycles` is the batch-level output of
    `detection.path_cycle_detector.detect_path_payment_cycles`. When it is not
    supplied but `path_payments` are, the cycle search is run on demand; passing
    precomputed cycles avoids re-running Johnson's algorithm per account.
    """
    from detection.path_cycle_detector import detect_cycles_from_payments, path_cycle_features

    zero = {name: 0.0 for name in PATH_PAYMENT_CYCLE_FEATURE_NAMES}
    if path_cycles is None:
        if not path_payments:
            return zero
        path_cycles = detect_cycles_from_payments(path_payments)

    return path_cycle_features(path_cycles, account)


def sandwich_features(
    trades: pd.DataFrame,
    account: str,
    as_of: pd.Timestamp,
    min_profit_xlm: float = 10.0,
) -> dict:
    """Compute wallet-level sandwich-aggressor features for `account`.

    `sandwich_ratio` is the share of `account`'s pool trades that are attacker
    legs (buy + sell) of a detected sandwich. `sandwich_profit_xlm_30d` is the
    XLM profit `account` extracted as the attacker across sandwiches whose
    opening buy falls in the 30 days ending at `as_of`.
    """
    zero = {name: 0.0 for name in SANDWICH_FEATURE_NAMES}
    if trades.empty or "trade_type" not in trades.columns or "price" not in trades.columns:
        return zero

    pool_trades = trades.loc[trades["trade_type"] == TradeType.LIQUIDITY_POOL]
    if pool_trades.empty:
        return zero

    window = _window_slice(pool_trades, as_of, ROLLING_WINDOWS["30d"])
    if window.empty:
        return zero

    candidates = detect_sandwich_candidates(window, min_profit_xlm=min_profit_xlm)
    own = [c for c in candidates if c.attacker == account]
    if not own:
        return zero

    account_pool_trades = pool_trades[pool_trades["base_account"] == account]
    denom = len(account_pool_trades)
    # each sandwich contributes two attacker legs (buy + sell)
    ratio = min(2 * len(own) / denom, 1.0) if denom > 0 else 0.0
    profit = sum(c.profit_xlm for c in own)

    return {
        "sandwich_ratio": float(ratio),
        "sandwich_profit_xlm_30d": float(profit),
    }


def build_cross_chain_features(
    wallet: str,
    linker: "CrossChainLinker",  # noqa: F821
    sdex_volume: float = 0.0,
    evm_trades: list[dict] | None = None,
) -> dict:
    """Compute the six cross-chain features for ``wallet``.

    ``linker`` is a :class:`detection.cross_chain_linker.CrossChainLinker`
    instance.  ``sdex_volume`` is the total SDEX trade volume for the wallet
    (used to compute ``bridge_volume_ratio``).  ``evm_trades`` is an optional
    list of EVM trade dicts (same schema as ``CrossChainTrade.model_dump()``).
    """
    zero: dict = {name: 0.0 for name in CROSS_CHAIN_FEATURE_NAMES}

    evm_wallets = linker.link_wallets(wallet)
    if not evm_wallets:
        return zero

    pattern = linker.get_evm_trade_pattern(
        evm_wallets, chain="ethereum", evm_trades=evm_trades or []
    )

    evm_volume = pattern.get("total_evm_volume", 0.0)
    bridge_volume_ratio = evm_volume / sdex_volume if sdex_volume > 0 else 0.0

    unique_cp = pattern.get("unique_counterparties", 0)
    if unique_cp > 0 and evm_trades:
        wallet_trades = [
            t for t in (evm_trades or []) if t.get("wallet_address") in set(evm_wallets)
        ]
        from collections import Counter
        cp_counts = Counter(t.get("counterparty", "") for t in wallet_trades if t.get("counterparty"))
        total = sum(cp_counts.values())
        hhi = sum((c / total) ** 2 for c in cp_counts.values()) if total > 0 else 0.0
    else:
        hhi = 0.0

    return {
        "has_evm_link": 1.0,
        "evm_round_trip_frequency": float(pattern.get("round_trip_frequency", 0.0)),
        "evm_benford_mad_30d": float(pattern.get("benford_mad", 0.0)),
        "evm_counterparty_concentration": float(hhi),
        "bridge_volume_ratio": float(bridge_volume_ratio),
        "cross_chain_time_lag_median_h": 0.0,
    }


def multivariate_benford_features(account: str, trades_by_pair: dict[str, pd.DataFrame] | None) -> dict:
    """Cross-pair Benford coordination features for `account`.

    `trades_by_pair` maps asset-pair label -> trades DataFrame (each row must
    carry an `asset_pair` column; see
    `detection.benford_engine.multivariate_benford_score`). Omitting it (or
    passing fewer than 2 pairs) yields the i.i.d.-Benford defaults.
    """
    zero = {name: (1.0 if name == "benford_copula_pval" else 0.0) for name in MULTIVARIATE_BENFORD_FEATURE_NAMES}
    if not trades_by_pair or len(trades_by_pair) < 2:
        return zero

    from detection.benford_engine import multivariate_benford_score

    wallet_pairs = [(account, pair) for pair in trades_by_pair]
    combined = pd.concat(trades_by_pair.values(), ignore_index=True)
    result = multivariate_benford_score(combined, wallet_pairs)
    return {
        "benford_copula_pval": result["copula_pval"],
        "cross_pair_sync_ratio": result["sync_ratio"],
        "digit_entropy_delta": result["digit_entropy_delta"],
    }


def causal_features(
    trades: pd.DataFrame, account: str, prices: pd.DataFrame | None, pair: str | None
) -> dict:
    """Price-discovery-contribution (PDC) features for `account` on `pair`.

    `prices` is a `timestamp` + `mid_price`/`price` series for `pair`;
    omitting it (or `pair`) yields `0.0` for both windows.
    """
    if prices is None or pair is None:
        return {name: 0.0 for name in CAUSAL_FEATURE_NAMES}
    return {
        "pdc_5m": estimate_pdc(trades, prices, account, pair, window_minutes=5),
        "pdc_1h": estimate_pdc(trades, prices, account, pair, window_minutes=60),
    }


def _build_feature_vector_base(
    trades: pd.DataFrame,
    account: str,
    as_of: pd.Timestamp,
    order_book_events: pd.DataFrame | None = None,
    account_metadata: dict[str, dict] | None = None,
    trades_by_pair: dict[str, pd.DataFrame] | None = None,
    correlated_pairs: list[tuple[str, str, float]] | None = None,
    cross_pair_wallets: dict[str, list[str]] | None = None,
    liquidity_pools: dict[str, LiquidityPool] | None = None,
    pool_deposits: dict[str, pd.DataFrame] | None = None,
    path_payments: list[PathPayment] | None = None,
    path_cycles: list[dict] | None = None,
    ring_membership: dict[str, dict] | None = None,
    prices: pd.DataFrame | None = None,
    pair: str | None = None,
    cross_chain_linker: "CrossChainLinker | None" = None,  # noqa: F821
    adaptive_benford_window: AdaptiveBenfordWindow | None = None,
) -> dict:
    """Assemble the full feature vector for `account` as of `as_of`.

    `trades` should already be filtered to the relevant asset pair / time
    range covering the largest rolling window, and may include AMM pool
    trades (`trade_type == LIQUIDITY_POOL`, `counter_account is None`)
    alongside order-book trades. `order_book_events` (from
    `ingestion.operations_loader.load_order_book_events_for_pair`) and
    `account_metadata` (from `ingestion.account_loader.load_account_metadata`)
    are optional; omitting them yields `0.0` for the features that depend
    on them rather than raising.

    `trades_by_pair`, `correlated_pairs`, and `cross_pair_wallets` are
    produced by the cross-pair engine and are optional; omitting them yields
    `0.0` for all five cross-pair features.

    `liquidity_pools`, `pool_deposits`, and `path_payments` are optional;
    omitting them yields `0.0` for the AMM/path-payment features that depend
    on them.

    `prices` (a `timestamp` + `mid_price`/`price` series for the pair) and
    `pair` drive the causal PDC features; omitting `prices` yields `0.0` for
    `pdc_5m`/`pdc_1h`.
    """
    order_book_events = order_book_events if order_book_events is not None else pd.DataFrame(columns=["account", "event_type"])
    account_metadata = account_metadata or {}

    features = benford_features(trades, as_of, adaptive_window=adaptive_benford_window)
    features.update(
        {
            "counterparty_concentration_ratio": counterparty_concentration_ratio(trades, account),
            "round_trip_trade_frequency": round_trip_trade_frequency(trades, account),
            "self_matching_rate": self_matching_rate(trades),
            "order_cancellation_rate": order_cancellation_rate(order_book_events, account),
            "volume_to_unique_counterparty_ratio": volume_to_unique_counterparty_ratio(trades, account),
            "intra_minute_clustering_coefficient": intra_minute_clustering_coefficient(trades),
            "off_hours_activity_ratio": off_hours_activity_ratio(trades),
            "volume_spike_frequency": volume_spike_frequency(trades, as_of),
            "funding_source_similarity_score": funding_source_similarity_score(trades, account, account_metadata),
            "network_centrality": network_centrality(trades, account),
            "account_age_days": account_age_days(account, as_of, account_metadata),
        }
    )
    features.update(graph_ring_features(account, ring_membership))
    features.update(cross_pair_features(account, trades_by_pair, correlated_pairs, cross_pair_wallets))
    features.update(amm_features(trades, account, liquidity_pools, pool_deposits))
    features.update(path_payment_features(path_payments, account, trades))
    features.update(path_payment_cycle_features(path_payments, path_cycles, account))
    features.update(causal_features(trades, account, prices, pair))
    features.update(multivariate_benford_features(account, trades_by_pair))
    features.update(sandwich_features(trades, account, as_of))
    if _HAS_ADVERSARIAL:
        features.update(_compute_adv(trades, account))

    if cross_chain_linker is not None:
        sdex_volume = float(_account_trades(trades, account)["base_amount"].sum()) if not trades.empty else 0.0
        features.update(build_cross_chain_features(account, cross_chain_linker, sdex_volume=sdex_volume))
    else:
        features.update({name: 0.0 for name in CROSS_CHAIN_FEATURE_NAMES})

    features.update(multivariate_benford_features(account, trades_by_pair))
    features.update(causal_features(trades, account, prices, pair))

    return features


# --- T-GNN features (appended after PATH_PAYMENT_FEATURE_NAMES so existing
# model checkpoints trained without these stay loadable by index/order). ---
GNN_FEATURE_NAMES = [
    "gnn_wash_ring_probability",
    "gnn_neighbor_avg_score",
]

# Path-payment cycle features are appended after GNN features so that existing
# model checkpoints stay loadable (old models default these to 0.0 at inference).
FEATURE_NAMES = FEATURE_NAMES + GNN_FEATURE_NAMES + PATH_PAYMENT_CYCLE_FEATURE_NAMES


def build_feature_vector(*args, use_gnn: bool = False, gnn_features: dict = None, **kwargs):
    """Wraps the base feature vector builder, optionally appending GNN features.

    Args:
        use_gnn: If True, appends gnn_wash_ring_probability and
            gnn_neighbor_avg_score (default 0.0 if not found in gnn_features).
        gnn_features: Optional {wallet: {feature_name: value}} lookup.
        adaptive_benford_window: Optional AdaptiveBenfordWindow instance. When
            provided, Benford windows are expanded as needed to meet the
            minimum sample count; ``benford_window_expanded_{label}`` flags are
            set accordingly.
    """
    vector = _build_feature_vector_base(*args, **kwargs)
    if use_gnn:
        wallet = kwargs.get("wallet") or (args[0] if args else None)
        feats = (gnn_features or {}).get(wallet, {})
        vector = list(vector) + [
            float(feats.get("gnn_wash_ring_probability", 0.0)),
            float(feats.get("gnn_neighbor_avg_score", 0.0)),
        ]
    return vector


class FeatureEngineering:
    """Thin wrapper around the module-level feature functions.

    Provides :meth:`compute_incremental` for the streaming path, which takes
    per-wallet rolling-window trade lists (already scoped to 1h/4h/24h) and
    builds the full feature vector without requiring a global DataFrame.
    """

    def compute_incremental(
        self,
        wallet: str,
        trades_1h: list,
        trades_4h: list,
        trades_24h: list,
    ) -> dict:
        """Compute all features from rolling-window trade lists for *wallet*.

        Benford features are computed over each window independently.
        Graph and cross-pair features use 24-h trades only.
        Returns a ``{feature_name: float}`` dict keyed to :data:`FEATURE_NAMES`.
        """
        import pandas as pd

        def _to_df(trades: list) -> pd.DataFrame:
            if not trades:
                return pd.DataFrame()
            rows = [t.model_dump() for t in trades]
            df = pd.DataFrame(rows)
            df["ledger_close_time"] = pd.to_datetime(df["ledger_close_time"], utc=True)
            # Flatten nested asset dicts
            if "base_asset" in df.columns and isinstance(df["base_asset"].iloc[0], dict):
                df["base_asset"] = df["base_asset"].apply(lambda d: d)  # keep as dict for _asset_symbol
            return df

        df_1h = _to_df(trades_1h)
        df_4h = _to_df(trades_4h)
        df_24h = _to_df(trades_24h)
        as_of = pd.Timestamp.now(tz="UTC")

        features: dict = {}

        # Benford features per window
        for label, df in [("1h", df_1h), ("4h", df_4h), ("24h", df_24h)]:
            if df.empty:
                for metric in ("chi_square", "mad", "max_zscore"):
                    features[f"benford_{metric}_{label}"] = 0.0
            else:
                from detection.benford_engine import compute_benford_metrics
                metrics = compute_benford_metrics(df["base_amount"].tolist())
                features[f"benford_chi_square_{label}"] = metrics["chi_square"]
                features[f"benford_mad_{label}"] = metrics["mad"]
                features[f"benford_max_zscore_{label}"] = max(metrics["z_scores"].values(), default=0.0)

        # Remaining windows (7d, 30d) fallback to 0 — no data available in streaming
        for label in ("7d", "30d"):
            for metric in ("chi_square", "mad", "max_zscore"):
                features[f"benford_{metric}_{label}"] = 0.0

        # Trade-pattern and volume/timing features (use 24h window)
        if not df_24h.empty:
            features["counterparty_concentration_ratio"] = counterparty_concentration_ratio(df_24h, wallet)
            features["round_trip_trade_frequency"] = round_trip_trade_frequency(df_24h, wallet)
            features["self_matching_rate"] = self_matching_rate(df_24h)
            features["order_cancellation_rate"] = 0.0
            features["volume_to_unique_counterparty_ratio"] = volume_to_unique_counterparty_ratio(df_24h, wallet)
            features["intra_minute_clustering_coefficient"] = intra_minute_clustering_coefficient(df_24h)
            features["off_hours_activity_ratio"] = off_hours_activity_ratio(df_24h)
            features["volume_spike_frequency"] = volume_spike_frequency(df_24h, as_of)
        else:
            for name in TRADE_PATTERN_FEATURE_NAMES + VOLUME_TIMING_FEATURE_NAMES:
                features[name] = 0.0

        # Graph/wallet features: no metadata available in streaming path
        features["funding_source_similarity_score"] = 0.0
        features["network_centrality"] = 0.0
        features["account_age_days"] = 0.0
        features.update(graph_ring_features(wallet, None))

        # Cross-pair, AMM, path-payment, sandwich — not available incrementally
        for name in CROSS_PAIR_FEATURE_NAMES:
            features[name] = 0.0
        for name in AMM_FEATURE_NAMES:
            features[name] = 0.0
        for name in PATH_PAYMENT_FEATURE_NAMES:
            features[name] = 0.0
        for name in PATH_PAYMENT_CYCLE_FEATURE_NAMES:
            features[name] = 0.0
        for name in SANDWICH_FEATURE_NAMES:
            features[name] = 0.0
        for name in CAUSAL_FEATURE_NAMES:
            features[name] = 0.0
        for name in MULTIVARIATE_BENFORD_FEATURE_NAMES:
            features[name] = 1.0 if name == "benford_copula_pval" else 0.0
        if _HAS_ADVERSARIAL:
            from detection.adversarial_features import ADVERSARIAL_FEATURE_NAMES
            for name in ADVERSARIAL_FEATURE_NAMES:
                features[name] = 0.0
        for name in CROSS_CHAIN_FEATURE_NAMES:
            features[name] = 0.0
        for name in GNN_FEATURE_NAMES:
            features[name] = 0.0

        # Ensure every expected feature key is present
        for name in FEATURE_NAMES:
            features.setdefault(name, 0.0)

        return features
