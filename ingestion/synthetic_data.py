"""Synthetic SDEX trade data generator for local training and testing.

The canonical labelled dataset lives in `ledgerlens-data`, which is not
populated for local development. This module generates synthetic trade
activity — a pool of "normal" accounts trading with organic, Benford-
conforming amounts and timing, plus a number of "wash rings" that trade
round-lot amounts back and forth in tight time clusters — so that
`detection.dataset.build_training_dataset` and `detection.model_training`
have something realistic to train and test against.

Output matches the schemas in `ingestion.data_models` (`Trade`,
`OrderBookEvent`) so it can be passed directly into
`detection.feature_engineering.build_feature_vector`.
"""

import random
import string
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ingestion.data_models import Asset, OrderBookEvent, Trade

NATIVE = Asset(code="XLM", issuer=None)
USDC = Asset(code="USDC", issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")

# Round-lot amounts wash-trading bots commonly reuse, which skew the
# leading-digit distribution away from Benford's expectation.
WASH_LOT_SIZES = (100.0, 200.0, 250.0, 500.0, 1000.0, 5000.0)

_ADDRESS_ALPHABET = string.ascii_uppercase + "234567"


def _random_account(rng: random.Random) -> str:
    """Generate a pseudo Stellar account id (`G` + 55 base32 chars)."""
    return "G" + "".join(rng.choices(_ADDRESS_ALPHABET, k=55))


def _make_trade(
    trade_id: int,
    close_time: datetime,
    base_account: str,
    counter_account: str,
    base_amount: float,
    price: float,
) -> Trade:
    return Trade(
        id=str(trade_id),
        ledger_close_time=close_time,
        base_account=base_account,
        counter_account=counter_account,
        base_asset=NATIVE,
        counter_asset=USDC,
        base_amount=base_amount,
        counter_amount=round(base_amount * price, 7),
        price=price,
        base_is_seller=trade_id % 2 == 0,
    )


def generate_synthetic_dataset(
    n_normal_accounts: int = 20,
    n_wash_rings: int = 3,
    ring_size: int = 3,
    trades_per_normal: int = 15,
    trades_per_wash: int = 30,
    lookback_days: int = 30,
    as_of: datetime | None = None,
    seed: int = 42,
) -> tuple[pd.DataFrame, dict[str, dict], pd.DataFrame, dict[str, int]]:
    """Generate a synthetic trade history with labelled wash-trading rings.

    Returns `(trades, account_metadata, order_book_events, labels)`:

    - `trades`: DataFrame of `Trade` records (native XLM / USDC pair).
    - `account_metadata`: `{account: {"funding_source", "created_at"}}` as
      returned by `ingestion.account_loader.load_account_metadata`.
    - `order_book_events`: DataFrame of `OrderBookEvent` records.
    - `labels`: `{account: 0 | 1}`, 1 for accounts participating in a wash ring.
    """
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    as_of = as_of or datetime.now(timezone.utc)
    start = as_of - timedelta(days=lookback_days)

    trades: list[Trade] = []
    account_metadata: dict[str, dict] = {}
    order_book_events: list[OrderBookEvent] = []
    labels: dict[str, int] = {}
    trade_id = 0
    event_id = 0

    normal_accounts = [_random_account(rng) for _ in range(n_normal_accounts)]
    for account in normal_accounts:
        labels[account] = 0
        account_metadata[account] = {
            "funding_source": _random_account(rng),
            "created_at": start - timedelta(days=rng.uniform(30, 365)),
        }

    for account in normal_accounts:
        for _ in range(trades_per_normal):
            counterparty = rng.choice([a for a in normal_accounts if a != account])
            offset_seconds = rng.uniform(0, lookback_days * 86400)
            close_time = start + timedelta(seconds=offset_seconds)
            amount = float(np_rng.lognormal(mean=3.0, sigma=1.5))
            price = float(np_rng.uniform(0.08, 0.15))

            trade_id += 1
            trades.append(_make_trade(trade_id, close_time, account, counterparty, amount, price))

            if rng.random() < 0.1:
                event_id += 1
                order_book_events.append(
                    OrderBookEvent(
                        id=str(event_id),
                        timestamp=close_time,
                        account=account,
                        asset_pair="XLM/USDC",
                        side=rng.choice(["buy", "sell"]),
                        amount=amount,
                        price=price,
                        event_type=rng.choice(["created", "updated"]),
                    )
                )

    for ring_idx in range(n_wash_rings):
        ring_accounts = [_random_account(rng) for _ in range(ring_size)]
        shared_funding_source = _random_account(rng)
        ring_created_at = as_of - timedelta(days=rng.uniform(0, 5))

        for account in ring_accounts:
            labels[account] = 1
            account_metadata[account] = {
                "funding_source": shared_funding_source,
                "created_at": ring_created_at,
            }

        # Wash trades cluster tightly in time, off-hours, with round lots
        # that round-trip the same pair of assets between ring members.
        for _ in range(trades_per_wash):
            base_account, counter_account = rng.sample(ring_accounts, 2)
            burst_start = start + timedelta(seconds=rng.uniform(0, lookback_days * 86400))
            off_hours_time = burst_start.replace(hour=rng.randint(0, 5))
            amount = rng.choice(WASH_LOT_SIZES)
            price = float(np_rng.uniform(0.08, 0.15))

            trade_id += 1
            trades.append(_make_trade(trade_id, off_hours_time, base_account, counter_account, amount, price))

            # Round-trip leg: send the same amount straight back shortly after.
            trade_id += 1
            return_time = off_hours_time + timedelta(seconds=rng.uniform(1, 60))
            trades.append(_make_trade(trade_id, return_time, counter_account, base_account, amount, price))

            if rng.random() < 0.4:
                event_id += 1
                order_book_events.append(
                    OrderBookEvent(
                        id=str(event_id),
                        timestamp=off_hours_time,
                        account=base_account,
                        asset_pair="XLM/USDC",
                        side=rng.choice(["buy", "sell"]),
                        amount=amount,
                        price=price,
                        event_type="cancelled",
                    )
                )

        # Occasional self-matched trades within the ring.
        for _ in range(trades_per_wash // 5):
            account = rng.choice(ring_accounts)
            offset_seconds = rng.uniform(0, lookback_days * 86400)
            close_time = start + timedelta(seconds=offset_seconds)
            amount = rng.choice(WASH_LOT_SIZES)
            price = float(np_rng.uniform(0.08, 0.15))

            trade_id += 1
            trades.append(_make_trade(trade_id, close_time, account, account, amount, price))

    trades_df = pd.DataFrame([t.model_dump() for t in trades])
    trades_df["ledger_close_time"] = pd.to_datetime(trades_df["ledger_close_time"], utc=True)
    trades_df = trades_df.sort_values("ledger_close_time").reset_index(drop=True)

    events_df = pd.DataFrame([e.model_dump() for e in order_book_events])
    if events_df.empty:
        events_df = pd.DataFrame(columns=["id", "timestamp", "account", "asset_pair", "side", "amount", "price", "event_type"])

    return trades_df, account_metadata, events_df, labels


if __name__ == "__main__":
    trades_df, _, events_df, labels = generate_synthetic_dataset()
    n_wash = sum(labels.values())
    print(f"Generated {len(trades_df)} trades, {len(events_df)} order-book events, "
          f"{len(labels)} accounts ({n_wash} wash-labelled)")
