---
title: "Build a Parallel Historical Trade Loader with Configurable Concurrency"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
The current `historical_loader.py` fetches Stellar ledger history sequentially, making full-history backfills prohibitively slow — the Stellar ledger has millions of ledgers and the Horizon API rate-limit means sequential fetching can take days for a meaningful time window. A parallel loader that partitions the ledger range into independent chunks and fetches them concurrently will reduce backfill time by an order of magnitude while providing restart-safe progress tracking and deduplication.

## Background & Context
`ingestion/historical_loader.py` is responsible for bulk historical trade ingestion from the Horizon REST API (`GET /trades?start_time=...&end_time=...` or cursor-based pagination). This feeds both the ML training pipeline (`detection/model_training.py` via `detection/dataset.py`) and gap-filling after streamer restarts.

The Stellar mainnet ledger closes every ~5 seconds and has been running since 2015, yielding approximately 200+ million ledger closes. Even limiting to recent trade data, a single asset pair can have hundreds of thousands of trades. Sequential page-by-page fetching at Horizon's pagination limit of 200 records/page creates a long tail of fetching time.

The parallelisation strategy is to split the requested time range `[start, end]` into N sub-ranges of equal duration, each fetched by an independent async worker. Each worker maintains its own cursor within its sub-range and writes completed records to a shared SQLite store (using per-worker write batching to minimize lock contention). A progress file tracks which sub-ranges are complete, allowing interrupted backfills to resume without re-fetching completed chunks.

The `http_client.py` `RetryingHorizonClient` must be reused for all sub-range workers — do not introduce a new HTTP client. Worker concurrency must be bounded by a semaphore to respect Horizon rate limits.

## Objectives
- [ ] Implement a `ParallelHistoricalLoader` class in `ingestion/historical_loader.py` that partitions a `[start_time, end_time]` range into configurable N chunks and fetches each chunk concurrently using `asyncio` + a semaphore-bounded worker pool.
- [ ] Implement a `ProgressTracker` that persists chunk completion state to a JSON file, enabling interrupted backfills to skip already-completed chunks on restart.
- [ ] Implement idempotent upsert logic (`INSERT OR IGNORE`) in the SQLite write path so duplicate records from overlapping chunk boundaries do not cause constraint violations.
- [ ] Add a `cli.py historical-load` sub-command with `--start`, `--end`, `--concurrency`, `--chunk-hours`, and `--resume` flags.

## Technical Requirements

**Chunk partitioning:**
```python
def partition_range(
    start: datetime,
    end: datetime,
    chunk_hours: float = 6.0,
) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into non-overlapping chunks of chunk_hours duration."""
```
The last chunk may be shorter than `chunk_hours`. Chunks must not overlap (use half-open intervals `[chunk_start, chunk_end)`).

**`ParallelHistoricalLoader` interface:**
```python
class ParallelHistoricalLoader:
    def __init__(
        self,
        client: RetryingHorizonClient,
        storage: RiskScoreStore,
        concurrency: int = 4,
        chunk_hours: float = 6.0,
        progress_path: Path = Path("./data/historical_progress.json"),
    ): ...

    async def load(
        self,
        start: datetime,
        end: datetime,
        asset_pair: str | None = None,
        resume: bool = True,
    ) -> LoadResult: ...

    async def _fetch_chunk(
        self,
        chunk: tuple[datetime, datetime],
        asset_pair: str | None,
        sem: asyncio.Semaphore,
    ) -> ChunkResult: ...
```

**`ProgressTracker`:**
```python
@dataclass
class ChunkProgress:
    chunk_id: str          # SHA256[:8] of (start_iso, end_iso, asset_pair)
    start: datetime
    end: datetime
    status: Literal["pending", "in_progress", "complete", "failed"]
    records_fetched: int = 0
    completed_at: datetime | None = None
    error: str | None = None

class ProgressTracker:
    def load(self) -> dict[str, ChunkProgress]: ...
    def mark_in_progress(self, chunk_id: str) -> None: ...
    def mark_complete(self, chunk_id: str, records_fetched: int) -> None: ...
    def mark_failed(self, chunk_id: str, error: str) -> None: ...
    def save(self) -> None: ...  # atomic write
```

**Concurrency control:**
```python
sem = asyncio.Semaphore(concurrency)
tasks = [asyncio.create_task(self._fetch_chunk(chunk, asset_pair, sem)) for chunk in chunks]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

Workers that encounter a non-retriable error (e.g., 400 Bad Request) should mark their chunk as `failed` and continue — do not cancel other workers.

**Deduplication** — the `Trade` primary key is `(paging_token,)`. All writes must use:
```sql
INSERT OR IGNORE INTO trades (...) VALUES (...)
```

**`LoadResult` dataclass:**
```python
@dataclass
class LoadResult:
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    skipped_chunks: int       # already completed on resume
    total_records: int
    duration_seconds: float
    records_per_second: float
```

**Performance target**: with `concurrency=8` and `chunk_hours=6`, loading 30 days of XLM/USDC trade history should complete in under 10 minutes on a standard broadband connection (Horizon rate limit permitting).

**Configuration** (add to `config/settings.py`):
- `HISTORICAL_LOADER_CONCURRENCY`: default `4`
- `HISTORICAL_CHUNK_HOURS`: default `6.0`
- `HISTORICAL_PROGRESS_PATH`: default `./data/historical_progress.json`

## Security Considerations
- The `start` and `end` datetime parameters must be validated to ensure `start < end` and that neither exceeds a configurable maximum lookback window (default: 365 days) to prevent runaway backfills triggered by misconfigured automation.
- The progress file path must be resolved and validated to stay within the data directory — same path-traversal concern as ISSUE-001.
- All Horizon API responses must go through the existing `data_models.py` validation (Pydantic) before being written to SQLite; raw JSON must never be written directly to the database.
- Log the `LoadResult` summary at `INFO` level but do not log individual trade records (they may contain wallet addresses that operators prefer not to log at `INFO` level).

## Testing Requirements
- Unit tests covering `partition_range`: correct chunk count, correct boundaries, last short chunk, single-chunk range, zero-duration range (should raise)
- Unit tests covering `ProgressTracker`: load from file, mark transitions, atomic save, corrupt file fallback
- Unit tests covering `_fetch_chunk`: mock HTTP client returning paginated responses; assert all pages are fetched and records written
- Integration tests: mock Horizon REST endpoint serving 3 pages of trades per chunk; run `ParallelHistoricalLoader.load()` with `concurrency=3`; assert all records written, `LoadResult` totals correct
- Integration tests: simulate one failed chunk (mock HTTP 500); assert other chunks complete successfully and `LoadResult.failed_chunks == 1`
- Integration tests: resume scenario — pre-populate progress file with 2 completed chunks; assert those chunks are skipped (zero HTTP calls for them)
- Edge cases: `concurrency` > number of chunks (no deadlock), `end` before `start`, asset pair with no trades in range (empty response), SQLite write failure mid-chunk
- Performance benchmark: 1,000 synthetic trade records across 10 chunks processed in < 5 seconds in unit test with mocked HTTP

## Documentation Requirements
- Update `README.md` CLI Reference to document `python cli.py historical-load` with all flags
- Add docstrings to `ParallelHistoricalLoader`, `partition_range`, `ProgressTracker`, and `_fetch_chunk`
- Update `docs/ingestion.md` with a section on parallel historical loading, resume semantics, and tuning `concurrency` vs Horizon rate limits
- Add a comment in `config/settings.py` explaining that `HISTORICAL_LOADER_CONCURRENCY` should not exceed Horizon's per-IP rate limit divided by average request duration

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Python `asyncio`, `asyncio.Semaphore`, concurrent HTTP clients, SQLite bulk writes, Stellar Horizon pagination API
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python async engineer with experience building concurrent data ingestion pipelines. Solid understanding of Horizon's cursor-based pagination model, SQLite upsert semantics, and progress-tracking patterns for resumable jobs. Experience with `aiohttp` or `httpx` async clients is expected.
