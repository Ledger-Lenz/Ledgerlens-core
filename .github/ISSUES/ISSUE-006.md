---
title: "Build an EVM Bridge Event Deduplication Layer for Cross-Chain Ingestion"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
The cross-chain detection feature (see `docs/cross_chain_detection.md`) ingests bridge events from EVM chains (Ethereum, Base, Polygon) via `bridge_loader.py` and `evm_loader.py`. These loaders can receive the same bridge event multiple times due to RPC provider retries, block reorganisations, and restart replay. Without a deduplication layer, duplicate events propagate into the feature engineering pipeline, artificially inflating cross-chain volume metrics and producing false wash-trading alerts. A content-hash-based dedup layer with idempotent SQLite upserts and replay-attack protection will make the cross-chain ingestion path idempotent and correct.

## Background & Context
The README describes six dedicated cross-chain features computed by `detection/feature_engineering.py` from bridge events. These features feed directly into the ensemble ML classifiers (Random Forest, XGBoost, LightGBM) and contribute to the composite `LedgerLens Risk Score (0–100)`.

`ingestion/bridge_loader.py` polls the Allbridge relayer contract logs on EVM chains for bridge transfer events. `ingestion/evm_loader.py` fetches raw EVM transaction receipts and decodes event logs. Both can receive duplicates because:
1. **RPC retries**: when a `getLogs` call times out and is retried, the provider may return overlapping block ranges
2. **Block reorganisations**: a bridge event included in a block that is later reorged may be re-emitted in the canonical chain's replacement block, but with a different block hash and potentially different log index
3. **Restart replay**: when the EVM loader restarts, it typically re-scans a small lookback window (e.g., the last 100 blocks) to catch events that arrived during the downtime — these overlap with previously processed events

The current loaders write events to SQLite without any deduplication check. A content-hash approach computes a stable identifier from the event's immutable fields (transaction hash + log index + chain ID), which is independent of block reorganisation metadata and can be used as a unique key.

## Objectives
- [ ] Implement a `BridgeEventDeduplicator` class in a new `ingestion/dedup.py` module that computes a SHA-256 content hash from the immutable fields of each bridge event and checks/stores it in a SQLite dedup table.
- [ ] Modify `bridge_loader.py` and `evm_loader.py` to call the deduplicator before writing events to the main tables, using `INSERT OR IGNORE` semantics so duplicate inserts are silently skipped.
- [ ] Add a `replay_protection_window_blocks` configuration that rejects events with block numbers older than the current chain head minus a configurable window (default: 1000 blocks) to prevent targeted replay attacks with stale events.
- [ ] Expose a `DeduplicationStats` dataclass on the deduplicator with `seen_total`, `duplicate_total`, `replay_rejected_total` counters, queryable from the metrics layer.

## Technical Requirements

**Content hash computation:**
```python
import hashlib, json

def compute_event_hash(
    chain_id: int,
    tx_hash: str,
    log_index: int,
) -> str:
    """
    Stable, reorg-resistant event identifier.
    Does NOT include block_hash or block_number (reorg-sensitive).
    Returns hex-encoded SHA-256[:32] (64 hex chars).
    """
    payload = json.dumps(
        {"chain_id": chain_id, "tx_hash": tx_hash.lower(), "log_index": log_index},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
```

**SQLite dedup table schema:**
```sql
CREATE TABLE IF NOT EXISTS bridge_event_dedup (
    event_hash TEXT PRIMARY KEY,
    chain_id INTEGER NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dedup_chain_block ON bridge_event_dedup (chain_id, block_number);
```

**`BridgeEventDeduplicator` interface:**
```python
class BridgeEventDeduplicator:
    def __init__(self, db_conn: sqlite3.Connection, replay_window_blocks: int = 1000): ...

    def is_duplicate(
        self,
        chain_id: int,
        tx_hash: str,
        log_index: int,
        block_number: int,
        current_chain_head: int,
    ) -> DedupResult: ...

    def mark_seen(
        self,
        chain_id: int,
        tx_hash: str,
        log_index: int,
        block_number: int,
    ) -> None: ...

    def stats(self) -> DeduplicationStats: ...
```

**`DedupResult` enum:**
```python
class DedupResult(Enum):
    NEW = "new"                        # not seen before, within replay window
    DUPLICATE = "duplicate"            # hash already in dedup table
    REPLAY_REJECTED = "replay_rejected"  # block_number too old
```

**`DeduplicationStats` dataclass:**
```python
@dataclass
class DeduplicationStats:
    seen_total: int
    duplicate_total: int
    replay_rejected_total: int
    duplicate_rate: float  # duplicate_total / seen_total if seen_total > 0 else 0.0
```

**Integration in `bridge_loader.py`:**
```python
for event in raw_events:
    result = self.deduplicator.is_duplicate(
        chain_id=event.chain_id,
        tx_hash=event.tx_hash,
        log_index=event.log_index,
        block_number=event.block_number,
        current_chain_head=current_head,
    )
    if result == DedupResult.NEW:
        self.deduplicator.mark_seen(...)
        self._write_event(event)
    elif result == DedupResult.DUPLICATE:
        logger.debug("Skipping duplicate bridge event %s:%d", event.tx_hash, event.log_index)
    elif result == DedupResult.REPLAY_REJECTED:
        logger.warning("Rejecting replayed bridge event at block %d (head=%d, window=%d)",
                       event.block_number, current_head, self.deduplicator.replay_window_blocks)
```

**Dedup table pruning**: add a `prune_old_entries(keep_blocks: int = 10_000)` method that deletes dedup entries for blocks older than `current_head - keep_blocks`. Call this daily (or every N calls) to prevent unbounded table growth. Log the number of pruned rows at `DEBUG` level.

**Reorg handling**: when a reorg is detected (block at height H has a different hash than recorded), mark all dedup entries for `block_number >= H` as `reorg_invalidated` by deleting them and reprocessing the events from that height. Add a `handle_reorg(chain_id: int, reorg_from_block: int)` method.

**Configuration** (add to `config/settings.py`):
- `EVM_DEDUP_REPLAY_WINDOW_BLOCKS`: default `1000`
- `EVM_DEDUP_PRUNE_KEEP_BLOCKS`: default `10000`

## Security Considerations
- The content hash must be computed from normalised inputs: `tx_hash` lowercased, `log_index` as integer (not string) — to prevent hash-bypass attacks that exploit case differences or type coercions.
- The `replay_protection_window_blocks` limit prevents an adversary from submitting a bridge event that occurred thousands of blocks ago as if it were recent, which could artificially inflate cross-chain volume for a wallet under investigation.
- Dedup table writes must be wrapped in transactions to prevent partial writes on process kill.
- The `first_seen_at` column in `bridge_event_dedup` serves as an audit trail — do not delete rows as part of normal dedup logic, only as part of the explicit `prune_old_entries` or `handle_reorg` operations.
- All SQL queries must use parameterised statements — never f-string or format-string SQL to prevent SQL injection from attacker-controlled `tx_hash` values in RPC responses.

## Testing Requirements
- Unit tests covering `compute_event_hash`: same inputs always produce same hash, different `log_index` produces different hash, `tx_hash` case-insensitivity (uppercase and lowercase produce same hash)
- Unit tests covering `is_duplicate`: new event → `NEW`, same event again → `DUPLICATE`, old event beyond replay window → `REPLAY_REJECTED`
- Unit tests covering `prune_old_entries`: assert rows for old blocks are removed; assert rows for recent blocks are retained
- Unit tests covering `handle_reorg`: assert dedup entries for reorged blocks are deleted
- Integration tests: run `bridge_loader` mock against an EVM RPC mock that returns the same events twice (simulating retry); assert `DeduplicationStats.duplicate_total > 0` and only one record in the main bridge events table
- Integration tests: simulate a block reorg at block H; assert events from H onwards are reprocessed correctly
- Edge cases: `log_index=0` (valid), empty event batch, `current_chain_head=0`, events exactly at replay window boundary
- Performance benchmark: dedup check for 10,000 events against a 100,000-row dedup table should complete in < 500 ms

## Documentation Requirements
- Add module docstring to `ingestion/dedup.py` explaining the deduplication strategy, content hash design, and reorg handling
- Add docstrings to all public methods of `BridgeEventDeduplicator`
- Update `docs/cross_chain_detection.md` with a section on event deduplication, replay protection, and reorg handling
- Document the `bridge_event_dedup` table schema in any database schema documentation

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: EVM event log processing, block reorganisations, SQLite idempotent writes, content-addressable storage, Python `hashlib`
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Engineer with hands-on experience processing EVM event logs, including handling reorgs and duplicate delivery from JSON-RPC providers. Strong Python backend skills and familiarity with SQLite transaction patterns. Understanding of Allbridge or similar cross-chain bridge event schemas is a significant plus.
