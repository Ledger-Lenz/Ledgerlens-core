# Testing Guide

## Overview

Tests across this codebase need realistic `Trade` sequences as input —
a wash-trading ring, a normal market maker, background noise, and so
on. Constructing those by hand with inline `Trade(...)` calls is
brittle (hard-coded amounts/timestamps drift out of sync with what the
test actually asserts) and hard to read. `tests/factories.py` provides
`TradeFactory`, a single source of truth for this kind of test data.

## `TradeFactory`

All methods are deterministic given the same `seed` (default `42`):
same accounts, amounts, timestamps, and Horizon-style trade ids every
run, so tests built on top of them never flake and diffs in CI are
meaningful.

### Scenario builders

```python
from tests.factories import TradeFactory

# 3 wallets handing round-lot amounts back and forth for 10 rounds —
# the classic wash-trading ring shape. Detectable via the same Benford /
# round-trip-frequency signals used in training (see
# ingestion.synthetic_data and ingestion.adversarial_data).
ring_trades = TradeFactory.wash_ring(n_accounts=3, n_rounds=10)

# One market-making account trading Benford-conforming amounts against
# many counterparties spread over 24h — the "looks completely normal" baseline.
normal_trades = TradeFactory.legitimate_market_maker(n_trades=50)

# Executed-trade footprint of a layering/spoofing attack: a handful of
# small trades at successively worse prices against rotating decoys.
# (Real spoofing is mostly cancelled orders — `OrderBookEvent`, not
# `Trade` — which this Trade-only factory does not model; see the
# method's docstring.)
spoof_trades = TradeFactory.spoofing_attack(n_layers=5)

# Uncorrelated random trades — background noise for tests that need
# "nothing interesting is happening" data.
noise_trades = TradeFactory.random_noise(n_trades=100)
```

Every scenario method accepts `seed=`, `as_of=` (defaults to a fixed
date so tests don't depend on wall-clock time), and `asset_pair=` to
override the default native-XLM/USDC pair.

### Low-level builder

When a test needs exact control over every field (e.g. migrating a
hand-written fixture one-for-one with zero behavior change),
`TradeFactory.trade(...)` builds a single `Trade` directly — every
scenario method above delegates to it:

```python
trade = TradeFactory.trade(
    id="trade_123",
    ledger_close_time=some_datetime,
    base_account="GA123",
    counter_account="GA456",
    base_amount=100.5,
    price=5.0,
)
```

If `id` is omitted, a realistic Horizon-style "total order ID" is
generated from `ledger_seq`/`tx_order`/`op_order` (ledger sequence in
the high 32 bits, mirroring how Horizon actually encodes trade
ids/paging tokens) — use `ledger_sequence_of(trade.id)` to recover the
ledger sequence from a factory-generated id.

## Migrating existing tests

`tests/test_feature_store.py`, `tests/test_pipeline.py`, and
`tests/test_streaming_scorer.py` have been migrated to build their
trades via `TradeFactory.trade(...)` instead of calling `Trade(...)`
directly — same field values, so test behavior is unchanged, but new
trades and edits to the id/paging-token scheme only need to happen in
one place. Use the same pattern (call `TradeFactory.trade(...)` with
your existing literal field values) when touching other ad hoc
`Trade(...)` constructions.
