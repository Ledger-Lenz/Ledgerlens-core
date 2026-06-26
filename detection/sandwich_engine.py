"""Sandwich attack detection for SDEX order-book trades.

A sandwich attack consists of three ordered events in a tight ledger window:
  1. Front-run: attacker buys asset X just before a large victim order.
  2. Victim order: large buy of X that moves the price.
  3. Back-run: attacker sells X immediately after at the inflated price.

False-positive hardening (issue #122):
  - Victim-amount minimum threshold (MIN_VICTIM_AMOUNT_XLM).
  - Price-impact score: minimum 0.3% movement required.
  - Statistical significance test: permutation test against 24h baseline.
  - Confidence score combining all three signals; only events >= 0.7 emitted.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

MIN_VICTIM_AMOUNT_XLM: float = 500.0
MIN_PRICE_IMPACT: float = 0.003  # 0.3%
MIN_SANDWICH_CONFIDENCE: float = 0.7
_PERMUTATION_N: int = 1000
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
    attacker_wallet: str
    victim_wallet: str
    asset_pair: str
    front_run_time: datetime
    victim_time: datetime
    back_run_time: datetime
    front_run_price: float
    victim_amount: float
    back_run_price: float
    pre_trade_price: float
    post_trade_price: float
    confidence: float = 0.0


@dataclass
class SandwichEvent:
    attacker_wallet: str
    victim_wallet: str
    asset_pair: str
    front_run_time: datetime
    victim_time: datetime
    back_run_time: datetime
    victim_amount: float
    price_impact: float
    sandwich_confidence: float


class SandwichEngine:
    """Detect sandwich attacks in a window of SDEX trade records."""

    def __init__(
        self,
        victim_amount_filter: float = MIN_VICTIM_AMOUNT_XLM,
        min_price_impact: float = MIN_PRICE_IMPACT,
        min_confidence: float = MIN_SANDWICH_CONFIDENCE,
        ledger_window: int = 5,
    ) -> None:
        self.victim_amount_filter = victim_amount_filter
        self.min_price_impact = min_price_impact
        self.min_confidence = min_confidence
        self.ledger_window = ledger_window

    def _compute_price_impact(self, pre_trade_price: float, post_trade_price: float) -> float:
        """Signed fractional price change caused by the victim order."""
        if pre_trade_price == 0.0:
            return 0.0
        return (post_trade_price - pre_trade_price) / pre_trade_price

    def _timing_significance(
        self,
        attacker_interval_s: float,
        pair_baseline_intervals: list[float],
    ) -> float:
        """One-sided permutation p-value: probability that a random interval
        from the 24h baseline is <= attacker_interval_s.

        Returns a significance score in [0, 1] where higher means tighter
        (more anomalous) timing. Score = 1 - p_value.
        """
        if not pair_baseline_intervals:
            return 0.5
        n = len(pair_baseline_intervals)
        count_le = sum(1 for v in pair_baseline_intervals if v <= attacker_interval_s)
        # Permutation p-value: fraction of baseline intervals at least as extreme
        p_value = count_le / n
        return 1.0 - p_value

    def _confidence(
        self,
        victim_amount: float,
        price_impact: float,
        timing_score: float,
    ) -> float:
        """Combine the three hardening signals into a [0, 1] confidence score."""
        # Amount signal: sigmoid-style, saturates at ~5x threshold
        amount_score = min(1.0, victim_amount / (self.victim_amount_filter * 5))
        # Impact signal: saturates at 3x min threshold
        impact_score = min(1.0, abs(price_impact) / (self.min_price_impact * 3))
        # Timing signal already in [0, 1]
        return (amount_score + impact_score + timing_score) / 3.0

    def detect(
        self,
        trades: pd.DataFrame,
        baseline_intervals: dict[str, list[float]] | None = None,
    ) -> list[SandwichEvent]:
        """Scan `trades` for sandwich attack patterns.

        `baseline_intervals` maps asset_pair -> list of inter-trade interval
        seconds over the prior 24h, used for the permutation significance test.
        """
        if trades.empty:
            return []

        required = {"base_account", "counter_account", "base_amount", "price", "ledger_close_time"}
        if not required.issubset(trades.columns):
            return []

        baseline_intervals = baseline_intervals or {}
        df = trades.sort_values("ledger_close_time").reset_index(drop=True)
        events: list[SandwichEvent] = []

        asset_pairs = df["asset_pair"].unique() if "asset_pair" in df.columns else [None]

        for pair in asset_pairs:
            if pair is not None:
                pair_df = df[df["asset_pair"] == pair].reset_index(drop=True)
            else:
                pair_df = df

            if len(pair_df) < 3:
                continue

            pair_key = str(pair) if pair else ""
            baselines = baseline_intervals.get(pair_key, [])

            # Group trades by account for fast lookup
            by_account: dict[str, list[int]] = {}
            for idx, row in pair_df.iterrows():
                acct = str(row["base_account"])
                by_account.setdefault(acct, []).append(idx)

            n = len(pair_df)
            for victim_idx in range(1, n - 1):
                victim_row = pair_df.iloc[victim_idx]
                victim_amount = float(victim_row["base_amount"])

                if victim_amount < self.victim_amount_filter:
                    continue

                victim_time = pd.Timestamp(victim_row["ledger_close_time"])
                victim_acct = str(victim_row["base_account"])

                # Look for front-run (buy) just before victim
                for fr_idx in range(max(0, victim_idx - self.ledger_window), victim_idx):
                    fr_row = pair_df.iloc[fr_idx]
                    fr_acct = str(fr_row["base_account"])
                    if fr_acct == victim_acct:
                        continue

                    fr_time = pd.Timestamp(fr_row["ledger_close_time"])

                    # Look for back-run (sell) just after victim by same attacker
                    for br_idx in range(victim_idx + 1, min(n, victim_idx + self.ledger_window + 1)):
                        br_row = pair_df.iloc[br_idx]
                        br_acct = str(br_row["base_account"])
                        if br_acct != fr_acct:
                            continue

                        br_time = pd.Timestamp(br_row["ledger_close_time"])
                        pre_price = float(fr_row["price"])
                        post_price = float(br_row["price"])
                        impact = self._compute_price_impact(pre_price, post_price)

                        if abs(impact) < self.min_price_impact:
                            continue

                        interval_s = (br_time - fr_time).total_seconds()
                        timing_score = self._timing_significance(interval_s, baselines)
                        conf = self._confidence(victim_amount, impact, timing_score)

                        if conf >= self.min_confidence:
                            events.append(
                                SandwichEvent(
                                    attacker_wallet=fr_acct,
                                    victim_wallet=victim_acct,
                                    asset_pair=pair_key,
                                    front_run_time=fr_time.to_pydatetime(),
                                    victim_time=victim_time.to_pydatetime(),
                                    back_run_time=br_time.to_pydatetime(),
                                    victim_amount=victim_amount,
                                    price_impact=impact,
                                    sandwich_confidence=conf,
                                )
                            )

        return events
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


def sandwich_candidates_to_alerts(candidates: list[SandwichCandidate], asset_pair: str) -> list[dict]:
    """Convert detected sandwich candidates into alert dicts for `detection.storage.save_alerts`."""
    return [
        {
            "alert_type": "SANDWICH_ATTACK",
            "wallet": c.attacker,
            "asset_pair": asset_pair,
            "pool_id": c.pool_id,
            "detail": {
                "victim": c.victim,
                "profit_xlm": c.profit_xlm,
                "slippage_inflicted": c.slippage_inflicted,
                "ledger_sequence": c.ledger_sequence,
                "buy_op_idx": c.buy_op_idx,
                "victim_op_idx": c.victim_op_idx,
                "sell_op_idx": c.sell_op_idx,
            },
        }
        for c in candidates
    ]
