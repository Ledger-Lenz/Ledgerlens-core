---
title: "Build Account Funding-Source Graph for Sybil Cluster Detection"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary

Extend `ingestion/account_loader.py` to trace wallet creation funding chains up to depth 5. Build a funding-source graph and cluster wallets that share a common funder within N hops. Export cluster membership as two new ML features (`sybil_cluster_id`, `sybil_cluster_size`) for the ML feature engineering pipeline. Sybil clusters are the structural backbone of coordinated wash-trading rings: without detecting that 50 wallets were all funded by the same root account, the graph engine sees them as independent actors.

## Background & Context

On Stellar, every new account must be funded with a minimum XLM balance by an existing account via a `create_account` operation. This creates an immutable, on-chain funding tree. Wash-trading operators typically fund dozens to hundreds of throw-away wallets from a small number of root accounts, creating a highly identifiable tree structure.

`ingestion/account_loader.py` currently fetches account metadata (creation time, home domain, sequence number) for individual wallets. It does not trace the funding chain or build the cross-wallet graph.

The Horizon API exposes the funding operation via `GET /accounts/{account_id}/operations?type=create_account`, which returns the creating account ID. This can be followed recursively up to depth 5 to build the full funding ancestry tree.

Cluster detection logic:
1. For each wallet in the current scoring batch, resolve its funding chain up to depth 5
2. Build a directed graph where edges point from funder to funded wallet
3. Find connected components: two wallets in the same component share a common funder within 5 hops
4. Report `sybil_cluster_id` (hash of sorted wallet set) and `sybil_cluster_size` (number of wallets in the component) per wallet

A cluster of ≥ 3 wallets from the same root funder, all trading with each other, is a strong wash-trade signal. This feature dramatically improves ML recall for Sybil-based ring attacks.

## Objectives

- [ ] Implement `FundingChainResolver` that resolves a wallet's funding ancestry up to depth 5 via Horizon
- [ ] Implement `FundingGraph` that builds and maintains the directed funder→funded graph across all resolved wallets
- [ ] Implement `SybilClusterDetector` using union-find to identify connected components in the funding graph
- [ ] Export `sybil_cluster_id` and `sybil_cluster_size` per wallet for injection into `feature_engineering.py`
- [ ] Add both features to `FEATURE_NAMES` in `feature_engineering.py`
- [ ] Cache resolved funding chains in SQLite to avoid re-fetching known ancestry
- [ ] Add `GET /sybil-clusters` endpoint in `api/main.py` listing detected clusters
- [ ] Write tests with a mock Horizon covering chain resolution, cycle detection (unlikely but possible via `manage_buy_offer` re-funding), and cluster merging

## Technical Requirements

### Data structures

```python
# ingestion/account_loader.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class FundingRecord:
    wallet: str
    funder: Optional[str]          # None for genesis accounts (no funder)
    funded_at: Optional[datetime]
    depth_from_target: int          # 0 = target wallet, 5 = 5 hops up

@dataclass
class SybilCluster:
    cluster_id: str                # SHA-256[:8] of sorted(wallets)
    root_funder: str               # highest-depth common ancestor
    wallets: list[str]
    cluster_size: int
    detected_at: datetime = field(default_factory=datetime.utcnow)
```

### Funding chain resolver

```python
class FundingChainResolver:
    def __init__(
        self,
        http_client,
        horizon_url: str,
        max_depth: int = 5,
        cache_store: "FundingCacheStore",
    ): ...

    async def resolve(self, wallet: str) -> list[FundingRecord]:
        """
        Resolve the funding ancestry of `wallet` up to max_depth hops.
        Check cache_store first; fetch from Horizon only on miss.
        Returns list of FundingRecord from wallet (depth=0) to root (depth=max_depth or genesis).
        Handles cycle guard: if a funder appears twice in the chain, stop (log WARNING).
        """
        chain = []
        current = wallet
        visited = set()
        for depth in range(self.max_depth + 1):
            if current in visited:
                logger.warning("Funding chain cycle detected at wallet %s", current[:10])
                break
            visited.add(current)
            record = await self._fetch_or_cache(current, depth)
            chain.append(record)
            if record.funder is None:
                break
            current = record.funder
        return chain
```

### Union-find for cluster detection

```python
class UnionFind:
    def __init__(self): ...
    def union(self, a: str, b: str) -> None: ...
    def find(self, a: str) -> str: ...    # returns canonical root
    def components(self) -> dict[str, list[str]]: ...  # root → [members]


class SybilClusterDetector:
    def __init__(self, min_cluster_size: int = 3): ...

    def ingest_chains(self, chains: list[list[FundingRecord]]) -> list[SybilCluster]:
        """
        1. For each chain, union all wallets that share a common ancestor at any depth.
        2. Find components with size >= min_cluster_size.
        3. Return SybilCluster records.
        """
        uf = UnionFind()
        for chain in chains:
            wallets_in_chain = [r.wallet for r in chain]
            for i in range(len(wallets_in_chain) - 1):
                uf.union(wallets_in_chain[i], wallets_in_chain[i + 1])
        return [
            SybilCluster(
                cluster_id=_cluster_id(members),
                root_funder=_find_root(members, chains),
                wallets=members,
                cluster_size=len(members),
            )
            for root, members in uf.components().items()
            if len(members) >= self.min_cluster_size
        ]
```

### Funding cache schema

```sql
CREATE TABLE IF NOT EXISTS funding_chain_cache (
    wallet      TEXT PRIMARY KEY,
    funder      TEXT,
    funded_at   TIMESTAMP,
    fetched_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Expire after 7 days (check in Python; no DB trigger needed)
```

### Feature integration

```python
# detection/feature_engineering.py  (additions)
FEATURE_NAMES = [
    # ... existing 37 features ...
    "sybil_cluster_size",   # Feature 38: 0.0 if not in any cluster
    "sybil_in_cluster",     # Feature 39: 1.0 if in a cluster of >= 3, else 0.0
]
```

Note: `sybil_cluster_id` is a string identifier (not a float) and is stored in metadata, not in the feature vector. `sybil_cluster_size` and `sybil_in_cluster` are the float-encodable ML features.

### API endpoint

```python
@router.get("/sybil-clusters")
async def list_sybil_clusters(
    min_size: int = Query(3, ge=2),
    limit: int = Query(100, le=500),
) -> list[SybilClusterResponse]:
    """Return Sybil clusters ordered by cluster_size DESC."""
    ...
```

### Configuration

```
ACCOUNT_FUNDING_MAX_DEPTH=5
ACCOUNT_FUNDING_CACHE_TTL_DAYS=7
SYBIL_MIN_CLUSTER_SIZE=3
```

## Security Considerations

- **Horizon rate limits on ancestry resolution**: each wallet in a batch triggers up to 5 Horizon requests. At 1000 wallets/batch, that is up to 5000 requests. Use `asyncio.Semaphore(20)` to limit concurrent requests; respect `Retry-After` headers
- **Cycle detection**: the funding graph should be a DAG (tree). In practice, account re-funding via secondary mechanisms could create cycles. The cycle guard in `FundingChainResolver.resolve` must always terminate to prevent infinite loops
- **Wallet address validation**: all wallet strings from Horizon responses must be validated against Stellar public key format (`G[A-Z2-7]{55}`) before inserting into the graph or cache
- **Cache staleness**: the funding chain is immutable (you can't change who funded you), but the cache TTL still applies because early fetches may have failed partially. TTL of 7 days is conservative; do not lower it below 24 hours
- **Cluster ID collisions**: SHA-256[:8] is used for display; the canonical key in SQLite must be the full sorted wallet list string, not the hash, to prevent collision false-merges

## Testing Requirements

- [ ] `tests/test_account_loader.py` — unit tests for all new classes
- [ ] Test: `FundingChainResolver.resolve` fetches up to max_depth hops and stops at genesis (no funder)
- [ ] Test: cycle guard — if chain contains wallet A → B → A, resolve stops and logs WARNING
- [ ] Test: cache hit — second resolve call for same wallet does not make a Horizon request
- [ ] Test: cache expiry — cached entry older than TTL_DAYS triggers re-fetch
- [ ] Test: `SybilClusterDetector.ingest_chains` merges three wallets funded by the same root into one cluster
- [ ] Test: cluster with size < `min_cluster_size` is not emitted
- [ ] Test: `get_features()` returns `sybil_cluster_size=0.0, sybil_in_cluster=0.0` for a wallet with no cluster
- [ ] Integration test: `GET /sybil-clusters?min_size=3` returns correct schema

## Documentation Requirements

- [ ] Docstrings on `FundingChainResolver`, `SybilClusterDetector`, `UnionFind`, `SybilCluster`, `FundingRecord`
- [ ] Add `docs/sybil_detection.md` explaining the Stellar funding-chain mechanic, the union-find clustering approach, threshold guidance, and known limitations (wallets funded by exchanges will appear in very large clusters — document the exchange address allowlist approach)
- [ ] Update `README.md` feature table with two new Sybil features
- [ ] Document the `funding_chain_cache` table in `docs/database_schema.md`
- [ ] Update `.env.example` with three new configuration variables

## Definition of Done

- [ ] `FundingChainResolver`, `FundingGraph`, `SybilClusterDetector`, `UnionFind` implemented
- [ ] Two new ML features in `FEATURE_NAMES` and computed in `feature_engineering.py`
- [ ] `GET /sybil-clusters` endpoint live
- [ ] SQLite cache table created via migration
- [ ] All tests pass including cycle guard and cache expiry tests
- [ ] No regressions in existing `test_account_loader.py` tests
- [ ] `docs/sybil_detection.md` authored

## For Contributors

**Ideal contributor profile**: You have experience building graph-based identity clustering systems (Sybil detection, account linking, entity resolution). You understand union-find and connected-component algorithms at production scale. Familiarity with Stellar's account creation model and Horizon's operations API is a strong plus. Experience with async Python and HTTP caching strategies (cache-aside pattern) is expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "Sybil/identity clustering", "graph algorithms at scale", "Stellar/blockchain account analysis"
2. **Relevant experience** — specific systems where you built account-linkage or Sybil detection; any publications on Sybil resistance
3. **Approach / initial thoughts** — how you would handle the exchange-address problem (large exchanges fund millions of wallets, creating spurious mega-clusters); your thoughts on depth-5 vs alternative depth limits
4. **Estimated time** — breakdown by component (resolver, graph, cluster detector, cache, API, tests, docs)
