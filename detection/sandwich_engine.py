"""AMM sandwich-attack / price-manipulation detection.

`detection.amm_engine` computes *volume* manipulation features (round-trip
ratio, share concentration). It cannot see the temporal pattern that defines a
sandwich attack: an attacker submits a large buy, lets a victim's trade execute
at the inflated price, then immediately sells back into the pool for a profit.

On Stellar, ledger ordering is deterministic within a ledger close, so a
sandwich reduces to finding ordered triples ``[buy_a -> trade_v -> sell_a]``
over ``(ledger_sequence, operation_order)`` keys against the same pool.

This module operates on a `Trade`-shaped DataFrame (see
`ingestion.data_models.Trade`) restricted to pool trades. The two ordering
columns the algorithm needs — ``ledger_sequence`` and ``operation_order`` — are
optional: when absent they are derived deterministically from
``ledger_close_time`` so the detector works against the existing trade schema
without requiring a migration of `Trade`.
"""

from dataclasses import dataclass

import pandas as pd

from ingestion.data_models import TradeType


@dataclass
class SandwichCandidate:
    attacker: str
    victim: str
    pool_id: str
    buy_op_idx: int
    victim_op_idx: int
    sell_op_idx: int
    profit_xlm: float
    ledger_sequence: int
    slippage_inflicted: float = 0.0


def _with_ordering(trades: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of `trades` guaranteed to carry integer ``ledger_sequence``
    and ``operation_order`` columns.

    When the columns are missing they are synthesised from ``ledger_close_time``:
    each distinct close time becomes one ledger (dense-ranked), and rows sharing
    a ledger are ordered by their original position. This mirrors Horizon's
    deterministic in-ledger operation ordering closely enough for detection
    while keeping the detector usable on plain `Trade` rows.
    """
    df = trades.copy()

    if "ledger_sequence" not in df.columns:
        if "ledger_close_time" in df.columns:
            times = pd.to_datetime(df["ledger_close_time"])
            df["ledger_sequence"] = times.rank(method="dense").astype(int)
        else:
            df["ledger_sequence"] = range(len(df))

    if "operation_order" not in df.columns:
        df["_orig"] = range(len(df))
        df["operation_order"] = (
            df.sort_values(["ledger_sequence", "_orig"])
            .groupby("ledger_sequence")
            .cumcount()
            .reindex(df.index)
        )
        df = df.drop(columns="_orig")

    df["ledger_sequence"] = df["ledger_sequence"].astype(int)
    df["operation_order"] = df["operation_order"].astype(int)
    return df


def _pool_rows(trades: pd.DataFrame) -> pd.DataFrame:
    """Restrict to liquidity-pool trades that carry a pool id."""
    df = trades
    if "trade_type" in df.columns:
        df = df[df["trade_type"] == TradeType.LIQUIDITY_POOL]
    if "liquidity_pool_id" not in df.columns:
        return df.iloc[0:0]
    return df[df["liquidity_pool_id"].notna()]


def detect_sandwich_candidates(
    trades: pd.DataFrame,
    min_profit_xlm: float = 10.0,
    max_ledger_gap: int = 2,
    slippage_threshold: float = 0.0,
    fee_rate: float = 0.0,
) -> list[SandwichCandidate]:
    """Find sandwich-attack triples per pool.

    For each pool, find triples ``(buy_a, trade_v, sell_a)`` where:

    - ``buy_a`` and ``sell_a`` share the same account (the *attacker*);
    - ``trade_v`` is from a different account (the *victim*) and trades the
      same direction as ``buy_a`` (it buys into the inflated price);
    - ``buy_a.ledger <= trade_v.ledger <= sell_a.ledger`` and the span of the
      sandwich is within ``max_ledger_gap`` ledgers;
    - ``buy_a.operation_order < trade_v.operation_order < sell_a.operation_order``
      when trades share a ledger (enforced globally by the
      ``(ledger_sequence, operation_order)`` sort order);
    - ``sell_a`` price exceeds ``buy_a`` price by at least ``slippage_threshold``
      (a fraction of the buy price);
    - the attacker's profit clears ``min_profit_xlm``.

    Price impact and profit follow the issue's formulas::

        slippage_inflicted = (victim_price - pre_attack_pool_price)
                             / pre_attack_pool_price
        attacker_profit = (sell_price - buy_price) * quantity - fees

    where ``quantity`` is the attacker's traded base amount and ``fees`` are
    ``fee_rate`` of the round-trip notional. ``pre_attack_pool_price`` is taken
    as the price of the most recent pool trade strictly before the attacker's
    buy (falling back to the buy price when none exists).

    A "buy" of the pool's base asset is a trade with ``base_is_seller`` falsey;
    a "sell" has ``base_is_seller`` truthy.
    """
    pool_rows = _pool_rows(trades)
    if pool_rows.empty:
        return []

    df = _with_ordering(pool_rows)
    candidates: list[SandwichCandidate] = []

    for pool_id, pool_df in df.groupby("liquidity_pool_id"):
        ordered = pool_df.sort_values(["ledger_sequence", "operation_order"]).reset_index(drop=True)
        n = len(ordered)
        if n < 3:
            continue

        is_buy = ~ordered["base_is_seller"].astype(bool)
        accounts = ordered["base_account"].tolist()
        prices = ordered["price"].astype(float).tolist()
        amounts = ordered["base_amount"].astype(float).tolist()
        ledgers = ordered["ledger_sequence"].tolist()
        op_orders = ordered["operation_order"].tolist()
        buy_flags = is_buy.tolist()

        for i in range(n):
            if not buy_flags[i]:
                continue
            attacker = accounts[i]
            buy_price = prices[i]
            if buy_price <= 0:
                continue

            # earliest qualifying closing sell by the same account
            for k in range(i + 1, n):
                if buy_flags[k] or accounts[k] != attacker:
                    continue
                if ledgers[k] - ledgers[i] > max_ledger_gap:
                    break
                sell_price = prices[k]
                if sell_price <= buy_price * (1.0 + slippage_threshold):
                    continue

                # a victim must sit strictly between the two attacker legs
                victim_j = _select_victim(i, k, accounts, buy_flags, prices, attacker)
                if victim_j is None:
                    continue

                quantity = min(amounts[i], amounts[k])
                fees = fee_rate * (buy_price + sell_price) * quantity
                profit = (sell_price - buy_price) * quantity - fees
                if profit < min_profit_xlm:
                    continue

                pre_price = _pre_attack_price(prices, i, buy_price)
                victim_price = prices[victim_j]
                slippage = (victim_price - pre_price) / pre_price if pre_price > 0 else 0.0

                candidates.append(
                    SandwichCandidate(
                        attacker=attacker,
                        victim=accounts[victim_j],
                        pool_id=str(pool_id),
                        buy_op_idx=int(op_orders[i]),
                        victim_op_idx=int(op_orders[victim_j]),
                        sell_op_idx=int(op_orders[k]),
                        profit_xlm=round(float(profit), 7),
                        ledger_sequence=int(ledgers[i]),
                        slippage_inflicted=round(float(slippage), 7),
                    )
                )
                break  # one sandwich per opening buy

    return candidates


def _select_victim(
    i: int,
    k: int,
    accounts: list[str],
    buy_flags: list[bool],
    prices: list[float],
    attacker: str,
) -> int | None:
    """Pick the most-impacted victim (highest price) buying between legs ``i`` and ``k``."""
    best_j = None
    best_price = -1.0
    for j in range(i + 1, k):
        if accounts[j] == attacker or not buy_flags[j]:
            continue
        if prices[j] > best_price:
            best_price = prices[j]
            best_j = j
    return best_j


def _pre_attack_price(prices: list[float], buy_idx: int, fallback: float) -> float:
    """Price of the pool trade immediately before the attacker's buy, else `fallback`."""
    if buy_idx > 0:
        prev = prices[buy_idx - 1]
        if prev > 0:
            return prev
    return fallback
