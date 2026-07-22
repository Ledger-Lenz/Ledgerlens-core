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

## Fuzz Testing

LedgerLens uses [Atheris](https://github.com/google/atheris) coverage-guided fuzzing
alongside the Hypothesis property-based test suite. The two approaches are genuinely
complementary:

| Approach | How inputs are generated | What it finds |
|----------|--------------------------|---------------|
| **Hypothesis** (strategy-driven) | Test author defines explicit strategies (`st.text()`, composite nested objects) | Edge cases *within* the defined strategy space: valid boundary values, missing required fields, malformed numeric strings |
| **Atheris** (coverage-guided) | libFuzzer instruments real code paths and mutates inputs to reach new branches | Inputs that strategy authors never considered: adversarial float strings (`"1e400"`), deeply nested JSON, byte sequences that crash binary parsers, off-by-one errors in length checks |

Neither replaces the other. Hypothesis proves your defined expectations; Atheris finds
what you forgot to expect.

### Harnesses

One harness per parser entrypoint lives in the `fuzz/` directory:

| Harness | Target |
|---------|--------|
| `fuzz/fuzz_trade_parser.py` | `Trade.model_validate()` |
| `fuzz/fuzz_asset_parser.py` | `Asset.model_validate()` |
| `fuzz/fuzz_orderbook_event_parser.py` | `OrderBookEvent.model_validate()` |
| `fuzz/fuzz_evm_rpc_parser.py` | `UniswapV3Adapter._parse_swap_event` + `CurveAdapter._parse_exchange_event` |
| `fuzz/fuzz_solana_vaa_parser.py` | `_extract_stellar_address_from_vaa` + `_stellar_pubkey_to_address` + `_crc16_xmodem` |

### Running fuzz tests locally

```bash
# Install atheris (one-time):
pip install atheris

# Quick pre-merge smoke check (30s per harness):
make fuzz-quick
# or via CLI:
python cli.py fuzz-check --duration 30

# Full session on a single harness (60s):
python fuzz/fuzz_trade_parser.py fuzz/corpus/fuzz_trade_parser -max_total_time=60
```

### Nightly CI

The `.github/workflows/nightly_fuzz.yml` scheduled job runs each harness for 300s and
uploads crash inputs as a `fuzz-crashes` build artifact. The corpus is persisted across
runs via `actions/cache` (keyed on `fuzz-corpus-`) so the fuzzer builds on previous
findings rather than starting from scratch each night.

### When to add a new harness

Add a harness whenever a new function parses externally-controlled data:
1. Create `fuzz/fuzz_<target>.py` following an existing harness as a template.
2. Catch only exceptions already treated as "expected" in the production ingestion code.
3. Add the harness to `.github/workflows/nightly_fuzz.yml`.
4. Add a smoke-test case to `tests/test_fuzz_harness_smoke.py`.
5. Update `fuzz/README.md`.

### Handling a crash finding

1. Download the `fuzz-crashes` artifact from the failed CI run.
2. Reproduce: `python fuzz/fuzz_<harness>.py fuzz/corpus/crash-<hash>`
3. Minimise: `python fuzz/fuzz_<harness>.py -minimize_crash=1 -exact_artifact_path=crash-min fuzz/corpus/crash-<hash>`
4. Fix the root cause in the parser.
5. Add the minimised bytes as a regression case in `tests/test_data_models.py`:
   ```python
   def test_trade_parser_regression_issue_NNN():
       """Regression: crash found by fuzz_trade_parser, issue #NNN."""
       Trade.model_validate(json.loads(b"<minimised bytes>"))
   ```

## Migrating existing tests

`tests/test_feature_store.py`, `tests/test_pipeline.py`, and
`tests/test_streaming_scorer.py` have been migrated to build their
trades via `TradeFactory.trade(...)` instead of calling `Trade(...)`
directly — same field values, so test behavior is unchanged, but new
trades and edits to the id/paging-token scheme only need to happen in
one place. Use the same pattern (call `TradeFactory.trade(...)` with
your existing literal field values) when touching other ad hoc
`Trade(...)` constructions.


## Cross-Repo E2E Harness

### Overview

The `tests/e2e_cross_repo/` suite is a Testcontainers-based harness
that exercises the full end-to-end data flow:
core computes scores → ledgerlens-api serves them via REST →
scores above threshold are forwarded to the Soroban ledgerlens-score
contract.

This suite is **not part of the default pytest run** (marked with
`@pytest.mark.cross_repo_e2e` and excluded via `pyproject.toml`'s
`addopts = "-m 'not cross_repo_e2e'"`).

### Local Multi-Repo Directory Layout

To run locally, we recommend this sibling directory structure:
```
my-workspace/
├── ledgerlens-core/
├── ledgerlens-api/
└── ledgerlens-contracts/
```

### Environment Variables

- `LEDGERLENS_API_REPO_PATH`: Optional path to local ledgerlens-api
  checkout. Defaults to `../ledgerlens-api`.
- `LEDGERLENS_CONTRACTS_REPO_PATH`: Optional path to local
  ledgerlens-contracts checkout. Defaults to `../ledgerlens-contracts`.
- `CROSS_REPO_E2E_PINNED_REF`: Git ref to use when cloning sibling
  repos locally if no path is set. Defaults to `main`.

### Running Locally

1. Ensure Docker is running locally.
2. Set up the sibling directory layout as above (or set env vars
   accordingly).
3. Run:
   ```bash
   pytest -m cross_repo_e2e tests/e2e_cross_repo/
   ```

### What This Catches That `tests/e2e/` Doesn't

- Schema drift between core's `RiskScore` model and ledgerlens-api's
  response models.
- Integration issues between core's score output and ledgerlens-api's
  ingestion.
- Correctness of the Soroban contract's `submit_score` and `get_score`
  functions when fed real core scores.
