---
title: "Build Streaming Feature Store with Redis Hot-Tier and SQLite Cold-Tier"
labels: ["difficulty: advanced", "area: infrastructure", "type: feature"]
assignees: []
---

## Summary

Extend `detection/feature_store.py` with a Redis-backed hot tier for sub-millisecond feature lookup during real-time scoring, with automatic promotion/eviction between the hot tier (Redis, 1-hour TTL) and the cold tier (SQLite, 30-day retention). Implement a write-through cache with asynchronous flush so that the inference path never blocks on SQLite I/O. This is a foundational infrastructure change that enables the streaming pipeline (`cli.py stream`) to sustain 500+ wallet scorings per second without latency spikes.

## Background & Context

`detection/feature_engineering.py` currently recomputes all 37 features from raw trade records on every scoring call. For the batch pipeline (`run_pipeline.py`) this is acceptable, but for `cli.py stream` — which scores wallets in rolling windows as trades arrive — recomputing from scratch on every window is prohibitively slow: at 500 trades/second with 10-wallet rolling windows, the engine must serve feature vectors in < 2ms end-to-end.

`detection/feature_store.py` exists as a stub. This issue implements the two-tier architecture:

- **Hot tier (Redis)**: recently scored wallets' feature vectors are cached with a 1-hour TTL. Reads are O(1) hash lookups (HGETALL). Writes are synchronous (write-through) on feature update.
- **Cold tier (SQLite)**: all feature vectors are persisted with a 30-day retention window, pruned nightly. This serves as the canonical store for drift monitoring and historical analysis.
- **Async flush**: a background asyncio task drains a write buffer to SQLite every 5 seconds, so the hot path never waits for disk I/O.
- **Promotion/eviction**: when a cache miss occurs on Redis (TTL expired or first access), the cold tier is consulted and the result is re-promoted to Redis.

The feature store must be transparent to callers: `feature_store.get(wallet)` returns a `FeatureVector` regardless of which tier served it.

## Objectives

- [ ] Implement `RedisHotTier` class wrapping `redis.asyncio.Redis` with serialisation, TTL management, and connection pooling
- [ ] Implement `SQLiteColdTier` class with write-buffer, async flush loop, and 30-day retention pruning
- [ ] Implement `FeatureStore` façade with `get`, `put`, `invalidate`, and `warm_up` methods
- [ ] Implement write-through semantics: `put` writes to Redis synchronously and enqueues to the SQLite write buffer
- [ ] Implement cache-miss promotion: on Redis miss, fetch from SQLite and re-promote to Redis
- [ ] Integrate `FeatureStore` into `detection/model_inference.py` as a drop-in replacement for the current inline dict
- [ ] Add `GET /admin/feature-store/stats` endpoint returning hit rate, miss rate, and tier sizes
- [ ] Write unit tests with a Redis mock (fakeredis) and SQLite in-memory database
- [ ] Benchmark: `get()` for a hot-tier hit in < 1ms p99; cold-tier promotion in < 10ms p99

## Technical Requirements

### Data structures

```python
# detection/feature_store.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import asyncio, json, time
import redis.asyncio as aioredis

@dataclass
class FeatureVector:
    wallet: str
    asset_pair: str
    features: dict[str, float]      # keyed by FEATURE_NAMES entries
    computed_at: datetime
    source: str = "cold"             # "hot" | "cold" | "computed"

    def to_redis_hash(self) -> dict[str, str]:
        """Serialise for HSET. All values are JSON-encoded strings."""
        return {k: json.dumps(v) for k, v in self.features.items()}

    @classmethod
    def from_redis_hash(cls, wallet: str, asset_pair: str, raw: dict) -> "FeatureVector":
        features = {k: json.loads(v) for k, v in raw.items() if not k.startswith("__")}
        computed_at_str = json.loads(raw.get("__computed_at", "null"))
        return cls(
            wallet=wallet,
            asset_pair=asset_pair,
            features=features,
            computed_at=datetime.fromisoformat(computed_at_str) if computed_at_str else datetime.utcnow(),
            source="hot",
        )
```

### Hot tier

```python
class RedisHotTier:
    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        ttl_seconds: int = 3600,
        key_prefix: str = "ll:fv:",
        pool_size: int = 10,
    ):
        self._pool = aioredis.ConnectionPool.from_url(
            redis_url, max_connections=pool_size, decode_responses=True
        )
        self._client = aioredis.Redis(connection_pool=self._pool)
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix

    def _key(self, wallet: str, asset_pair: str) -> str:
        return f"{self.key_prefix}{wallet}:{asset_pair}"

    async def get(self, wallet: str, asset_pair: str) -> Optional[FeatureVector]:
        raw = await self._client.hgetall(self._key(wallet, asset_pair))
        if not raw:
            return None
        return FeatureVector.from_redis_hash(wallet, asset_pair, raw)

    async def put(self, fv: FeatureVector) -> None:
        key = self._key(fv.wallet, fv.asset_pair)
        pipe = self._client.pipeline()
        pipe.hset(key, mapping=fv.to_redis_hash())
        pipe.expire(key, self.ttl_seconds)
        await pipe.execute()

    async def invalidate(self, wallet: str, asset_pair: str) -> None:
        await self._client.delete(self._key(wallet, asset_pair))
```

### Cold tier

```python
class SQLiteColdTier:
    FLUSH_INTERVAL_S = 5.0
    MAX_BUFFER_SIZE  = 1000

    def __init__(self, db_path: str, retention_days: int = 30): ...

    async def get(self, wallet: str, asset_pair: str) -> Optional[FeatureVector]:
        """Synchronous SQLite read wrapped in asyncio.to_thread."""
        ...

    async def put(self, fv: FeatureVector) -> None:
        """Enqueue to write buffer; does NOT block."""
        ...

    async def _flush_loop(self) -> None:
        """
        Background task: drain write buffer to SQLite every FLUSH_INTERVAL_S.
        Use executemany for batch inserts (INSERT OR REPLACE).
        Prune rows older than retention_days after each flush.
        """
        ...
```

### Feature store façade

```python
class FeatureStore:
    def __init__(self, hot: RedisHotTier, cold: SQLiteColdTier): ...

    async def get(self, wallet: str, asset_pair: str) -> Optional[FeatureVector]:
        """
        1. Try hot tier.
        2. On miss: try cold tier, promote result to hot tier.
        3. Return None if both miss (caller must compute).
        Track hit/miss counters for /admin/feature-store/stats.
        """
        ...

    async def put(self, fv: FeatureVector) -> None:
        """Write-through: hot (sync) + cold (async buffer)."""
        await asyncio.gather(self._hot.put(fv), self._cold.put(fv))

    async def warm_up(self, wallets: list[str], asset_pairs: list[str]) -> int:
        """
        Bulk-promote N most-recently-scored wallet/pair combinations from cold to hot.
        Returns number of vectors promoted.
        """
        ...
```

### SQLite schema

```sql
CREATE TABLE IF NOT EXISTS feature_vectors (
    wallet      TEXT NOT NULL,
    asset_pair  TEXT NOT NULL,
    features_json TEXT NOT NULL,
    computed_at TIMESTAMP NOT NULL,
    PRIMARY KEY (wallet, asset_pair)
);
CREATE INDEX IF NOT EXISTS idx_fv_computed ON feature_vectors(computed_at DESC);
```

### Configuration

```
FEATURE_STORE_REDIS_URL=redis://localhost:6379/0
FEATURE_STORE_REDIS_TTL_SECONDS=3600
FEATURE_STORE_REDIS_POOL_SIZE=10
FEATURE_STORE_COLD_RETENTION_DAYS=30
FEATURE_STORE_FLUSH_INTERVAL_S=5
FEATURE_STORE_MAX_BUFFER_SIZE=1000
```

### Stats endpoint

```python
@router.get("/admin/feature-store/stats")
async def feature_store_stats() -> dict:
    return {
        "hot_hit_rate": ...,
        "cold_hit_rate": ...,
        "total_misses": ...,
        "redis_key_count": ...,
        "sqlite_row_count": ...,
    }
```

## Security Considerations

- **Redis authentication**: `FEATURE_STORE_REDIS_URL` must support `redis://:password@host:port/db` and `rediss://` (TLS). Reject plaintext `redis://` URLs in production mode (`LEDGERLENS_ENV=production`)
- **Key namespace isolation**: all Redis keys must use the `ll:fv:` prefix to prevent collision with other services sharing the same Redis instance
- **Data serialisation**: feature values are floats; validate that deserialized values are finite (`math.isfinite`) before returning to the inference engine. Reject and log any NaN or Inf values from the cold tier
- **Write buffer overflow**: if the SQLite flush falls behind and the buffer exceeds `MAX_BUFFER_SIZE`, emit a `WARNING` log and start dropping oldest entries (not newest) — never block the hot path
- **Wallet address in Redis keys**: keys are not encrypted; do not store PII beyond Stellar public key strings (which are public by design). Keys must never contain raw secret keys
- **Connection pool exhaustion**: wrap all Redis operations in a `asyncio.wait_for` with a 200ms timeout; on timeout, log `WARNING` and fall through to the cold tier

## Testing Requirements

- [ ] Use `fakeredis.aioredis` (fakeredis package) to mock Redis in all unit tests — no live Redis required
- [ ] Use `aiosqlite` with `:memory:` for SQLite unit tests
- [ ] Test: hot-tier hit returns `source="hot"` and does not query cold tier
- [ ] Test: cold-tier promotion sets `source="cold"` and re-promotes to hot tier
- [ ] Test: double miss returns `None`
- [ ] Test: write-through — `put()` writes to both tiers; hot has TTL; cold buffer is non-empty
- [ ] Test: flush loop drains buffer within `FLUSH_INTERVAL_S * 2`
- [ ] Test: `warm_up()` promotes correct number of vectors
- [ ] Test: Redis timeout (simulated with fakeredis latency injection) falls through to cold tier
- [ ] Benchmark: hot-tier `get()` p99 < 1ms; cold-tier promotion p99 < 10ms (pytest-benchmark)

## Documentation Requirements

- [ ] Full docstrings on all public classes and methods
- [ ] Add `docs/feature_store.md` explaining the two-tier architecture, TTL rationale, write-through semantics, and operational runbook (how to flush the cold tier, warm up after Redis restart)
- [ ] Update `README.md` architecture diagram to show the feature store between ingestion and inference
- [ ] Document the `feature_vectors` SQLite schema in `docs/database_schema.md`
- [ ] Update `.env.example` with all six configuration variables with comments

## Definition of Done

- [ ] `detection/feature_store.py` fully implements `RedisHotTier`, `SQLiteColdTier`, and `FeatureStore`
- [ ] `detection/model_inference.py` uses `FeatureStore.get/put` instead of inline dict
- [ ] `GET /admin/feature-store/stats` endpoint live
- [ ] All unit tests pass with fakeredis/in-memory SQLite (no live Redis dependency in CI)
- [ ] Benchmarks pass (hot < 1ms p99, cold < 10ms p99)
- [ ] `cli.py db-migrate` creates `feature_vectors` table
- [ ] No new lint errors
- [ ] `docs/feature_store.md` authored

## For Contributors

**Ideal contributor profile**: You have production experience building two-tier caching systems (e.g., Redis + Postgres, Memcached + MySQL) and understand cache coherence, write-through vs write-back semantics, and async I/O patterns in Python. You are comfortable with `asyncio`, `redis.asyncio`, and `aiosqlite`. Experience with `fakeredis` for testing and `pytest-benchmark` for performance validation is a significant advantage.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "distributed caching systems", "async Python infrastructure", "Redis internals"
2. **Relevant experience** — specific systems where you built multi-tier caches; any production Redis deployments you have operated
3. **Approach / initial thoughts** — your thoughts on write-through vs write-back for this use case; how you would handle the Redis-down scenario during the inference path
4. **Estimated time** — breakdown by tier (hot, cold, façade, integration, tests, docs)
