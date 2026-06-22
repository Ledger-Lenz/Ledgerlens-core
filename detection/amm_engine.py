"""AMM liquidity-pool manipulation features.

A swap against a pool has no counterparty wallet, so the classic
counterparty-concentration / round-trip features in
`detection.feature_engineering` can't see pool-routed wash volume. These
functions operate on `Trade` rows with `trade_type=LIQUIDITY_POOL` (see
`ingestion.data_models.TradeType`) instead.
"""

import pandas as pd

from detection.sandwich_engine import detect_sandwich_candidates
from ingestion.data_models import LiquidityPool, TradeType


def _pair_key(row: pd.Series) -> tuple:
    base = row["base_asset"]
    counter = row["counter_asset"]
    return (base.get("code"), base.get("issuer"), counter.get("code"), counter.get("issuer"))


def pool_round_trip_ratio(
    trades: pd.DataFrame,
    account: str,
    pool_id: str,
    window: pd.Timedelta = pd.Timedelta(hours=1),
) -> float:
    """Fraction of an account's pool trades that are a buy followed by a sell
    of the same asset pair within `window` — a proxy for using pool swaps to
    manufacture volume without real price exposure.
    """
    if trades.empty or "trade_type" not in trades.columns:
        return 0.0

    mask = (
        (trades["trade_type"] == TradeType.LIQUIDITY_POOL)
        & (trades["liquidity_pool_id"] == pool_id)
        & (trades["base_account"] == account)
    )
    pool_trades = trades.loc[mask].sort_values("ledger_close_time").reset_index(drop=True)
    n = len(pool_trades)
    if n < 2:
        return 0.0

    round_trips = 0
    for i in range(n):
        row_i = pool_trades.iloc[i]
        pair_i = _pair_key(row_i)
        window_end = row_i["ledger_close_time"] + window
        later = pool_trades.iloc[i + 1 :]
        later = later[later["ledger_close_time"] <= window_end]
        for _, row_j in later.iterrows():
            if _pair_key(row_j) == pair_i and row_j["base_is_seller"] != row_i["base_is_seller"]:
                round_trips += 1
                break

    return round_trips / n


def pool_sandwich_count(
    trades: pd.DataFrame,
    pool_id: str,
    min_profit_xlm: float = 10.0,
    max_ledger_gap: int = 2,
) -> int:
    """Number of sandwich-attack candidates detected against `pool_id`.

    Operates on the same `Trade`-shaped DataFrame as `pool_round_trip_ratio`
    (rows with `trade_type == LIQUIDITY_POOL`). Returns 0 when the pool has no
    trades or the schema lacks the price/direction columns the detector needs.
    """
    if trades.empty or "liquidity_pool_id" not in trades.columns:
        return 0

    pool_trades = trades.loc[trades["liquidity_pool_id"] == pool_id]
    if pool_trades.empty:
        return 0

    return len(
        detect_sandwich_candidates(
            pool_trades,
            min_profit_xlm=min_profit_xlm,
            max_ledger_gap=max_ledger_gap,
        )
    )


def pool_sandwich_frequency(
    trades: pd.DataFrame,
    pool_id: str,
    min_profit_xlm: float = 10.0,
    max_ledger_gap: int = 2,
) -> float:
    """Fraction of `pool_id`'s trades that participate in a detected sandwich.

    Each candidate consumes three trade legs (buy, victim, sell); the ratio is
    `3 * candidate_count / pool_trade_count`, clamped to 1.0. A pool-level
    proxy for how heavily a pool is being sandwiched.
    """
    if trades.empty or "liquidity_pool_id" not in trades.columns:
        return 0.0

    pool_trades = trades.loc[trades["liquidity_pool_id"] == pool_id]
    n = len(pool_trades)
    if n == 0:
        return 0.0

    count = pool_sandwich_count(trades, pool_id, min_profit_xlm, max_ledger_gap)
    return float(min(3 * count / n, 1.0))


def pool_share_concentration(pool: LiquidityPool, deposits: pd.DataFrame) -> float:
    """Herfindahl-style concentration of `pool`'s deposit/withdraw activity
    across accounts — flags a single actor inflating then draining a pool to
    move its price around their own trades.

    `deposits` must have `account` and `amount` columns.
    """
    if deposits.empty:
        return 0.0

    volumes = deposits.groupby("account")["amount"].sum().abs()
    total = volumes.sum()
    if total <= 0:
        return 0.0

    shares = volumes / total
    return float((shares**2).sum())


def pool_risk_from_trade_rows(rows: list[dict], window: pd.Timedelta = pd.Timedelta(hours=1)) -> dict:
    """Aggregate round-trip ratio and trader concentration from stored pool
    trade rows (`detection.storage.get_liquidity_pool_trades`'s shape:
    `base_account`, `base_asset_pair`, `counter_asset_pair`, `base_amount`,
    `base_is_seller`, `timestamp`).

    Used by the `/amm/pools/{pool_id}/risk` API endpoint, where trades have
    already been flattened to scalar columns rather than the nested `Trade`
    schema `pool_round_trip_ratio` expects.
    """
    if not rows:
        return {"round_trip_ratio": 0.0, "trader_concentration": 0.0, "trade_count": 0}

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    volumes = df.groupby("base_account")["base_amount"].sum()
    total_volume = volumes.sum()
    trader_concentration = float(((volumes / total_volume) ** 2).sum()) if total_volume > 0 else 0.0

    round_trips = 0
    for account, account_df in df.groupby("base_account"):
        account_df = account_df.sort_values("timestamp").reset_index(drop=True)
        n = len(account_df)
        for i in range(n):
            row_i = account_df.iloc[i]
            window_end = row_i["timestamp"] + window
            later = account_df.iloc[i + 1 :]
            later = later[later["timestamp"] <= window_end]
            matched = later[
                (later["base_asset_pair"] == row_i["base_asset_pair"])
                & (later["counter_asset_pair"] == row_i["counter_asset_pair"])
                & (later["base_is_seller"] != row_i["base_is_seller"])
            ]
            if not matched.empty:
                round_trips += 1

    return {
        "round_trip_ratio": float(round_trips / len(df)),
        "trader_concentration": trader_concentration,
        "trade_count": int(len(df)),
    }
