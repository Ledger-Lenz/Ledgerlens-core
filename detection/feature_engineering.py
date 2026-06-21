"""On-chain feature extraction for the wash-trading ML ensemble.

Builds the feature set described in the README's "Machine Learning Layer"
section: Benford features (per rolling window), trade pattern features,
volume/timing features, and wallet graph features. Trade input is a
`Trade`-shaped DataFrame as produced by
`ingestion.historical_loader.load_historical_trades`. Order-book and
account-metadata inputs are optional and come from
`ingestion.operations_loader` / `ingestion.account_loader` respectively.
"""

import pandas as pd

from detection.amm_engine import pool_round_trip_ratio, pool_share_concentration
from detection.benford_engine import compute_benford_metrics
from detection.causal_engine import estimate_pdc
from detection.path_payment_engine import detect_atomic_circular_routes
from ingestion.data_models import LiquidityPool, PathPayment, TradeType

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
]

CAUSAL_FEATURE_NAMES = [
    "pdc_5m",  # price discovery contribution (ATE) over 5-minute windows
    "pdc_1h",  # price discovery contribution (ATE) over 1-hour windows
]

FEATURE_NAMES = (
    BENFORD_FEATURE_NAMES
    + TRADE_PATTERN_FEATURE_NAMES
    + VOLUME_TIMING_FEATURE_NAMES
    + WALLET_GRAPH_FEATURE_NAMES
    + CROSS_PAIR_FEATURE_NAMES
    + AMM_FEATURE_NAMES
    + PATH_PAYMENT_FEATURE_NAMES
    + CAUSAL_FEATURE_NAMES
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


def benford_features(trades: pd.DataFrame, as_of: pd.Timestamp) -> dict:
    """Chi-square, MAD, and max Z-score for `base_amount` across each rolling window."""
    features = {}
    for label, window in ROLLING_WINDOWS.items():
        subset = _window_slice(trades, as_of, window)
        metrics = compute_benford_metrics(subset["base_amount"].tolist())
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


def path_payment_features(path_payments: list[PathPayment] | None, account: str) -> dict:
    """Compute the three path-payment features for `account`'s own payments
    (`source_account == account`). Omitting `path_payments` yields `0.0`.
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

    return {
        "atomic_self_payment_ratio": float(self_payment_count / len(own_payments)),
        "avg_path_hop_count": float(sum(hop_counts) / len(hop_counts)),
        "path_cycle_volume_ratio": float(cycle_volume / total_volume) if total_volume > 0 else 0.0,
    }


def causal_features(
    trades: pd.DataFrame,
    account: str,
    prices: pd.DataFrame | None = None,
    pair: str | None = None,
) -> dict:
    """Compute the price-discovery-contribution (PDC) features for `account`.

    PDC is the doubly-robust treatment effect of the wallet's trades on the
    subsequent mid-price (see `detection.causal_engine.estimate_pdc`): positive
    for market makers, near-zero/negative for wash traders. Requires a `prices`
    time series (`timestamp` + `mid_price`/`price`); omitting it yields `0.0`
    for both features, so existing callers are unaffected.
    """
    zero = {name: 0.0 for name in CAUSAL_FEATURE_NAMES}
    if prices is None or getattr(prices, "empty", True) or trades.empty:
        return zero

    return {
        "pdc_5m": float(estimate_pdc(trades, prices, account, pair, window_minutes=5)),
        "pdc_1h": float(estimate_pdc(trades, prices, account, pair, window_minutes=60)),
    }


def build_feature_vector(
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
    ring_membership: dict[str, dict] | None = None,
    prices: pd.DataFrame | None = None,
    pair: str | None = None,
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

    features = benford_features(trades, as_of)
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
    features.update(path_payment_features(path_payments, account))
    features.update(causal_features(trades, account, prices, pair))
    if _HAS_ADVERSARIAL:
        features.update(_compute_adv(trades, account))
    return features
