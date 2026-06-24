---
title: "Add AMM Liquidity Pool Event Ingestion for SDEX Constant-Product Pools"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
Stellar's SDEX introduced AMM (Automated Market Maker) constant-product liquidity pools, and wash trading on AMMs differs structurally from order-book wash trading — it involves coordinated deposit, swap, and withdrawal cycles rather than matched buy/sell orders. LedgerLens currently has no AMM event ingestion, leaving a significant detection gap for this attack surface. `ingestion/amm_loader.py` needs to be built from scratch to ingest pool deposit, withdrawal, and swap events and align them with the existing `Trade` schema for downstream feature engineering.

## Background & Context
Stellar's Horizon API exposes AMM liquidity pool operations through multiple endpoints:
- `GET /liquidity_pools` — lists all pools with current reserves
- `GET /liquidity_pools/{pool_id}/transactions` — all transactions touching a pool
- `GET /liquidity_pools/{pool_id}/operations` — individual operations (deposit, withdraw, swap)
- `GET /liquidity_pools/{pool_id}/trades` — swap operations in trade format

AMM wash trading patterns differ from order-book patterns:
1. **Deposit-swap-withdraw cycle**: attacker deposits large liquidity, executes self-swaps to inflate pool volume metrics, then withdraws. The deposit/withdraw operations cancel out economically, but the swap volume appears on aggregator dashboards.
2. **Circular pool routing**: attacker routes a path payment through multiple pools they control, recording artificial volume in each pool.
3. **Sandwich coordination**: attacker front-runs their own large trade using a second wallet to extract MEV-like profits while inflating volume.

The AMM swap events are already partially exposed via the `/trades` endpoint with `trade_type: "liquidity_pool"`, but deposit and withdrawal events are not, and the `liquidity_pool_id` field is not captured in the current `Trade` model. The `amm_loader.py` module must ingest all three event types and produce records that the feature engineering pipeline can process.

## Objectives
- [ ] Implement `AMMLoader` class in `ingestion/amm_loader.py` that ingests pool deposit, withdrawal, and swap events from the Horizon `/liquidity_pools/{pool_id}/operations` endpoint.
- [ ] Extend `ingestion/data_models.py` with `LiquidityPoolEvent` (covering deposit and withdrawal) and extend `Trade` with optional `liquidity_pool_id: str | None` and `trade_type` fields to accommodate AMM swaps.
- [ ] Implement `AMMPoolRegistry` that fetches and caches the list of active pools from `/liquidity_pools`, with periodic refresh and filtering by minimum TVL (Total Value Locked) threshold to avoid ingesting dust pools.
- [ ] Add AMM-specific feature extraction hooks in `detection/feature_engineering.py`: `deposit_withdraw_imbalance`, `pool_self_swap_rate`, and `round_trip_via_pool` boolean indicator.

## Technical Requirements

**`LiquidityPoolEvent` schema:**
```python
class LiquidityPoolEventType(str, Enum):
    DEPOSIT = "liquidity_pool_deposit"
    WITHDRAW = "liquidity_pool_withdraw"

class LiquidityPoolEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    paging_token: str
    transaction_hash: str
    ledger_close_time: datetime
    pool_id: str
    account: str
    event_type: LiquidityPoolEventType
    reserves_deposited: list[PoolReserve] | None = None   # for deposits
    reserves_received: list[PoolReserve] | None = None    # for withdrawals
    shares_amount: Decimal                                 # LP tokens minted/burned
    min_price: Decimal | None = None
    max_price: Decimal | None = None

class PoolReserve(BaseModel):
    asset: str          # e.g. "native" or "USDC:GABC..."
    amount: Decimal
```

**`Trade` model extension** (backward-compatible):
```python
# Add to existing Trade model in data_models.py:
liquidity_pool_id: str | None = None
trade_type: Literal["orderbook", "liquidity_pool"] = "orderbook"
```

**`AMMPoolRegistry`:**
```python
class AMMPoolRegistry:
    def __init__(
        self,
        client: RetryingHorizonClient,
        min_tvl_xlm: Decimal = Decimal("1000"),   # ignore dust pools
        refresh_interval_seconds: int = 3600,
    ): ...

    async def get_active_pools(self) -> list[PoolInfo]: ...
    async def refresh(self) -> int:
        """Refresh pool list. Returns number of pools fetched."""
    def get_pool(self, pool_id: str) -> PoolInfo | None: ...

@dataclass
class PoolInfo:
    pool_id: str
    asset_a: str
    asset_b: str
    tvl_xlm: Decimal
    total_shares: Decimal
    last_refreshed: datetime
```

**`AMMLoader`:**
```python
class AMMLoader:
    def __init__(
        self,
        client: RetryingHorizonClient,
        registry: AMMPoolRegistry,
        storage: RiskScoreStore,
    ): ...

    async def load_pool_events(
        self,
        pool_id: str,
        since_cursor: str = "0",
        limit_per_page: int = 200,
    ) -> PoolLoadResult: ...

    async def load_all_pools(
        self,
        concurrency: int = 4,
    ) -> dict[str, PoolLoadResult]: ...
```

**Deposit/withdraw imbalance feature**: for a given wallet and time window, compute:
```
deposit_withdraw_imbalance = |total_deposited_xlm - total_withdrawn_xlm| / (total_deposited_xlm + total_withdrawn_xlm)
```
Values near 0 indicate balanced deposit/withdraw (normal LPs); values near 1 indicate one-sided (potentially wash trading).

**Pool self-swap rate**: fraction of swaps in a pool where the `account` that deposited is also the account that swapped:
```
pool_self_swap_rate = self_swap_count / total_swap_count
```

**Horizon endpoint pagination**: AMM operations are paginated via cursor. Use the same cursor-based pagination as the trade loader, with the same `RetryingHorizonClient`.

**Minimum TVL filtering**: pools with TVL below `min_tvl_xlm` should be skipped — tiny pools are often test deployments or dust and would add noise to the feature computation.

**Configuration** (add to `config/settings.py`):
- `AMM_LOADER_ENABLED`: default `True`
- `AMM_MIN_TVL_XLM`: default `1000.0`
- `AMM_POOL_REFRESH_INTERVAL_SECONDS`: default `3600`
- `AMM_LOADER_CONCURRENCY`: default `4`

## Security Considerations
- `pool_id` values from the Horizon response must be validated as 64-character hex strings before being used in subsequent API calls or SQL queries to prevent injection attacks.
- The `AMMPoolRegistry` cache must have a maximum size limit (e.g., 10,000 pools) to prevent memory exhaustion on networks with many pools.
- All `amount` and `shares` fields must use `Decimal` (not `float`) to prevent floating-point precision errors in the feature engineering computations.
- Pool TVL values used for filtering must be fetched from Horizon, not computed locally — local computation could be manipulated by an attacker who controls the pool data feed.
- SQL INSERT statements for `LiquidityPoolEvent` records must use parameterised queries to prevent SQL injection from attacker-controlled pool IDs or account addresses.

## Testing Requirements
- Unit tests covering `LiquidityPoolEvent` Pydantic model: valid deposit, valid withdrawal, missing required field, invalid `event_type`
- Unit tests covering `AMMPoolRegistry.get_active_pools()`: mock Horizon response with 3 pools (2 above TVL threshold, 1 below); assert only 2 returned
- Unit tests covering `AMMLoader.load_pool_events()`: mock paginated operations endpoint; assert all pages fetched, correct `PoolLoadResult` totals
- Unit tests covering deposit/withdraw imbalance feature: balanced wallet (score ~0), one-sided depositor (score ~1)
- Unit tests covering pool self-swap rate: 0% self-swap, 100% self-swap, mixed case
- Integration tests: mock full Horizon AMM endpoints for 2 pools; run `AMMLoader.load_all_pools()`; assert events written to storage, no duplicates
- Edge cases: pool with zero swaps, pool with identical deposit and withdrawal amounts (imbalance=0), pool with only one asset type (edge case in reserve list), Horizon returning 404 for a pool that was just deleted
- Performance benchmark: loading events for 100 pools with 50 events each should complete in < 30 seconds with `concurrency=4`

## Documentation Requirements
- Add module docstring to `ingestion/amm_loader.py` explaining AMM wash-trading patterns and the detection approach
- Add docstrings to `AMMLoader`, `AMMPoolRegistry`, and `LiquidityPoolEvent`
- Update `README.md` feature groups section to include the new AMM-specific features
- Update `docs/ingestion.md` with a section on AMM event ingestion, pool filtering, and the relationship to the order-book ingestion path

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Stellar AMM/liquidity pool mechanics, Horizon AMM API endpoints, Python async HTTP, DeFi AMM wash trading detection
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Developer with hands-on experience with Stellar's liquidity pool (AMM) operations and the Horizon API's AMM endpoints. Understanding of constant-product AMM mechanics, LP token accounting, and DeFi wash-trading detection techniques. Strong Python async and Pydantic skills required.
