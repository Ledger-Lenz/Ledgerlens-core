"""Flash-Loan Price-Manipulation Detector for AMM Pools.

Detects single-block, capital-disproportionate round-trips that temporarily
distort an AMM pool's spot price to exploit a downstream protocol using that
pool as a price oracle.

This is structurally distinct from:
  - **Wash trading** (``detection/amm_engine.py``): inflates volume, not price.
  - **Sandwich attacks** (``detection/sandwich_engine.py``): requires a victim's
    pending transaction, not a single atomic round-trip.

A flash-loan price-manipulation attack has this signature:
  1. An account with **near-zero prior organic exposure** to the pool.
  2. A swap large enough to move the pool price outside its trailing
     volatility band (default: 4 sigma).
  3. Reversal of the position within the same block (or a tight N-block
     window), leaving net exposure near pre-manipulation level.

False-positive hardening:
  - ``prior_position_ratio`` check: the account must have minimal organic
    holdings before the manipulation.
  - Pool-depth-share requirement: the manipulating trade must move >= a
    configurable fraction of the pool's total depth.
  - Trailing volatility band: adapts to natural pool volatility so that
    legitimately volatile pools are not over-flagged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from detection.amm_engine import _pair_key
from detection.sandwich_engine import _pool_rows, _with_ordering
from ingestion.data_models import TradeType

if TYPE_CHECKING:
    from ingestion.data_models import LiquidityPool

logger = logging.getLogger("ledgerlens.oracle_manipulation")

# ---------------------------------------------------------------------------
# Default thresholds
# ---------------------------------------------------------------------------

DEFAULT_PRICE_DEVIATION_SIGMA: float = 4.0
"""Number of standard-deviations from the trailing band that flags a trade."""

DEFAULT_REVERSAL_WINDOW_BLOCKS: int = 1
"""Max blocks within which reversal must occur."""

DEFAULT_REVERSAL_TOLERANCE_PCT: float = 0.05
"""Net position after reversal must be within this fraction of pre-manip size."""

DEFAULT_MIN_POOL_SHARE_PCT: float = 0.10
"""Manipulating trade must move at least this fraction of pool depth."""

DEFAULT_ROLLING_WINDOW_TRADES: int = 50
"""Number of preceding trades used to compute the trailing volatility band."""

_MIN_SAMPLES_FOR_BAND: int = 5
"""Minimum pool trades required before a volatility band can be computed."""

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FlashLoanManipulationCandidate:
    """A single detected flash-loan price-manipulation candidate."""

    account: str
    """Wallet address of the suspected manipulator."""

    pool_id: str
    """AMM pool ID where the manipulation occurred."""

    manipulating_trade_id: str
    """Trade ID or index of the manipulating (large) swap."""

    price_before: float
    """Pool price immediately before the manipulation."""

    price_at_peak: float
    """Pool price at the manipulated trade's execution."""

    price_deviation_sigma: float
    """How many sigma the price deviated from the trailing band."""

    reversal_trade_id: str | None
    """Trade ID or index of the reversal trade, if found."""

    reversed_within_blocks: int | None
    """Number of blocks between the manipulation and its reversal."""

    prior_position_ratio: float
    """Account's prior position relative to the manipulation size (0 = none)."""

    confidence: float
    """Overall confidence score in [0, 1]."""


def candidate_to_alert(c: FlashLoanManipulationCandidate, asset_pair: str) -> dict:
    """Convert a candidate to an alert dict for ``detection.storage.save_alerts``."""
    return {
        "alert_type": "FLASH_LOAN_MANIPULATION",
        "wallet": c.account,
        "asset_pair": asset_pair,
        "pool_id": c.pool_id,
        "detail": {
            "manipulating_trade_id": c.manipulating_trade_id,
            "price_before": c.price_before,
            "price_at_peak": c.price_at_peak,
            "price_deviation_sigma": c.price_deviation_sigma,
            "reversal_trade_id": c.reversal_trade_id,
            "reversed_within_blocks": c.reversed_within_blocks,
            "prior_position_ratio": c.prior_position_ratio,
            "confidence": c.confidence,
        },
    }


def candidates_to_alerts(
    candidates: list[FlashLoanManipulationCandidate], asset_pair: str = "",
) -> list[dict]:
    """Convert all candidates to alert dicts."""
    return [candidate_to_alert(c, asset_pair) for c in candidates]


# ---------------------------------------------------------------------------
# Trailing volatility band
# ---------------------------------------------------------------------------


def compute_volatility_band(
    pool_trades: pd.DataFrame,
    trade_idx: int,
    rolling_window: int = DEFAULT_ROLLING_WINDOW_TRADES,
) -> tuple[float, float]:
    """Compute trailing mean and std of ``price`` before ``trade_idx``.

    Returns ``(mean_price, std_price)`` from the up-to ``rolling_window``
    trades preceding ``trade_idx``.  When there are fewer than
    ``_MIN_SAMPLES_FOR_BAND`` prior trades, returns ``(NaN, NaN)`` so the
    caller can skip the trade.

    Parameters
    ----------
    pool_trades : pd.DataFrame
        Sorted pool-trade rows (must have ``price`` column).
    trade_idx : int
        Index of the trade being checked.
    rolling_window : int
        Number of preceding trades to use.

    Returns
    -------
    tuple[float, float]
        ``(mean, std)`` or ``(NaN, NaN)`` when insufficient data.
    """
    if trade_idx < _MIN_SAMPLES_FOR_BAND:
        return float("nan"), float("nan")

    start = max(0, trade_idx - rolling_window)
    prior_prices = pool_trades.iloc[start:trade_idx]["price"].astype(float)

    if len(prior_prices) < _MIN_SAMPLES_FOR_BAND:
        return float("nan"), float("nan")

    return float(prior_prices.mean()), float(prior_prices.std())


# ---------------------------------------------------------------------------
# Prior position
# ---------------------------------------------------------------------------


def _compute_prior_position(
    pool_trades: pd.DataFrame,
    account: str,
    trade_idx: int,
) -> tuple[float, float]:
    """Compute an account's net position before ``trade_idx``.

    Returns ``(net_position, max_position)`` where:
      - ``net_position`` = buys - sells (in base amount) before ``trade_idx``
      - ``max_position`` = maximum absolute position during the window

    A near-zero ``net_position`` indicates the account had no organic exposure.
    """
    prior = pool_trades.iloc[:trade_idx]
    account_trades = prior[prior["base_account"] == account]

    if account_trades.empty:
        return 0.0, 0.0

    net = 0.0
    max_pos = 0.0
    for _, row in account_trades.iterrows():
        amount = float(row["base_amount"])
        is_buy = not bool(row["base_is_seller"])
        net += amount if is_buy else -amount
        max_pos = max(max_pos, abs(net))

    return net, max_pos


# ---------------------------------------------------------------------------
# Reversal detection
# ---------------------------------------------------------------------------


def _find_reversal(
    pool_trades: pd.DataFrame,
    account: str,
    trade_idx: int,
    manipulation_amount: float,
    is_manipulation_buy: bool,
    block_window: int,
    tolerance_pct: float,
) -> tuple[str | None, int | None]:
    """Look for a reversal trade by ``account`` within ``block_window`` blocks.

    A reversal trade is one that moves the account's net position back toward
    zero — a sell after a large buy, or a buy after a large sell.

    Returns ``(reversal_trade_idx, blocks_between)`` or ``(None, None)``.
    """
    after = pool_trades.iloc[trade_idx + 1 :]
    if after.empty:
        return None, None

    manip_ledger = int(pool_trades.iloc[trade_idx]["ledger_sequence"])
    target_net = manipulation_amount * (1.0 - tolerance_pct)

    for j, (_, row) in enumerate(after.iterrows()):
        if str(row["base_account"]) != account:
            continue

        rev_ledger = int(row["ledger_sequence"])
        if rev_ledger - manip_ledger > block_window:
            break

        rev_amount = float(row["base_amount"])
        rev_is_buy = not bool(row["base_is_seller"])

        # If manipulation was a buy, reversal is a sell of similar magnitude
        if is_manipulation_buy and not rev_is_buy and rev_amount >= target_net:
            actual_idx = trade_idx + 1 + j
            return str(actual_idx), rev_ledger - manip_ledger

        # If manipulation was a sell, reversal is a buy of similar magnitude
        if not is_manipulation_buy and rev_is_buy and rev_amount >= target_net:
            actual_idx = trade_idx + 1 + j
            return str(actual_idx), rev_ledger - manip_ledger

    return None, None


# ---------------------------------------------------------------------------
# Main detection
# ---------------------------------------------------------------------------


def detect_flash_loan_manipulation(
    trades: pd.DataFrame,
    pool_id_to_depth: dict[str, float] | None = None,
    price_deviation_sigma: float = DEFAULT_PRICE_DEVIATION_SIGMA,
    reversal_window_blocks: int = DEFAULT_REVERSAL_WINDOW_BLOCKS,
    reversal_tolerance_pct: float = DEFAULT_REVERSAL_TOLERANCE_PCT,
    min_pool_share_pct: float = DEFAULT_MIN_POOL_SHARE_PCT,
    rolling_window: int = DEFAULT_ROLLING_WINDOW_TRADES,
    min_confidence: float = 0.5,
) -> list[FlashLoanManipulationCandidate]:
    """Detect flash-loan-funded price manipulation in pool trades.

    For each AMM pool, scans the ordered trade sequence looking for:

    1. A trade whose executed price deviates from the trailing volatility
       band by more than ``price_deviation_sigma`` standard deviations.
    2. The account had near-zero prior organic exposure to that pool
       (``prior_position_ratio`` near 0).
    3. The trade's size moves at least ``min_pool_share_pct`` of the pool's
       total depth.
    4. A same-account reversal occurs within ``reversal_window_blocks``
       blocks.

    Parameters
    ----------
    trades : pd.DataFrame
        Trade-shaped DataFrame (see ``ingestion.data_models.Trade``).
        Must include ``trade_type``, ``liquidity_pool_id``, ``price``,
        ``base_account``, ``base_amount``, ``base_is_seller`` columns.
    pool_id_to_depth : dict[str, float] or None
        Mapping of pool_id -> total_liquidity_depth. When ``None``, the
        pool-depth-share requirement is skipped.
    price_deviation_sigma : float
        Sigma threshold for flagging a price deviation.
    reversal_window_blocks : int
        Max ledger/block gap for reversal detection.
    reversal_tolerance_pct : float
        Tolerance for net position after reversal.
    min_pool_share_pct : float
        Minimum fraction of pool depth the manipulating trade must represent.
    rolling_window : int
        Number of prior trades for the trailing volatility band.
    min_confidence : float
        Minimum confidence score for a candidate to be returned.

    Returns
    -------
    list[FlashLoanManipulationCandidate]
        Detected candidates sorted by confidence descending.
    """
    pool_rows = _pool_rows(trades)
    if pool_rows.empty:
        return []

    df = _with_ordering(pool_rows)
    candidates: list[FlashLoanManipulationCandidate] = []

    for pool_id, pool_df in df.groupby("liquidity_pool_id"):
        ordered = pool_df.sort_values(
            ["ledger_sequence", "operation_order"]
        ).reset_index(drop=True)
        n = len(ordered)
        if n < _MIN_SAMPLES_FOR_BAND + 1:
            continue

        pool_depth = (pool_id_to_depth or {}).get(str(pool_id), 0.0)

        for i in range(n):
            row = ordered.iloc[i]
            price = float(row["price"])
            amount = float(row["base_amount"])
            account = str(row["base_account"])

            if price <= 0 or amount <= 0:
                continue

            # 1. Compute trailing volatility band
            mean_price, std_price = compute_volatility_band(
                ordered, i, rolling_window=rolling_window
            )
            if not np.isfinite(mean_price) or not np.isfinite(std_price) or std_price == 0:
                continue

            deviation_sigma = abs(price - mean_price) / std_price
            if deviation_sigma < price_deviation_sigma:
                continue

            # 2. Check pool depth share
            if pool_depth > 0:
                pool_share = amount / pool_depth
                if pool_share < min_pool_share_pct / 100.0:
                    continue

            # 3. Check prior organic position
            net_pos, max_pos = _compute_prior_position(ordered, account, i)
            prior_position_ratio = (
                min(max_pos / amount, 1.0) if amount > 0 else 1.0
            )
            # If the account had significant prior exposure, this is likely
            # a legitimate large trade, not a flash-loan attack.
            if prior_position_ratio > 0.1:
                continue

            # 4. Detect same-block reversal
            is_buy = not bool(row["base_is_seller"])
            reversal_id, blocks_between = _find_reversal(
                ordered,
                account,
                i,
                amount,
                is_buy,
                reversal_window_blocks,
                reversal_tolerance_pct,
            )

            # Require a reversal for a flash-loan pattern
            if reversal_id is None:
                continue

            # 5. Compute confidence score
            sigma_norm = min(deviation_sigma / (price_deviation_sigma * 2), 1.0)
            reversal_confidence = 1.0 if blocks_between == 0 else (
                0.9 if blocks_between == 1 else 0.6
            )
            pool_share_signal = min(pool_share / 0.5, 1.0) if pool_depth > 0 else 0.5
            confidence = (sigma_norm * 0.4 + reversal_confidence * 0.3 + pool_share_signal * 0.3)

            if confidence < min_confidence:
                continue

            candidates.append(
                FlashLoanManipulationCandidate(
                    account=account,
                    pool_id=str(pool_id),
                    manipulating_trade_id=str(i),
                    price_before=round(mean_price, 8),
                    price_at_peak=round(price, 8),
                    price_deviation_sigma=round(deviation_sigma, 4),
                    reversal_trade_id=reversal_id,
                    reversed_within_blocks=blocks_between,
                    prior_position_ratio=round(prior_position_ratio, 6),
                    confidence=round(confidence, 4),
                )
            )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def compute_flash_loan_manipulation_score(
    trades: pd.DataFrame,
    account: str,
    liquidity_pools: dict[str, "LiquidityPool"] | None = None,
    pool_id_to_depth: dict[str, float] | None = None,
    **kwargs,
) -> float:
    """Compute a per-account flash-loan manipulation score (0–1).

    Designed to be called from ``detection.feature_engineering.amm_features()``.
    Returns the maximum confidence across all candidates involving ``account``.

    Parameters
    ----------
    trades : pd.DataFrame
        Trade-shaped DataFrame.
    account : str
        Wallet address to score.
    liquidity_pools : dict[str, LiquidityPool] or None
        Map of pool_id -> LiquidityPool objects (used to build depth map).
    pool_id_to_depth : dict[str, float] or None
        Pre-built pool depth map. If provided alongside ``liquidity_pools``,
        this takes precedence.

    Returns
    -------
    float
        Manipulation score in [0, 1]; 0 when no candidate involves ``account``.
    """
    if pool_id_to_depth is None and liquidity_pools is not None:
        pool_id_to_depth = build_pool_depth_map(liquidity_pools)
    candidates = detect_flash_loan_manipulation(trades, pool_id_to_depth, **kwargs)
    account_candidates = [c for c in candidates if c.account == account]
    if not account_candidates:
        return 0.0
    return max(c.confidence for c in account_candidates)


# ---------------------------------------------------------------------------
# Convenience: build pool depth map from LiquidityPool objects
# ---------------------------------------------------------------------------


def build_pool_depth_map(
    liquidity_pools: dict[str, "LiquidityPool"] | None,
) -> dict[str, float]:
    """Build a ``pool_id -> total_depth`` map from ``LiquidityPool`` objects.

    Total depth is approximated as ``total_shares * sqrt(reserve_a * reserve_b)``
    (a simplified constant-product depth proxy).  Returns an empty dict when
    ``liquidity_pools`` is ``None`` or empty, so the pool-depth requirement is
    skipped gracefully.
    """
    if not liquidity_pools:
        return {}

    depth_map: dict[str, float] = {}
    for pid, pool in liquidity_pools.items():
        if pool.total_shares > 0:
            # Use total_shares as a rough depth proxy; in a CPMM, total_shares
            # is proportional to the geometric mean of the two reserves.
            depth_map[pid] = float(pool.total_shares)
        else:
            depth_map[pid] = 0.0
    return depth_map
