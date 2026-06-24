---
title: "Implement Path-Payment Multi-Hop Trade Decomposition in the Ingestion Layer"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
Stellar's path payment operations route trades through multiple intermediate assets in a single atomic transaction, but LedgerLens currently ingests these as a single opaque record rather than decomposing them into individual hop trades. This means that wash-trading rings that exploit path payment routing — using intermediate assets to obscure the round-trip structure — are invisible to the graph-based ring detection engine (`detection/graph_engine.py`). `ingestion/path_payment_loader.py` must decompose multi-hop path payments into a sequence of atomic `Trade` records, one per hop, so the full circular routing is visible to Tarjan SCC analysis.

## Background & Context
A Stellar path payment `A → [USDC → BTC → ETH] → B` is a single Horizon operation that atomically swaps asset `A` for asset `B` via the intermediate assets USDC, BTC, and ETH. The Horizon API returns this as a single `PathPaymentStrictSend` or `PathPaymentStrictReceive` operation. However, each hop in the path was independently matched against an order book or liquidity pool.

Horizon's `/operations` endpoint for path payments includes `path` — the list of intermediate assets — and the Horizon `/effects` endpoint for path payment operations includes per-hop offer/trade details. The decomposition must use these `effects` records to reconstruct the individual hop trades.

Why this matters for wash-trading detection:
- **Ring detection gap**: a ring A→B→A executed via path payment A→[X]→B and B→[X]→A appears as two operations with no direct edge between A and B in the trade graph. The graph engine cannot detect this as a ring.
- **Volume inflation via routing**: a wash trader can inflate volume for multiple intermediate assets simultaneously with a single path payment, counting the same notional value as volume for each hop.
- **Benford evasion**: path payment amounts are partially determined by on-chain order book prices, making them appear more Benford-compliant than direct wash trades with fixed amounts.

The `path_payment_loader.py` module must handle both `PathPaymentStrictSend` and `PathPaymentStrictReceive` operation types and produce `Trade` records for each hop that are structurally identical to regular order-book trades so the detection engine needs no changes.

## Objectives
- [ ] Implement `PathPaymentLoader` in `ingestion/path_payment_loader.py` that fetches path payment operations from Horizon's `/operations?type=path_payment_strict_send&type=path_payment_strict_receive` endpoint.
- [ ] Implement `PathPaymentDecomposer` that takes a single `PathPaymentOperation` and its associated `effects` records and emits N `Trade` objects (one per hop), linking them via a shared `path_payment_id` field.
- [ ] Extend `data_models.py` with `PathPaymentOperation` and `PathPaymentHop` schemas, and add `path_payment_id: str | None` and `hop_index: int | None` fields to the `Trade` model.
- [ ] Add a `path_payment_frequency` feature to `detection/feature_engineering.py`: the fraction of a wallet's trades that originated from path payments (high values indicate potential path-payment-based wash trading).

## Technical Requirements

**Horizon path payment operation structure** (from Horizon API):
```json
{
  "type": "path_payment_strict_send",
  "id": "...",
  "paging_token": "...",
  "transaction_hash": "...",
  "source_account": "GABC...",
  "destination_account": "GDEF...",
  "destination_asset_type": "credit_alphanum4",
  "destination_asset_code": "USDC",
  "destination_asset_issuer": "GABC...",
  "source_asset_type": "native",
  "source_amount": "100.0000000",
  "destination_min": "95.0000000",
  "destination_amount": "97.5432100",
  "path": [
    {"asset_type": "credit_alphanum4", "asset_code": "BTC", "asset_issuer": "GXYZ..."},
    {"asset_type": "credit_alphanum4", "asset_code": "ETH", "asset_issuer": "GUVW..."}
  ]
}
```

**`PathPaymentOperation` schema:**
```python
class PathPaymentOperation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    paging_token: str
    transaction_hash: str
    ledger_close_time: datetime
    source_account: str
    destination_account: str
    source_asset: Asset
    destination_asset: Asset
    source_amount: Decimal
    destination_amount: Decimal
    path: list[Asset]    # intermediate assets; may be empty (direct swap)
    operation_type: Literal["path_payment_strict_send", "path_payment_strict_receive"]
```

**`PathPaymentDecomposer.decompose()` algorithm:**
```python
def decompose(
    self,
    operation: PathPaymentOperation,
    effects: list[TradeEffect],
) -> list[Trade]:
    """
    Reconstruct individual hop trades from the effects list.
    Each TradeEffect has: sold_asset, sold_amount, bought_asset, bought_amount, account.
    
    The hops form a chain:
      hop 0: source_asset → path[0]
      hop 1: path[0]      → path[1]
      ...
      hop N: path[-1]     → destination_asset
    
    Each hop becomes a Trade with:
      - base_account = operation.source_account (for all hops)
      - counter_account = operation.destination_account (for all hops)
      - base_asset = sold_asset for this hop
      - counter_asset = bought_asset for this hop
      - path_payment_id = operation.id
      - hop_index = hop position (0-indexed)
    """
```

**`TradeEffect` schema** (from Horizon `/effects?type=trade` for the operation):
```python
class TradeEffect(BaseModel):
    id: str
    account: str
    sold_asset_type: str
    sold_asset_code: str | None = None
    sold_asset_issuer: str | None = None
    sold_amount: Decimal
    bought_asset_type: str
    bought_asset_code: str | None = None
    bought_asset_issuer: str | None = None
    bought_amount: Decimal
```

**Trade model extension** (backward-compatible):
```python
# Add to Trade model:
path_payment_id: str | None = None  # ID of the originating path payment operation
hop_index: int | None = None        # Position in the path (0 = first hop)
```

**Effect-to-hop matching**: Horizon returns effects in execution order. The decomposer must match effects to hops by checking that `sold_asset` matches the expected input asset for each hop position. If effects do not match the expected chain (e.g., Horizon bug or path change), log a `WARNING` and skip the operation rather than producing incorrect hop trades.

**Direct path payments** (empty `path` list): if `path` is empty, the operation is equivalent to a single-hop trade (source → destination directly). Produce a single `Trade` with `hop_index=0`.

**`path_payment_frequency` feature:**
```python
def compute_path_payment_frequency(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    path_payment_trades = sum(1 for t in trades if t.path_payment_id is not None)
    return path_payment_trades / len(trades)
```

**Configuration** (add to `config/settings.py`):
- `PATH_PAYMENT_LOADER_ENABLED`: default `True`
- `PATH_PAYMENT_FETCH_EFFECTS`: default `True` (fetch per-operation effects for accurate decomposition; set `False` for approximate decomposition from operation data alone)

## Security Considerations
- `path_payment_id` must be validated as a numeric string (Horizon operation IDs are large integers) before use in SQL queries.
- The effects endpoint may return more `TradeEffect` records than expected (e.g., from fee-bump transactions). The decomposer must validate that the number of effects matches `len(path) + 1` and reject operations where this invariant is violated, logging the discrepancy.
- Trade amounts from path payment effects must be validated to be positive and within reasonable bounds (e.g., < 10^15 XLM) to catch any parsing bugs that could produce astronomical feature values.
- The `hop_index` field is populated by the decomposer (not from Horizon directly) and must be bounded to `[0, len(path)]` to prevent out-of-range values reaching the feature store.

## Testing Requirements
- Unit tests covering `PathPaymentOperation` schema: valid 2-hop payment, direct payment (empty path), missing effects list
- Unit tests covering `PathPaymentDecomposer.decompose()`: 2-hop payment produces 2 `Trade` objects with correct assets and amounts, direct payment produces 1 `Trade`, effects count mismatch returns empty list with warning
- Unit tests covering `path_payment_frequency`: 0% (no path payments), 100% (all path payments), mixed
- Integration tests: mock Horizon `/operations` + `/effects` endpoints for a 3-hop path payment; run `PathPaymentLoader`; assert 3 `Trade` records written with correct `hop_index` values (0, 1, 2)
- Integration tests: verify graph engine receives decomposed hops and detects the correct ring structure (mock `graph_engine.py`)
- Edge cases: path payment with `path=[]` (direct), path with repeated asset (A→B→A via USDC), effects returning assets in unexpected order, Horizon returning 0 effects for a valid operation
- Performance benchmark: decomposing 1,000 path payments with 3 hops each should complete in < 1 second

## Documentation Requirements
- Add module docstring to `ingestion/path_payment_loader.py` explaining the decomposition algorithm and why it matters for ring detection
- Add docstrings to `PathPaymentDecomposer.decompose()` with a worked example showing input effects and output trades
- Update `README.md` feature groups section to include `path_payment_frequency`
- Update `docs/ingestion.md` with a section on path payment decomposition, effect-to-hop matching, and the `PATH_PAYMENT_FETCH_EFFECTS` configuration trade-off

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Stellar path payments, Horizon operations/effects API, graph algorithms, Python async
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Developer with deep knowledge of Stellar's path payment mechanism and the Horizon effects API. Understanding of how path payments interact with order books and AMM pools is essential. Experience with graph-based fraud detection or trade decomposition pipelines is a strong plus.
