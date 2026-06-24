---
title: "Parallelise Historical Ledger Backfill with Concurrent Range Fetching"
labels: ["difficulty: advanced", "area: ingestion", "type: enhancement"]
assignees: []
---

## Summary

Extend `ingestion/historical_loader.py` to use a configurable async worker pool (default 8 workers) to fetch non-overlapping ledger ranges concurrently. Implement deduplication, range-overlap detection, and a progress checkpoint so interrupted backfills resume rather than restart from the beginning. This reduces full-history backfill time from hours to minutes on a standard internet connection.

## Background & Context

The Stellar Horizon API paginates trade records chronologically. `historical_loader.py` currently fetches one page at a time sequentially: page N+1 is not fetched until page N is fully processed. On a 3-month backfill at 200 trades/page, this means ~50,000 sequential HTTP requests. At 300ms average latency per request, that is 4+ hours of wall-clock time for a single backfill — a major operational bottleneck.

Horizon supports fetching records with arbitrary `cursor` values, meaning non-overlapping ledger ranges can be fetched independently. The natural decomposition is to split the target date range into N equal sub-ranges, each fetched by a separate async worker, with results merged into a global deduplication set.

Key engineering challenges:
1. **Cursor semantics**: Horizon cursors encode a `paging_token` (ledger sequence + operation index). Converting a target timestamp to the nearest cursor requires a binary search via `GET /ledgers?order=asc&limit=1&cursor=...`. Workers need a utility to resolve timestamps to cursors.
2. **Range overlap at boundaries**: sub-range boundaries may produce a 1–2 trade overlap if the split ledger has multiple trades. Deduplication must be on `operation_id`, not on position.
3. **Checkpoint/resume**: progress is stored per sub-range. A sub-range is marked `COMPLETE` only when its last page returns an empty result set. On restart, complete sub-ranges are skipped.
4. **Backpressure**: if the SQLite writer can't keep up, the async worker pool must slow down (bounded queue with `asyncio.Queue(maxsize=1000)`).

## Objectives

- [ ] Implement `LedgerRangeSplitter` that converts a `(start_ts, end_ts)` pair into N non-overlapping `LedgerRange` objects
- [ ] Implement `TimestampToCursorResolver` that binary-searches Horizon to find the paging_token for a given datetime
- [ ] Implement `BackfillWorker` as an async coroutine fetching one `LedgerRange` and putting `Trade` records into a shared `asyncio.Queue`
- [ ] Implement `BackfillCoordinator` launching N workers, draining the queue into SQLite, and tracking per-range progress
- [ ] Implement SQLite-backed `CheckpointStore` persisting `(range_id, last_cursor, status)` per backfill run
- [ ] Implement `OperationIdDeduplicator` backed by a SQLite `UNIQUE` constraint (not an in-memory set — must survive restarts)
- [ ] Add `--workers`, `--start`, `--end`, and `--resume` flags to `cli.py` for the backfill command
- [ ] Write tests with an `aiohttp` mock that verifies correct range decomposition, deduplication, and checkpoint resume behaviour

## Technical Requirements

### Data structures

```python
# ingestion/historical_loader.py

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

class RangeStatus(str, Enum):
    PENDING   = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE  = "complete"
    FAILED    = "failed"

@dataclass
class LedgerRange:
    range_id: str          # SHA-256[:8] of (start_ts.isoformat() + end_ts.isoformat())
    start_ts: datetime
    end_ts: datetime
    start_cursor: Optional[str] = None
    end_cursor: Optional[str] = None
    status: RangeStatus = RangeStatus.PENDING
    last_cursor: Optional[str] = None   # resume point
    trades_fetched: int = 0
```

### Timestamp-to-cursor resolver

```python
class TimestampToCursorResolver:
    def __init__(self, http_client, horizon_url: str): ...

    async def resolve(self, ts: datetime) -> str:
        """
        Binary-search Horizon ledgers to find the paging_token
        of the first trade at or after `ts`.
        Max 20 iterations; raises TimeoutError if not converged.
        """
        ...
```

### Backfill worker

```python
async def backfill_worker(
    worker_id: int,
    ledger_range: LedgerRange,
    http_client,
    queue: asyncio.Queue,
    checkpoint_store: "CheckpointStore",
    page_limit: int = 200,
) -> None:
    """
    Fetch pages from ledger_range.start_cursor to ledger_range.end_cursor.
    After each page, update checkpoint_store with last_cursor.
    Put Trade objects into queue.
    Mark range COMPLETE when response is empty.
    """
    cursor = ledger_range.last_cursor or ledger_range.start_cursor
    while True:
        trades = await _fetch_page(http_client, cursor, page_limit)
        if not trades:
            await checkpoint_store.mark_complete(ledger_range.range_id)
            break
        # Stop if we've passed the range end
        if trades[-1].timestamp > ledger_range.end_ts:
            trades = [t for t in trades if t.timestamp <= ledger_range.end_ts]
            for t in trades:
                await queue.put(t)
            await checkpoint_store.mark_complete(ledger_range.range_id)
            break
        for t in trades:
            await queue.put(t)
        cursor = trades[-1].paging_token
        await checkpoint_store.update_cursor(ledger_range.range_id, cursor)
```

### Backfill coordinator

```python
class BackfillCoordinator:
    def __init__(
        self,
        horizon_url: str,
        db_path: str,
        n_workers: int = 8,
        queue_maxsize: int = 1000,
        page_limit: int = 200,
    ): ...

    async def run(
        self,
        start_ts: datetime,
        end_ts: datetime,
        resume: bool = True,
    ) -> BackfillSummary:
        """
        1. Split range into n_workers sub-ranges.
        2. Resolve cursors for each sub-range boundary.
        3. Load checkpoint (if resume=True) and skip COMPLETE ranges.
        4. Launch workers; drain queue into SQLite via _writer_task.
        5. Return summary (total trades, duplicates skipped, elapsed time).
        """
        ...
```

### Checkpoint store schema

```sql
CREATE TABLE IF NOT EXISTS backfill_checkpoints (
    run_id      TEXT NOT NULL,
    range_id    TEXT NOT NULL,
    start_ts    TIMESTAMP NOT NULL,
    end_ts      TIMESTAMP NOT NULL,
    start_cursor TEXT,
    end_cursor   TEXT,
    last_cursor  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    trades_fetched INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, range_id)
);
```

### CLI additions

```
python cli.py backfill \
    --start 2024-01-01 \
    --end 2024-03-31 \
    --workers 8 \
    --resume           # skip completed ranges from last run
```

### Configuration

```
BACKFILL_WORKERS=8
BACKFILL_PAGE_LIMIT=200
BACKFILL_QUEUE_MAXSIZE=1000
BACKFILL_MAX_CURSOR_RESOLVE_ITERATIONS=20
```

## Security Considerations

- **Rate limiting**: Horizon enforces rate limits per IP. Workers must share a single `asyncio.Semaphore(max_concurrent=BACKFILL_WORKERS)` and respect `Retry-After` headers. If a 429 is received, all workers must back off for the specified duration before resuming
- **Cursor injection**: cursors are opaque Horizon-issued strings used in query parameters. They must be URL-encoded before inclusion in requests. Validate that cursor values match `[0-9]+` (numeric paging tokens) before use
- **Checkpoint integrity**: the checkpoint DB is SQLite. Use WAL mode (`PRAGMA journal_mode=WAL`) to prevent corruption if the process is killed mid-write
- **Trade deduplication via DB constraint**: use `INSERT OR IGNORE` against a `UNIQUE (operation_id)` constraint rather than an in-memory set, so deduplication survives process restarts
- **Memory bounds**: the `asyncio.Queue(maxsize=1000)` provides natural backpressure. Do not use `queue.put_nowait()` in workers; always await `queue.put()` so workers block when the writer is slow

## Testing Requirements

- [ ] `tests/test_historical_loader.py` — unit and integration tests using `aiohttp` mock
- [ ] Test: `LedgerRangeSplitter` produces N non-overlapping ranges that cover the full `[start_ts, end_ts]` window
- [ ] Test: `TimestampToCursorResolver` converges within 20 iterations on a mock Horizon (verify number of HTTP calls)
- [ ] Test: `BackfillWorker` stops at `end_cursor` and does not emit trades past `end_ts`
- [ ] Test: deduplication — same `operation_id` ingested twice results in exactly one record in SQLite
- [ ] Test: checkpoint resume — mark one range as `COMPLETE` in the checkpoint store; verify worker skips it and total trades match single-range fetch
- [ ] Test: backpressure — when queue is full, worker blocks and does not overflow
- [ ] Test: 429 response triggers exponential backoff and retry
- [ ] Integration test: `cli.py backfill --start 2024-01-01 --end 2024-01-02` (mock Horizon, 1000 trades) completes and writes correct record count

## Documentation Requirements

- [ ] Docstrings on `LedgerRange`, `BackfillCoordinator`, `BackfillWorker`, `CheckpointStore`
- [ ] Add `docs/backfill_guide.md` with operational instructions: how to start, resume, and verify a backfill; how to tune `--workers` for different network conditions; known limitations (Horizon rate limits, cursor precision)
- [ ] Update `README.md` CLI reference to include `backfill` command
- [ ] Document the `backfill_checkpoints` table in `docs/database_schema.md`
- [ ] Update `.env.example` with the four new configuration variables

## Definition of Done

- [ ] `BackfillCoordinator`, `BackfillWorker`, `TimestampToCursorResolver`, and `CheckpointStore` fully implemented
- [ ] `cli.py backfill` command functional with all four flags
- [ ] Deduplication via DB `UNIQUE` constraint (not in-memory set)
- [ ] Checkpoint resume verified by test
- [ ] 429 backoff verified by test
- [ ] All tests pass
- [ ] `docs/backfill_guide.md` authored
- [ ] `.env.example` updated

## For Contributors

**Ideal contributor profile**: You have production experience building async Python data ingestion pipelines with rate limiting, backpressure, and checkpoint/resume semantics. You are comfortable with `asyncio`, `aiohttp`, and `aiosqlite`. Experience with the Stellar Horizon API pagination model (cursor-based, not offset-based) is a significant advantage. Familiarity with SQLite WAL mode and concurrency-safe writes in Python is expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "async Python data pipelines", "Horizon API integration", "high-throughput ingestion systems"
2. **Relevant experience** — pipeline systems you have built with checkpoint/resume and concurrent fetching; specific Horizon or blockchain ingestion work
3. **Approach / initial thoughts** — how you would handle the cursor-boundary overlap problem; your thoughts on the binary-search resolver vs alternative timestamp-to-cursor approaches
4. **Estimated time** — broken down by component (splitter, resolver, worker, coordinator, checkpoint, CLI, tests, docs)
