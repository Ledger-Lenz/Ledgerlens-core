# Streaming Feature Store Architecture

## Overview

The LedgerLens Streaming Feature Store provides efficient, incremental per-trade feature computation using a Redis hot layer and SQLite cold layer. This replaces the previous full-recompute approach that rescanned all historical trades for each wallet on every scoring pass.

### Problem Solved

**Previous Approach (Inefficient):**
- `run_pipeline.py` loaded full 30-day trade history for each wallet on every scoring pass
- `build_feature_vector()` recomputed all rolling windows (1h, 4h, 24h, 7d, 30d) from scratch
- For wallets with thousands of trades, this was expensive and didn't scale

**New Approach (Incremental):**
- Feature state is cached in Redis (hot layer) with 48-hour TTL
- When a new trade arrives from Horizon SSE stream, only a small delta is applied to existing feature state
- Rolling-window aggregates are maintained in memory using circular deque buffers
- Expired entries are automatically pruned based on time windows
- Periodic flush to SQLite (cold layer) for persistent history

## Architecture Diagram

```
                    Horizon SSE Stream
                            ↓
                    (Trade events)
                            ↓
              ┌─────────────────────────────┐
              │  stream_trades_with_cursor  │
              └──────────────┬──────────────┘
                             ↓
              ┌──────────────────────────────┐
              │  update_feature_state()      │ ← Incremental update per trade
              │  + derive_feature_vector()   │   (3x faster than full recompute)
              └──────────┬───────────────────┘
                         ↓
        ┌────────────────────────────────────────┐
        │         Redis HOT layer                │
        │  (48h TTL, in-memory, per wallet)      │
        │                                        │
        │  Key: ll:feature:<SHA256(wallet:pair)> │
        │  Value: WalletFeatureState (JSON)      │
        └────────┬───────────────────────────────┘
                 ↓
        (every 5 min, configurable)
         Periodic flush to SQLite
                 ↓
        ┌────────────────────────────────────────┐
        │        SQLite COLD layer               │
        │  (persistent queryable history)        │
        │                                        │
        │  Table: wallet_feature_states          │
        │  Fields: wallet, asset_pair,           │
        │          state_json, last_updated      │
        └────────────────────────────────────────┘
```

## Core Components

### 1. WalletFeatureState Model

Pydantic model storing the current rolling-window aggregates for a wallet/asset-pair:

```python
class WalletFeatureState(BaseModel):
    wallet: str
    asset_pair: str
    last_updated: datetime
    trade_count: int
    
    # Ring buffers (max cap to bound memory)
    trade_ring_1h: list[tuple[int, float]]    # max 10,000 entries
    trade_ring_4h: list[tuple[int, float]]    # max 40,000 entries
    trade_ring_24h: list[tuple[int, float]]   # max 100,000 entries
    trade_ring_7d: list[tuple[int, float]]    # max 500,000 entries
    trade_ring_30d: list[tuple[int, float]]   # max 1,000,000 entries
    
    # Benford digit frequency counts (indices 0-8 map to digits 1-9)
    benford_digit_counts_30d: list[int]
    
    # Hashed counterparty wallet IDs (32-bit integers for space efficiency)
    counterparty_hashes_30d: list[int]
```

**Why Ring Buffers?**
- Store (timestamp_us, amount) tuples for O(1) append
- Old entries are pruned when exceeding time window
- Ring overflow uses FIFO eviction to maintain memory bounds

**Why Hashed Counterparties?**
- Wallet addresses hashed to 32-bit integers to bound memory
- Prevents accidental exposure of wallet addresses in Redis SCAN output

### 2. Incremental Update Functions

#### `update_feature_state(state, trade) -> WalletFeatureState`

Atomically updates feature state with a new trade:

1. Add trade to appropriate ring buffers (1h, 4h, 24h, 7d, 30d)
2. Check ring overflow and evict oldest entry if at capacity
3. Prune expired entries from each ring (older than window threshold)
4. Update Benford digit counts incrementally (append first significant digit)
5. Update counterparty hashes incrementally (append if not duplicate)
6. Increment trade_count and update last_updated

**Performance:** O(1) per trade (after O(window_size) amortized pruning)

#### `derive_feature_vector(state) -> dict[str, float]`

Computes feature vector from cached ring buffers without I/O:

1. Extract Benford metrics from digit counts for each rolling window
2. Compute volume-to-unique-counterparty ratio from ring buffers
3. Return features as dict matching `build_feature_vector()` output

**Note:** Some features requiring full trade history (e.g., ring membership, account graph) are not computed incrementally and remain 0.0. These are cached separately or computed periodically in batch.

### 3. Redis Hot Layer

#### `FeatureStore` Class

Wraps Redis connection with fallback to in-process dict:

```python
class FeatureStore:
    def get_state(wallet, asset_pair) -> WalletFeatureState | None
    def set_state(state) -> None
    def delete_state(wallet, asset_pair) -> None
    def scan_all_keys() -> list[str]
    def is_using_redis() -> bool
```

**Key Format:** `ll:feature:<SHA256(wallet:asset_pair)>`
- SHA-256 hash prevents wallet address exposure in Redis SCAN output
- `ll:feature:` prefix allows Redis key-space eviction policies to target these keys

**TTL:** Configurable via `FEATURE_STORE_TTL_HOURS` (default 48 hours)
- Wallets that have not traded in 48 hours are evicted from Redis
- Must be rebuilt from cold storage on next access

**Fallback Mode:** If Redis is unavailable:
- Operations are served from in-process dict
- Dict has LRU eviction cap (default 10,000 entries)
- Logged as WARNING; pipeline continues functioning

**TLS Support:** Redis connection URL can use `rediss://` scheme for TLS

### 4. SQLite Cold Layer

#### Cold Storage Migration

Database migration v8 creates `wallet_feature_states` table:

```sql
CREATE TABLE wallet_feature_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    asset_pair TEXT NOT NULL,
    state_json TEXT NOT NULL,
    last_updated TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_wallet_feature_states_key ON wallet_feature_states (wallet, asset_pair);
CREATE INDEX idx_wallet_feature_states_updated ON wallet_feature_states (last_updated);
```

#### Functions

**`save_feature_state(state, db_path=None)`**
- Persists WalletFeatureState JSON to SQLite
- INSERT OR REPLACE pattern for idempotent updates

**`get_feature_state(wallet, asset_pair, db_path=None) -> WalletFeatureState | None`**
- Retrieves state from SQLite
- Returns None if not found

**`promote_cold_to_hot(feature_store, batch_size=100, db_path=None) -> int`**
- Loads most recently updated states from SQLite
- Writes them to Redis hot layer
- Returns count of states promoted
- Used during periodic cold-to-hot flush

### 5. Pipeline Integration

#### Streaming Loop Enhancement

`run_pipeline.py:run_streaming()` now uses the feature store:

1. For each Trade from `stream_trades_with_cursor()`:
   - Load wallet's WalletFeatureState from Redis (hot) or SQLite (cold) or initialize new
   - Call `update_feature_state(state, trade)`
   - Write updated state back to Redis
   - If batch threshold or time interval exceeded, derive features and score

2. Periodic background task (configurable via `FEATURE_STORE_FLUSH_INTERVAL_SECONDS`, default 5 min):
   - Flushes hot feature states to cold SQLite storage
   - Optionally promotes recently updated states back to Redis from cold tier

```python
def _maybe_flush_feature_store_to_cold():
    """Periodically flush hot feature states to cold storage."""
    if now - last_flush_time >= flush_interval:
        promote_cold_to_hot(feature_store)  # Sync cold → hot for recent states
        save_feature_state(state)  # Persist hot states to cold
        last_flush_time = now
```

## Configuration

### Environment Variables

Add to `.env`:

```bash
# Feature Store (Redis hot layer)
REDIS_URL=redis://localhost:6379/0           # Or rediss:// for TLS
FEATURE_STORE_TTL_HOURS=48                   # Default: 48 hours

# Feature Store (SQLite cold layer)
FEATURE_STORE_FLUSH_INTERVAL_SECONDS=300     # Default: 5 minutes (300s)

# Streaming
CURSOR_PATH=./horizon_cursor.txt             # Path to cursor file for resumption
```

### Python Settings (`config/settings.py`)

```python
redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
feature_store_ttl_hours: int = int(os.getenv("FEATURE_STORE_TTL_HOURS", "48"))
feature_store_flush_interval_seconds: int = int(os.getenv("FEATURE_STORE_FLUSH_INTERVAL_SECONDS", "300"))
cursor_path: str = os.getenv("CURSOR_PATH", "./horizon_cursor.txt")
```

## Memory Bounds

Ring buffer size caps to prevent OOM:

| Window | Max Entries |  Approx Mem @ ~20B/entry |
|--------|-------------|-------------------------|
| 1h     | 10,000      | ~200 KB per wallet      |
| 4h     | 40,000      | ~800 KB per wallet      |
| 24h    | 100,000     | ~2 MB per wallet        |
| 7d     | 500,000     | ~10 MB per wallet       |
| 30d    | 1,000,000   | ~20 MB per wallet       |

**Fallback dict cap:** 10,000 entries (LRU eviction)
- Prevents OOM if Redis is unavailable for extended period
- Each entry ~2-5 KB depending on trade history depth

## Fallback Behavior

If Redis is unavailable at startup or during operation:

1. **At Startup:**
   - Try to connect to Redis
   - If connection fails, log WARNING and fall back to in-process dict
   - Pipeline continues functioning

2. **During Operation:**
   - Any Redis operation failure triggers fallback to dict
   - Logged as WARNING per operation
   - Pipeline continues functioning
   - On Redis recovery, states remain in dict until evicted

3. **Performance Impact:**
   - In-process dict is slower than Redis (network round-trip eliminated, but no persistence)
   - Risk of data loss if process crashes while using fallback

## Testing

### Test Suites

**`tests/test_feature_store.py`** (Basic functionality)
- Incremental update adds trades to correct ring buffers
- Expired entries are pruned
- Ring overflow evicts oldest entry (FIFO)
- `derive_feature_vector()` vs `build_feature_vector()` agree to within 1e-6
- Serialization/deserialization roundtrip
- Feature equivalence on synthetic data

**`tests/test_feature_store_redis.py`** (Redis integration)
- set_state() / get_state() with fakeredis
- TTL is set on set_state()
- scan_all_keys() returns all stored keys
- Redis unavailability falls back to in-process dict

**`tests/test_feature_store_cold.py`** (Cold storage)
- save_feature_state() / get_feature_state() roundtrip
- promote_cold_to_hot() writes correct number of states to mock Redis
- Insert-or-replace semantics on duplicate save

**`tests/benchmark_feature_store.py`** (Performance)
- Incremental path ≥ 3× faster than full recompute
- Feature values agree to within 1e-6

### Running Tests

```bash
# Unit tests
pytest tests/test_feature_store.py -v
pytest tests/test_feature_store_redis.py -v
pytest tests/test_feature_store_cold.py -v

# Benchmark
pytest tests/benchmark_feature_store.py -v

# All pipeline tests
pytest tests/test_pipeline.py -v -k "streaming or feature"

# Full test suite
make test
```

## Security

### Wallet Address Hashing

- **Why:** Prevent accidental exposure of wallet addresses in Redis SCAN output
- **Implementation:** SHA-256(wallet + asset_pair) → hex digest
- **Key Format:** `ll:feature:<hash>` (not `ll:feature:<wallet>:<asset_pair>`)

### Redis TLS

- Supported via `rediss://` URL scheme
- Example: `rediss://username:password@redis.example.com:6380/0`
- Certificates validated by default; can be customized via Redis client options

### In-Process Fallback Limits

- Max 10,000 entries to prevent OOM
- LRU eviction of least-recently-used states
- Clear logging when fallback is active

## Limitations & Future Work

### Current Limitations

1. **Incomplete Feature Set:** Some features require full trade history or external data:
   - `wash_ring_membership`, `network_centrality` (require graph analysis)
   - `funding_source_similarity_score` (requires account metadata)
   - These remain 0.0 in incremental computation

2. **Single Ring Per Window:** Cannot compute percentile-based features (e.g., P95 volume spike)

3. **No Cross-Asset State:** Cross-pair features computed in batch only

### Recommended Enhancements

1. **Lua Scripting:** Use Redis Lua scripts for atomic multi-key operations
2. **Partitioning:** Shard feature states by wallet hash for horizontal scaling
3. **Time-Series DB:** Consider InfluxDB or TimescaleDB for time-series aggregation
4. **Streaming Aggregation:** Use Kafka/Faust for multi-stage stream processing
5. **Graph Store:** Dedicate graph database (Neo4j) for wallet interaction graphs

## References

- **Issue #41:** Streaming Feature Store with Redis Hot Layer and Incremental Per-Trade Feature Updates
- **Feast** (Uber): https://feast.dev
- **Tecton**: https://www.tecton.ai
- **Redis:** https://redis.io
- **Benford's Law:** NIST SP 800-32 / Digital Forensics
