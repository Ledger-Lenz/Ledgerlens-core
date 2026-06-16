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

from detection.benford_engine import compute_benford_metrics

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
]

FEATURE_NAMES = (
    BENFORD_FEATURE_NAMES + TRADE_PATTERN_FEATURE_NAMES + VOLUME_TIMING_FEATURE_NAMES + WALLET_GRAPH_FEATURE_NAMES
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
    """
    all_accounts = pd.unique(trades[["base_account", "counter_account"]].values.ravel())
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


def build_feature_vector(
    trades: pd.DataFrame,
    account: str,
    as_of: pd.Timestamp,
    order_book_events: pd.DataFrame | None = None,
    account_metadata: dict[str, dict] | None = None,
) -> dict:
    """Assemble the full feature vector for `account` as of `as_of`.

    `trades` should already be filtered to the relevant asset pair / time
    range covering the largest rolling window. `order_book_events` (from
    `ingestion.operations_loader.load_order_book_events`) and
    `account_metadata` (from `ingestion.account_loader.load_account_metadata`)
    are optional; omitting them yields `0.0` for the features that depend
    on them rather than raising.
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
    if _HAS_ADVERSARIAL:
        features.update(_compute_adv(trades, account))
    return features
