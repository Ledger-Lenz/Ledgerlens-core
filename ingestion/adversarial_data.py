"""Evasion-aware wash-ring generator for adversarial robustness testing.

Extends `ingestion.synthetic_data` with wash rings that actively try to
evade the detection features in `detection.feature_engineering`.  Each
evasion strategy targets a specific detection signal:

- ``benford_mimicry``       – amounts drawn from a lognormal fitted to
                              natural trade data so digit distribution
                              matches Benford's Law expectation.
- ``temporal_jitter``       – 60–180 s delay between round-trip legs to
                              avoid tight intra-minute clustering.
- ``hour_spread``           – trades spread uniformly across all 24 h to
                              dilute the off-hours activity ratio.
- ``counterparty_rotation`` – each wash trade uses a fresh wallet from a
                              pool of 20+ to suppress counterparty
                              concentration.
- ``decoy_trades``          – 6 low-value legitimate-looking trades
                              inserted between each wash pair to mask
                              the round-trip pattern.
"""

import random
import string
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ingestion.data_models import Asset, OrderBookEvent, Trade
from ingestion.synthetic_data import (
    NATIVE,
    USDC,
    _make_trade,
    _random_account,
    generate_synthetic_dataset,
)

ALL_STRATEGIES = [
    "benford_mimicry",
    "temporal_jitter",
    "hour_spread",
    "counterparty_rotation",
    "decoy_trades",
]

_ADDRESS_ALPHABET = string.ascii_uppercase + "234567"


def generate_adversarial_dataset(
    n_normal_accounts: int = 50,
    n_wash_rings: int = 10,
    ring_size: int = 4,
    evasion_strategies: list[str] | None = None,
    seed: int = 42,
) -> tuple[pd.DataFrame, dict, pd.DataFrame, dict[str, int]]:
    """Generate wash-trading rings that actively attempt to evade detection.

    Parameters
    ----------
    evasion_strategies:
        Subset of ``ALL_STRATEGIES`` to apply; ``None`` activates all five.

    Returns
    -------
    Same four-tuple as `generate_synthetic_dataset`:
    ``(trades_df, account_metadata, events_df, labels)``.
    """
    strategies = set(evasion_strategies if evasion_strategies is not None else ALL_STRATEGIES)
    unknown = strategies - set(ALL_STRATEGIES)
    if unknown:
        raise ValueError(f"Unknown evasion strategies: {unknown}")

    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    as_of = datetime.now(timezone.utc)
    lookback_days = 30
    start = as_of - timedelta(days=lookback_days)

    # --- normal accounts (same as synthetic_data baseline) ---
    normal_trades_df, account_metadata, normal_events_df, labels = generate_synthetic_dataset(
        n_normal_accounts=n_normal_accounts,
        n_wash_rings=0,
        ring_size=ring_size,
        lookback_days=lookback_days,
        as_of=as_of,
        seed=seed,
    )

    # Pool of decoy counterparties used for counterparty_rotation
    rotation_pool = [_random_account(rng) for _ in range(20)]
    for acc in rotation_pool:
        labels[acc] = 0  # not wash-labelled; they're innocent bystanders
        account_metadata[acc] = {
            "funding_source": _random_account(rng),
            "created_at": start - timedelta(days=rng.uniform(30, 365)),
        }

    wash_trades: list[Trade] = []
    wash_events: list[OrderBookEvent] = []
    trade_id = int(normal_trades_df["id"].astype(int).max()) + 1 if not normal_trades_df.empty else 1
    event_id = 1

    for _ in range(n_wash_rings):
        ring_accounts = [_random_account(rng) for _ in range(ring_size)]
        shared_funder = _random_account(rng)
        ring_created = as_of - timedelta(days=rng.uniform(0, 5))

        for acc in ring_accounts:
            labels[acc] = 1
            account_metadata[acc] = {
                "funding_source": shared_funder,
                "created_at": ring_created,
            }

        trades_per_wash = 30
        for _ in range(trades_per_wash):
            # --- pick base time ---
            base_time = start + timedelta(seconds=rng.uniform(0, lookback_days * 86400))

            if "hour_spread" in strategies:
                # Spread uniformly over all 24 hours instead of clustering off-hours
                base_time = base_time.replace(hour=rng.randint(0, 23))
            else:
                base_time = base_time.replace(hour=rng.randint(0, 5))

            # --- pick amount ---
            if "benford_mimicry" in strategies:
                # Sample from lognormal that mimics natural trade amounts
                amount = float(np_rng.lognormal(mean=3.0, sigma=1.5))
            else:
                amount = float(rng.choice((100.0, 200.0, 500.0, 1000.0)))

            price = float(np_rng.uniform(0.08, 0.15))

            # --- pick counterparty ---
            if "counterparty_rotation" in strategies:
                base_account = rng.choice(ring_accounts)
                counter_account = rng.choice([a for a in rotation_pool if a != base_account])
            else:
                base_account, counter_account = rng.sample(ring_accounts, 2)

            # --- decoy trades before the wash pair ---
            if "decoy_trades" in strategies:
                for d in range(6):
                    decoy_time = base_time - timedelta(seconds=rng.uniform(60, 600))
                    decoy_amount = float(np_rng.lognormal(mean=1.5, sigma=0.8))
                    decoy_counterparty = rng.choice(rotation_pool)
                    wash_trades.append(
                        _make_trade(trade_id, decoy_time, base_account, decoy_counterparty, decoy_amount, price)
                    )
                    trade_id += 1

            # --- wash trade leg 1 ---
            wash_trades.append(_make_trade(trade_id, base_time, base_account, counter_account, amount, price))
            trade_id += 1

            # --- round-trip leg ---
            if "temporal_jitter" in strategies:
                jitter = rng.uniform(60, 180)
            else:
                jitter = rng.uniform(1, 60)
            return_time = base_time + timedelta(seconds=jitter)
            wash_trades.append(_make_trade(trade_id, return_time, counter_account, base_account, amount, price))
            trade_id += 1

    wash_trades_df = pd.DataFrame([t.model_dump() for t in wash_trades])
    wash_trades_df["ledger_close_time"] = pd.to_datetime(wash_trades_df["ledger_close_time"], utc=True)

    wash_events_df = pd.DataFrame(columns=["id", "timestamp", "account", "asset_pair", "side", "amount", "price", "event_type"])

    trades_df = pd.concat([normal_trades_df, wash_trades_df], ignore_index=True)
    trades_df = trades_df.sort_values("ledger_close_time").reset_index(drop=True)

    events_df = pd.concat([normal_events_df, wash_events_df], ignore_index=True)

    return trades_df, account_metadata, events_df, labels
