"""Deterministic trade-sequence factory for tests.

Tests across the codebase construct `Trade` objects ad hoc with
hard-coded amounts and timestamps, which makes them brittle and hard to
read. `TradeFactory` is a single source of truth for realistic test
trade data: every method below is deterministic given the same `seed`
(same accounts, amounts, timestamps, and Horizon-style ids every run),
so tests built on top of it never flake.

See `ingestion.synthetic_data` and `ingestion.adversarial_data` for the
training-dataset equivalents this factory's `wash_ring` mirrors (same
round-lot amounts, same round-trip handoff shape) so wash rings produced
here are detectable by the same Benford / round-trip features used in
training.
"""

from __future__ import annotations

import random
import string
from datetime import datetime, timedelta, timezone

from ingestion.data_models import Asset, Trade, TradeType

NATIVE = Asset(code="XLM", issuer=None)
USDC = Asset(code="USDC", issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")

_ADDRESS_ALPHABET = string.ascii_uppercase + "234567"

# Round-lot amounts wash-trading bots commonly reuse, which skew the
# leading-digit distribution away from Benford's expectation. Mirrors
# ingestion.synthetic_data.WASH_LOT_SIZES.
WASH_LOT_SIZES = (100.0, 200.0, 250.0, 500.0, 1000.0, 5000.0)


def _toid(ledger_seq: int, tx_order: int = 1, op_order: int = 1) -> str:
    """Encode a Horizon-style "total order ID" for use as `Trade.id`.

    Mirrors Stellar's real TOID scheme: ledger sequence in the high 32
    bits, transaction order in the next 20 bits, operation order in the
    low 12 bits. Horizon trades use this same value as both `id` and
    `paging_token`, and `int(id) >> 32` recovers the ledger sequence --
    this is what "realistic paging_tokens and ledger sequences" means
    for a model that only has an `id` field.
    """
    return str((ledger_seq << 32) | ((tx_order & 0xFFFFF) << 12) | (op_order & 0xFFF))


def ledger_sequence_of(trade_id: str) -> int:
    """Recover the ledger sequence encoded in a factory-generated `Trade.id`."""
    return int(trade_id) >> 32


def _random_account(rng: random.Random) -> str:
    """Generate a pseudo Stellar account id (`G` + 55 base32 chars)."""
    return "G" + "".join(rng.choices(_ADDRESS_ALPHABET, k=55))


class TradeFactory:
    """Generates deterministic, realistic `Trade` sequences for tests."""

    @staticmethod
    def trade(
        *,
        id: str | None = None,
        ledger_seq: int = 1,
        tx_order: int = 1,
        op_order: int = 1,
        ledger_close_time: datetime,
        base_account: str,
        counter_account: str | None = None,
        base_asset: Asset = NATIVE,
        counter_asset: Asset = USDC,
        base_amount: float,
        counter_amount: float | None = None,
        price: float,
        base_is_seller: bool = True,
        trade_type: TradeType = TradeType.ORDERBOOK,
        liquidity_pool_id: str | None = None,
        transaction_hash: str | None = None,
    ) -> Trade:
        """Low-level explicit builder every higher-level factory method uses.

        Exposed directly so call sites that need exact control over every
        field (e.g. migrating a hand-written fixture one-for-one) can build
        a single `Trade` without duplicating the Horizon-id encoding logic.
        """
        return Trade(
            id=id if id is not None else _toid(ledger_seq, tx_order, op_order),
            ledger_close_time=ledger_close_time,
            base_account=base_account,
            counter_account=counter_account,
            base_asset=base_asset,
            counter_asset=counter_asset,
            base_amount=base_amount,
            counter_amount=counter_amount if counter_amount is not None else round(base_amount * price, 7),
            price=price,
            base_is_seller=base_is_seller,
            trade_type=trade_type,
            liquidity_pool_id=liquidity_pool_id,
            transaction_hash=transaction_hash,
        )

    @staticmethod
    def wash_ring(
        n_accounts: int = 3,
        n_rounds: int = 10,
        *,
        seed: int = 42,
        as_of: datetime | None = None,
        asset_pair: tuple[Asset, Asset] = (NATIVE, USDC),
        ledger_start: int = 1_000_000,
    ) -> list[Trade]:
        """`n_accounts` wallets trading round-lot amounts back and forth in a
        tight time cluster -- the classic wash-trading ring shape.

        Each round, a fixed handoff order (`account[i] -> account[(i+1) %
        n]`) executes one trade using a round-lot amount from
        `WASH_LOT_SIZES`, seconds apart. Round-lot amounts skew the leading
        digit distribution away from Benford's expectation, and the tight
        handoff loop drives up `round_trip_trade_frequency` /
        `self_matching_rate` -- the same signals `ingestion.synthetic_data`
        relies on to make wash rings detectable.
        """
        if n_accounts < 2:
            raise ValueError("wash_ring needs at least 2 accounts to form a ring")

        rng = random.Random(seed)
        as_of = as_of or datetime(2026, 1, 1, tzinfo=timezone.utc)
        base_asset, counter_asset = asset_pair
        accounts = [_random_account(rng) for _ in range(n_accounts)]

        trades: list[Trade] = []
        t = as_of
        ledger = ledger_start
        for round_idx in range(n_rounds):
            for i in range(n_accounts):
                sender = accounts[i]
                receiver = accounts[(i + 1) % n_accounts]
                amount = rng.choice(WASH_LOT_SIZES)
                t = t + timedelta(seconds=rng.randint(5, 30))
                ledger += 1
                trades.append(
                    TradeFactory.trade(
                        ledger_seq=ledger,
                        ledger_close_time=t,
                        base_account=sender,
                        counter_account=receiver,
                        base_asset=base_asset,
                        counter_asset=counter_asset,
                        base_amount=amount,
                        price=1.0,
                        base_is_seller=round_idx % 2 == 0,
                    )
                )
        return trades

    @staticmethod
    def legitimate_market_maker(
        n_trades: int = 50,
        *,
        seed: int = 42,
        as_of: datetime | None = None,
        asset_pair: tuple[Asset, Asset] = (NATIVE, USDC),
        n_counterparties: int = 20,
        ledger_start: int = 2_000_000,
        spread_hours: float = 24.0,
    ) -> list[Trade]:
        """A single market-making account trading organically-priced,
        Benford-conforming amounts against many distinct counterparties
        spread evenly across `spread_hours` -- the "looks completely
        normal" baseline.
        """
        rng = random.Random(seed)
        as_of = as_of or datetime(2026, 1, 1, tzinfo=timezone.utc)
        base_asset, counter_asset = asset_pair
        maker = _random_account(rng)
        counterparties = [_random_account(rng) for _ in range(n_counterparties)]

        # Real first-digit frequencies per Benford's Law (used to draw
        # leading digits so amounts conform rather than cluster on round lots).
        benford_weights = [30.1, 17.6, 12.5, 9.7, 7.9, 6.7, 5.8, 5.1, 4.6]

        trades: list[Trade] = []
        ledger = ledger_start
        start = as_of - timedelta(hours=spread_hours)
        step_seconds = (spread_hours * 3600) / max(n_trades, 1)
        for i in range(n_trades):
            counterparty = rng.choice(counterparties)
            leading_digit = rng.choices(range(1, 10), weights=benford_weights)[0]
            mantissa = rng.uniform(0.0, 0.999)
            magnitude = 10 ** rng.randint(0, 3)
            amount = round((leading_digit + mantissa) * magnitude, 4)
            price = round(rng.uniform(0.95, 1.05), 6)
            close_time = start + timedelta(seconds=step_seconds * i + rng.uniform(-30, 30))
            ledger += 1
            trades.append(
                TradeFactory.trade(
                    ledger_seq=ledger,
                    ledger_close_time=close_time,
                    base_account=maker,
                    counter_account=counterparty,
                    base_asset=base_asset,
                    counter_asset=counter_asset,
                    base_amount=amount,
                    price=price,
                    base_is_seller=i % 2 == 0,
                )
            )
        return trades

    @staticmethod
    def spoofing_attack(
        n_layers: int = 5,
        *,
        seed: int = 42,
        as_of: datetime | None = None,
        asset_pair: tuple[Asset, Asset] = (NATIVE, USDC),
        ledger_start: int = 3_000_000,
        layer_amount: float = 50.0,
    ) -> list[Trade]:
        """Executed-trade footprint of a spoofing/layering attack: one
        manipulator account executes `n_layers` small trades at
        successively worse prices against rotating decoy counterparties,
        seconds apart.

        Real spoofing is primarily about *placed-then-cancelled* orders
        (`OrderBookEvent`, not `Trade`), which `TradeFactory` does not
        model since it only produces *executed* trades. This generates the
        small number of low-volume executions that characteristically
        accompany a layering attack -- for tests exercising price-walk /
        volume features on the executed side.
        """
        rng = random.Random(seed)
        as_of = as_of or datetime(2026, 1, 1, tzinfo=timezone.utc)
        base_asset, counter_asset = asset_pair
        manipulator = _random_account(rng)

        trades: list[Trade] = []
        ledger = ledger_start
        t = as_of
        base_price = 1.0
        for layer in range(n_layers):
            decoy = _random_account(rng)
            t = t + timedelta(seconds=rng.randint(1, 5))
            ledger += 1
            price = round(base_price * (1 + 0.002 * layer), 6)
            trades.append(
                TradeFactory.trade(
                    ledger_seq=ledger,
                    ledger_close_time=t,
                    base_account=manipulator,
                    counter_account=decoy,
                    base_asset=base_asset,
                    counter_asset=counter_asset,
                    base_amount=layer_amount,
                    price=price,
                    base_is_seller=True,
                )
            )
        return trades

    @staticmethod
    def random_noise(
        n_trades: int = 100,
        *,
        seed: int = 42,
        as_of: datetime | None = None,
        asset_pair: tuple[Asset, Asset] = (NATIVE, USDC),
        n_accounts: int = 30,
        ledger_start: int = 4_000_000,
        spread_hours: float = 48.0,
    ) -> list[Trade]:
        """Uncorrelated random trades between random pairs of accounts at
        random times and amounts -- pure background noise for tests that
        need "nothing interesting is happening" data.
        """
        rng = random.Random(seed)
        as_of = as_of or datetime(2026, 1, 1, tzinfo=timezone.utc)
        base_asset, counter_asset = asset_pair
        accounts = [_random_account(rng) for _ in range(n_accounts)]

        trades: list[Trade] = []
        ledger = ledger_start
        start = as_of - timedelta(hours=spread_hours)
        for i in range(n_trades):
            base_acc, counter_acc = rng.sample(accounts, 2)
            amount = round(rng.uniform(1.0, 10_000.0), 4)
            price = round(rng.uniform(0.5, 2.0), 6)
            close_time = start + timedelta(seconds=rng.uniform(0, spread_hours * 3600))
            ledger += 1
            trades.append(
                TradeFactory.trade(
                    ledger_seq=ledger,
                    ledger_close_time=close_time,
                    base_account=base_acc,
                    counter_account=counter_acc,
                    base_asset=base_asset,
                    counter_asset=counter_asset,
                    base_amount=amount,
                    price=price,
                    base_is_seller=i % 2 == 0,
                )
            )
        return trades
