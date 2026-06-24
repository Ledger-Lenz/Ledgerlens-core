---
title: "Implement Persistent Cursor Checkpointing for Horizon SSE Streamer"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
The current `horizon_streamer.py` SSE streamer loses its position in the Horizon event stream on process restart, causing it to either replay all events from the beginning or miss events entirely depending on the configured start cursor. Persistent cursor checkpointing will allow the streamer to survive restarts gracefully, resuming from exactly where it left off and eliminating both duplicate processing and data gaps.

## Background & Context
`ingestion/horizon_streamer.py` connects to the Stellar Horizon API's Server-Sent Events (SSE) endpoint (`/trades?cursor=now` or a paging token) to ingest real-time trade data. The Horizon SSE protocol provides a `paging_token` on every event record which acts as a stable, resumable cursor into the event stream.

Currently, the streamer holds this cursor in memory only. Any process restart — due to a crash, deployment, or OOM kill — results in the cursor being reset. This means:
- **Restart with `cursor=now`**: All events between the crash and restart are permanently lost, creating gaps in detection coverage.
- **Restart with `cursor=0`**: The full event history is replayed, triggering duplicate detections and flooding the risk-score store with stale data.

The LedgerLens pipeline runs continuously and feeds the ensemble ML classifiers in real time (see `detection/model_inference.py`). Gaps in ingestion directly degrade detection quality — a wash-trading ring that trades during a gap window will not be scored until the next historical backfill run.

Cursor checkpointing is a standard pattern in event-streaming systems (Kafka consumer offsets, Kinesis checkpoints). For Horizon SSE, the checkpoint is the last successfully processed `paging_token`. The checkpoint must be written atomically so a crash mid-write does not corrupt the stored cursor.

## Objectives
- [ ] Implement a `CursorCheckpoint` class in `ingestion/horizon_streamer.py` (or a new `ingestion/checkpoint.py` module) that reads and writes the last processed `paging_token` to a local file using atomic rename semantics (`write-to-tmp` → `os.replace`).
- [ ] Modify the `HorizonStreamer` startup sequence to read the checkpoint file on initialization; fall back to the configured `DEFAULT_CURSOR` (e.g. `"now"`) if no checkpoint exists or the file is corrupt.
- [ ] Checkpoint the cursor after every successfully processed batch (configurable flush interval: default every 100 events or every 10 seconds, whichever comes first).
- [ ] Add a `--reset-cursor` CLI flag to `cli.py stream` that deletes the checkpoint file before starting, enabling intentional full-replay.

## Technical Requirements

**Checkpoint file format** — store as a single-line JSON object for human readability and easy debugging:
```json
{"paging_token": "12345678901234-0", "recorded_at": "2026-06-24T09:00:00Z", "ledger_sequence": 50123456}
```

**`CursorCheckpoint` interface:**
```python
class CursorCheckpoint:
    def __init__(self, path: Path): ...
    def load(self) -> str | None:
        """Return stored paging_token or None if absent/corrupt."""
    def save(self, paging_token: str, ledger_sequence: int | None = None) -> None:
        """Atomically write checkpoint using write-tmp + os.replace."""
    def delete(self) -> None:
        """Remove checkpoint file (used by --reset-cursor)."""
```

**Atomic write implementation:**
```python
tmp_path = self.path.with_suffix(".tmp")
tmp_path.write_text(json.dumps(payload))
os.replace(tmp_path, self.path)  # atomic on POSIX; near-atomic on Windows
```

**Flush policy** — use a `FlushPolicy` dataclass:
```python
@dataclass
class FlushPolicy:
    max_events: int = 100       # flush after N events
    max_seconds: float = 10.0   # flush after N seconds regardless of event count
```

The streamer's inner event loop should track `events_since_flush` and `last_flush_time` and call `checkpoint.save()` when either threshold is exceeded.

**Startup recovery sequence:**
1. `checkpoint.load()` → if `None`, use `settings.HORIZON_DEFAULT_CURSOR`
2. Log at `INFO` level: `"Resuming from cursor {paging_token}"` or `"Starting fresh from cursor {cursor}"`
3. Validate that the cursor string matches Horizon's `paging_token` format (`r'^\d+-\d+$'` or `"now"`) — raise `ValueError` on malformed checkpoint to prevent silent corruption

**Configuration** (add to `config/settings.py`):
- `CURSOR_CHECKPOINT_PATH`: default `./data/horizon_cursor.json`
- `CURSOR_FLUSH_EVENTS`: default `100`
- `CURSOR_FLUSH_SECONDS`: default `10.0`

**File locking** — if multiple streamer processes could share the same checkpoint file, acquire an advisory `fcntl.flock` (POSIX) lock before read/write operations to prevent torn writes.

## Security Considerations
- The checkpoint file path must be resolved to an absolute path and validated to remain within the configured data directory to prevent path-traversal attacks if the path is user-supplied via environment variable.
- The checkpoint file must not contain secrets, API keys, or wallet addresses — only the opaque `paging_token` string and timestamp.
- On startup, if the checkpoint file has permissions wider than `0o600`, log a `WARNING` (the file does not contain secrets but world-readable checkpoint files could leak stream position to other processes on shared hosts).
- Wrap all file I/O in `try/except` blocks; a corrupt or unreadable checkpoint should fall back gracefully rather than crash the process.

## Testing Requirements
- Unit tests covering `CursorCheckpoint.load()` with: valid file, missing file, corrupt JSON, invalid paging_token format
- Unit tests covering `CursorCheckpoint.save()` atomicity: simulate a crash mid-write by mocking `os.replace` to raise, assert the original checkpoint is unmodified
- Unit tests covering `FlushPolicy`: verify flush triggers at `max_events` boundary and at `max_seconds` timeout
- Integration tests covering streamer startup: mock Horizon SSE endpoint returning a sequence of events; assert checkpoint advances correctly after each flush interval
- Integration tests covering restart recovery: save a checkpoint, instantiate a new `HorizonStreamer`, assert it calls the mock SSE endpoint with the correct `cursor=` query parameter
- Edge cases: checkpoint file with a future `paging_token` not present on the mock server (Horizon returns 404/410 — streamer should fall back to `"now"`), empty checkpoint file, checkpoint directory not writable
- Performance benchmark: 10,000 simulated events through the flush loop should complete in < 2 seconds; checkpoint write latency (disk) should be < 5 ms p99

## Documentation Requirements
- Update `README.md` Quick Start section to document `CURSOR_CHECKPOINT_PATH` and `--reset-cursor` flag
- Add docstrings to `CursorCheckpoint.__init__`, `load`, `save`, `delete` explaining atomic-write semantics
- Update `config/settings.py` with inline comments explaining each new setting
- Create or update `docs/ingestion.md` with a section on cursor checkpointing, failure modes, and recovery procedures

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Python `asyncio`, SSE clients (`httpx-sse` or `aiohttp`), atomic file I/O, Stellar Horizon API
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python backend engineer with experience building resilient event-streaming consumers. Familiarity with Horizon's paging token model and SSE reconnection semantics is a strong plus. Experience with atomic file operations on Linux and checkpoint/offset patterns from Kafka or Kinesis consumers is highly relevant.
