---
title: "Build Path Payment Multi-Hop Wash-Trade Detector"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/path_payment_engine.py` to detect circular value flows disguised as path payments across 3–7 hops on the SDEX. Stellar path payments are a powerful feature — they allow assets to flow through intermediate pools along the cheapest route — but they also create a natural cover for wash trading: an attacker controls wallets at the origin and terminus of a multi-hop path and recovers the same asset value they started with, generating fabricated volume across every intermediate hop.

## Background & Context

A Stellar `path_payment_strict_send` or `path_payment_strict_receive` operation routes value through up to 6 intermediate assets. Each hop generates a trade record in the Horizon `/trades` endpoint, making a single round-trip path payment appear as 3–7 distinct trades — artificially multiplying reported volume by the path length.

LedgerLens currently detects pairwise wash trades (direct A→B→A cycles) via the SCC engine in `detection/graph_engine.py`. However, `graph_engine.py` operates on the trade graph where nodes are wallets. Path-payment wash trades are different: the same wallet sends and receives, but the path graph is an asset-hop graph, not a wallet graph. Existing detection misses these entirely.

`ingestion/path_payment_loader.py` already ingests raw path payment operation records from Horizon. This issue builds the cycle-detection layer on top of that data.

Detection strategy:
1. Build a directed **asset-hop graph** where nodes are `(wallet, asset)` pairs and edges are individual hop transfers
2. Run cycle detection (DFS with backtracking) to find paths where the origin `(wallet, asset)` is also the terminus within a configurable time window
3. Score each cycle by: path length, asset value recovered at terminus vs sent at origin (recovery ratio), timing between first and last hop, and counterparty overlap across hops

A recovery ratio ≥ 0.95 within a 1-hour window is treated as a confirmed round-trip. Shorter windows and higher recovery ratios increase confidence.

## Objectives

- [ ] Implement `PathPaymentGraph` class that builds the `(wallet, asset)` directed hop graph from path payment records
- [ ] Implement `PathCycleDetector` using iterative DFS with a depth limit of 7 hops
- [ ] Compute `recovery_ratio`, `path_length`, `cycle_duration_seconds`, and `counterparty_overlap` for each detected cycle
- [ ] Emit `PathPaymentCycle` records with a composite `cycle_score` (0–1)
- [ ] Add `path_cycle_count` and `path_cycle_recovery_ratio` as new ML features in `feature_engineering.py`
- [ ] Expose `GET /path-cycles` endpoint in `api/main.py`
- [ ] Integrate with `detection/path_cycle_detector.py` (existing stub) — this issue is the full implementation
- [ ] Write tests covering 3-hop, 5-hop, and 7-hop cycles, partial recovery, and timeout edge cases

## Technical Requirements

### Data structures

```python
# detection/path_payment_engine.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class HopEdge:
    src_wallet: str
    src_asset: str
    dst_wallet: str
    dst_asset: str
    amount: float
    ledger_timestamp: datetime
    operation_id: str

@dataclass
class PathPaymentCycle:
    origin_wallet: str
    origin_asset: str
    hops: list[HopEdge]
    recovery_ratio: float        # amount_recovered / amount_sent (ideal: 1.0)
    cycle_duration_seconds: float
    counterparty_overlap: float  # fraction of hops sharing a counterparty wallet
    cycle_score: float           # 0–1 composite suspicion score
    detected_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def path_length(self) -> int:
        return len(self.hops)
```

### Core engine interface

```python
class PathPaymentGraph:
    def __init__(self, cycle_window_seconds: float = 3600.0, max_depth: int = 7): ...

    def add_hop(self, edge: HopEdge) -> None:
        """Add a single hop edge; prune edges older than cycle_window_seconds."""
        ...

    def find_cycles(self, origin_wallet: str) -> list[PathPaymentCycle]:
        """
        DFS from all (origin_wallet, asset) nodes. Return cycles where
        the walk returns to origin_wallet with recovery_ratio >= 0.5.
        Depth-limited to max_depth hops.
        """
        ...


class PathCycleDetector:
    def __init__(
        self,
        cycle_window_seconds: float = 3600.0,
        max_depth: int = 7,
        min_recovery_ratio: float = 0.95,
        min_cycle_score: float = 0.6,
    ): ...

    def ingest(self, hop_records: list[dict]) -> list[PathPaymentCycle]:
        """
        Process raw Horizon path_payment operation records.
        Returns newly detected cycles above min_cycle_score.
        """
        ...

    def get_features(self, wallet: str) -> dict[str, float]:
        """Return {'path_cycle_count': int, 'path_cycle_recovery_ratio': float}."""
        ...
```

### Cycle scoring formula

```python
def _score_cycle(cycle: PathPaymentCycle) -> float:
    """
    Composite score weighted as:
      40% recovery_ratio
      30% timing_score  (shorter duration = higher score, sigmoid-scaled)
      20% 1 - (1 / path_length)  (longer path = harder to detect = higher risk)
      10% counterparty_overlap
    """
    timing_score = 1 / (1 + cycle.cycle_duration_seconds / 600)  # half-score at 10 min
    length_score = 1.0 - (1.0 / max(cycle.path_length, 1))
    return (
        0.40 * cycle.recovery_ratio
        + 0.30 * timing_score
        + 0.20 * length_score
        + 0.10 * cycle.counterparty_overlap
    )
```

### Feature integration

```python
# detection/feature_engineering.py additions
FEATURE_NAMES = [
    # ... existing features ...
    "path_cycle_count",            # integer cast to float
    "path_cycle_recovery_ratio",   # max recovery ratio across all cycles for this wallet
]
```

### API endpoint

```python
# api/main.py
@router.get("/path-cycles")
async def list_path_cycles(
    min_score: float = Query(0.6, ge=0.0, le=1.0),
    wallet: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
) -> list[PathPaymentCycleResponse]:
    """Return detected path-payment cycles, optionally filtered by wallet."""
    ...
```

### Configuration

```
PATH_CYCLE_WINDOW_SECONDS=3600
PATH_CYCLE_MAX_DEPTH=7
PATH_CYCLE_MIN_RECOVERY_RATIO=0.95
PATH_CYCLE_MIN_SCORE=0.6
```

### Storage

Store detected cycles in a new SQLite table `path_payment_cycles`:

```sql
CREATE TABLE IF NOT EXISTS path_payment_cycles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_wallet TEXT NOT NULL,
    origin_asset  TEXT NOT NULL,
    path_length   INTEGER NOT NULL,
    recovery_ratio REAL NOT NULL,
    cycle_duration_seconds REAL NOT NULL,
    counterparty_overlap REAL NOT NULL,
    cycle_score   REAL NOT NULL,
    hop_json      TEXT NOT NULL,   -- JSON-encoded list of HopEdge dicts
    detected_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ppc_wallet ON path_payment_cycles(origin_wallet);
CREATE INDEX IF NOT EXISTS idx_ppc_score  ON path_payment_cycles(cycle_score DESC);
```

## Security Considerations

- **Graph explosion**: a wallet involved in N path payments creates up to N! potential cycle candidates. Enforce `MAX_NODES_PER_WALLET = 500` and `MAX_EDGES_PER_WALLET = 2000`; beyond this, log a warning and skip DFS (record `truncated=True` in the cycle record)
- **Asset string injection**: asset codes from Horizon can be arbitrary strings up to 12 chars. Validate `asset_code` matches `[A-Za-z0-9]{1,12}` and `asset_issuer` matches Stellar public key format before storing or logging
- **Time-window manipulation**: attackers may spread round-trip hops just beyond the detection window. The `cycle_window_seconds` parameter must be operator-configurable but bounded: reject values outside `[300, 86400]` at startup
- **Replay**: the same Horizon operation can be ingested twice during backfill and streaming overlap. Deduplicate on `operation_id` before adding to the graph
- **Memory bounds**: the hop graph is in-memory. At 50,000 hops/hour, enforce a global `MAX_GRAPH_EDGES = 500_000` with oldest-edge eviction

## Testing Requirements

- [ ] `tests/test_path_payment_engine.py` — unit tests for `PathPaymentGraph` and `PathCycleDetector`
- [ ] Test: 3-hop cycle with recovery_ratio=1.0 and duration=60s → `cycle_score > 0.8`
- [ ] Test: 7-hop cycle with recovery_ratio=0.96 and duration=3500s → `cycle_score > 0.6`
- [ ] Test: partial recovery (ratio=0.4) → cycle not emitted (below `min_recovery_ratio`)
- [ ] Test: hops arriving out of order (by ledger_timestamp) still form correct cycles
- [ ] Test: duplicate operation_id ingestion does not create duplicate cycle records
- [ ] Test: `MAX_EDGES_PER_WALLET` guard triggers correctly and logs a warning
- [ ] Test: `get_features` returns zeros for wallet with no path payment history
- [ ] Integration test: `GET /path-cycles?wallet=GABC&min_score=0.6` filters correctly

## Documentation Requirements

- [ ] Docstrings on `PathPaymentGraph`, `PathCycleDetector`, `PathPaymentCycle`, and `HopEdge`
- [ ] Add `docs/path_payment_detection.md` explaining the attack model, hop-graph construction, cycle-scoring formula, and configuration guidance
- [ ] Update `README.md` feature table with two new path-cycle features
- [ ] Document the `path_payment_cycles` SQLite schema in `docs/database_schema.md`
- [ ] Update `.env.example` with the four new configuration keys

## Definition of Done

- [ ] `detection/path_payment_engine.py` fully implements `PathPaymentGraph` and `PathCycleDetector`
- [ ] `detection/path_cycle_detector.py` delegates to the new engine (backward-compatible stub preserved)
- [ ] Two new features present in `FEATURE_NAMES` and computed in `feature_engineering.py`
- [ ] `GET /path-cycles` endpoint live in `api/main.py`
- [ ] SQLite table `path_payment_cycles` created via `cli.py db-migrate`
- [ ] All unit and integration tests pass
- [ ] No regressions in existing `test_graph_engine.py` tests
- [ ] `docs/path_payment_detection.md` authored
- [ ] `.env.example` updated

## For Contributors

**Ideal contributor profile**: You are comfortable with graph algorithms (DFS, cycle detection, backtracking) and understand the performance tradeoffs of in-memory graph representations in Python. You have worked with Stellar Horizon operation data or similar blockchain event streams. Familiarity with Stellar path payment mechanics and the SDEX routing algorithm is a significant advantage. Experience with SQLite schema design and async Python is helpful for the storage and API layers.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "graph algorithms", "Stellar/blockchain data pipelines", "DeFi wash-trade detection"
2. **Relevant experience** — specific systems where you implemented cycle detection or multi-hop graph traversal at scale
3. **Approach / initial thoughts** — how you would handle the time-windowed graph with out-of-order hop arrivals; your thoughts on the depth-7 DFS vs alternative approaches (e.g., matrix methods)
4. **Estimated time** — broken down by component (graph core, scoring, storage, API, tests, docs)
