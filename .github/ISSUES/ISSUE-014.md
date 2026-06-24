---
title: "Create a Trade Ingestion Dead-Letter Queue with Structured Error Classification"
labels: ["difficulty: advanced", "area: ingestion", "type: reliability"]
assignees: []
---

## Summary
When ingestion fails for an individual trade or event record — due to a Pydantic parse error, a network timeout, or a schema mismatch — LedgerLens currently either silently drops the record or crashes the entire ingestion loop. Neither is acceptable in a production fraud detection system: silent drops create undetectable coverage gaps, while crashes require manual intervention. A Dead-Letter Queue (DLQ) with structured error classification will capture failed records, categorise the failure reason, enable operator inspection, and support selective replay once the root cause is fixed.

## Background & Context
The ingestion layer processes records from multiple sources: Horizon SSE stream (`horizon_streamer.py`), historical REST pages (`historical_loader.py`), operations stream (`operations_loader.py`), AMM events (`amm_loader.py`), and bridge events (`bridge_loader.py`, `evm_loader.py`). Each source has its own failure modes:
- **Parse errors** (`ParseError`): Pydantic `ValidationError` — Horizon returned a field with an unexpected type or value
- **Network errors** (`NetworkError`): `httpx.TimeoutException`, `httpx.ConnectError` — transient connectivity issue
- **Schema errors** (`SchemaError`): `HorizonSchemaError` (from ISSUE-005) — missing required envelope keys
- **Storage errors** (`StorageError`): `sqlite3.OperationalError` — disk full, locked database
- **Version errors** (`VersionError`): `HorizonVersionError` (from ISSUE-005) — API version mismatch

Each error class has a different remediation: parse errors require a code fix; network errors should be auto-replayed after a delay; schema errors indicate an API change; storage errors require operator action.

The DLQ should use the same SQLite database as the rest of LedgerLens (for simplicity) with a dedicated `dead_letter_queue` table. A CLI sub-command must allow operators to inspect, filter, and replay DLQ entries without restarting the ingestion pipeline.

## Objectives
- [ ] Implement a `DeadLetterQueue` class in a new `ingestion/dlq.py` module backed by a SQLite table, with methods for enqueue, list, filter by error class, replay, and purge.
- [ ] Implement an `ErrorClassifier` that takes an exception and raw record bytes and returns a `DLQErrorClass` enum value with a structured `DLQEntry` payload.
- [ ] Integrate DLQ enqueue calls into `horizon_streamer.py`, `historical_loader.py`, `operations_loader.py`, and `bridge_loader.py` at every point where a record is currently silently dropped or causes a crash.
- [ ] Add `cli.py dlq` sub-commands: `list`, `replay`, `purge`, and `inspect <id>` for operator use.

## Technical Requirements

**`DLQErrorClass` enum:**
```python
class DLQErrorClass(str, Enum):
    PARSE_ERROR = "parse_error"         # Pydantic ValidationError
    NETWORK_ERROR = "network_error"     # Transient HTTP/connection failure
    SCHEMA_ERROR = "schema_error"       # Missing envelope keys / version mismatch
    STORAGE_ERROR = "storage_error"     # SQLite write failure
    VERSION_ERROR = "version_error"     # Horizon version out of range
    UNKNOWN = "unknown"                 # Unclassified exception
```

**`DLQEntry` schema:**
```python
@dataclass
class DLQEntry:
    id: str                              # UUID v4
    source: str                          # e.g. "horizon_streamer", "bridge_loader"
    error_class: DLQErrorClass
    error_message: str                   # exc.__class__.__name__ + ": " + str(exc), truncated to 1000 chars
    raw_record: bytes                    # original raw bytes (JSON from Horizon, etc.)
    raw_record_hash: str                 # SHA-256 of raw_record (for dedup on replay)
    created_at: datetime
    retry_count: int = 0
    last_retry_at: datetime | None = None
    status: Literal["pending", "replayed", "resolved", "dead"] = "pending"
    resolved_at: datetime | None = None
    resolution_note: str | None = None  # set by operator on manual resolution
```

**SQLite schema:**
```sql
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    error_class TEXT NOT NULL,
    error_message TEXT NOT NULL,
    raw_record BLOB NOT NULL,
    raw_record_hash TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_retry_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_at TIMESTAMP,
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_dlq_status ON dead_letter_queue (status);
CREATE INDEX IF NOT EXISTS idx_dlq_error_class ON dead_letter_queue (error_class);
CREATE INDEX IF NOT EXISTS idx_dlq_source ON dead_letter_queue (source);
CREATE INDEX IF NOT EXISTS idx_dlq_created_at ON dead_letter_queue (created_at);
```

**`ErrorClassifier`:**
```python
class ErrorClassifier:
    @staticmethod
    def classify(exc: Exception) -> DLQErrorClass:
        if isinstance(exc, ValidationError):
            return DLQErrorClass.PARSE_ERROR
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
            return DLQErrorClass.NETWORK_ERROR
        if isinstance(exc, HorizonSchemaError):
            return DLQErrorClass.SCHEMA_ERROR
        if isinstance(exc, sqlite3.OperationalError):
            return DLQErrorClass.STORAGE_ERROR
        if isinstance(exc, HorizonVersionError):
            return DLQErrorClass.VERSION_ERROR
        return DLQErrorClass.UNKNOWN
```

**`DeadLetterQueue` interface:**
```python
class DeadLetterQueue:
    def __init__(self, db_conn: sqlite3.Connection, max_size: int = 100_000): ...

    def enqueue(
        self,
        source: str,
        exc: Exception,
        raw_record: bytes,
    ) -> DLQEntry: ...

    def list_entries(
        self,
        status: str | None = None,
        error_class: DLQErrorClass | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DLQEntry]: ...

    def get_entry(self, entry_id: str) -> DLQEntry | None: ...

    def mark_resolved(self, entry_id: str, note: str) -> None: ...

    def purge(
        self,
        status: str = "resolved",
        older_than_days: int = 30,
    ) -> int: ...
```

**CLI sub-commands:**
```bash
python cli.py dlq list [--status pending] [--error-class parse_error] [--source horizon_streamer] [--limit 50]
python cli.py dlq inspect <id>       # pretty-print DLQEntry + raw_record (truncated)
python cli.py dlq replay <id>        # re-attempt ingestion for a single entry
python cli.py dlq replay --error-class network_error  # replay all pending network errors
python cli.py dlq purge --status resolved --older-than-days 7
```

**Replay mechanism**: `dlq replay` re-parses `raw_record` and routes it to the appropriate ingestion handler based on `source`. If replay succeeds, mark as `resolved`. If it fails again, increment `retry_count`; after 8 retries, mark as `dead`.

**DLQ size cap**: when `len(pending entries) >= max_size`, log a `CRITICAL` alert and stop enqueuing new entries (drop them with a log line) — do not let the DLQ itself consume unbounded storage. Operators must address the root cause.

**Auto-replay on startup**: `NetworkError` entries that were created more than 60 seconds ago are automatically replayed once on pipeline startup (since the transient network issue may have resolved).

## Security Considerations
- `raw_record` (BLOB) may contain wallet addresses, transaction hashes, and API response data. It must be stored encrypted at rest if `LEDGERLENS_DB_ENCRYPTION_KEY` is set (use the same encryption as HMAC secrets per the webhook README section).
- The `error_message` field must truncate exception messages at 1,000 characters to prevent a crafted Horizon response from storing arbitrarily large data in the DLQ.
- `raw_record_hash` enables deduplication — if the same raw bytes have already been enqueued (e.g., due to a retry loop), `enqueue` should update `retry_count` on the existing entry rather than creating a new row.
- The `dlq inspect` CLI command must not print `raw_record` in full — it should print only the first 500 bytes (printable ASCII) to prevent accidentally displaying sensitive data in terminal logs.
- All SQL queries must use parameterised statements — `error_class` filter values are from enums but SQL injection from CLI input must still be prevented by using `?` placeholders, not f-strings.

## Testing Requirements
- Unit tests covering `ErrorClassifier.classify()`: each exception type → correct `DLQErrorClass`
- Unit tests covering `DeadLetterQueue.enqueue()`: new entry created, duplicate raw_record increments `retry_count` on existing entry, `max_size` enforcement (drops with log when full)
- Unit tests covering `list_entries()` filters: by status, by error_class, by source, combined filters, pagination (limit/offset)
- Unit tests covering `purge()`: removes resolved entries older than threshold, leaves pending entries intact
- Integration tests: simulate a Pydantic `ValidationError` in `horizon_streamer.py`; assert the failed record is enqueued in the DLQ with `error_class=PARSE_ERROR`
- Integration tests: run `cli.py dlq replay --error-class network_error` against a mock that now succeeds; assert entries marked `resolved`
- Edge cases: `raw_record` of 1 MB (large Horizon response), `error_message` with non-ASCII characters, `enqueue` called concurrently from multiple async workers (thread safety)
- Performance benchmark: enqueuing 1,000 DLQ entries should complete in < 1 second

## Documentation Requirements
- Add module docstring to `ingestion/dlq.py` explaining the DLQ pattern, error classification taxonomy, and replay semantics
- Update `README.md` CLI Reference to document `cli.py dlq` sub-commands
- Update `docs/ingestion.md` with a section on the DLQ, including guidance on how to triage each error class
- Add `DLQErrorClass` enum values and their remediation guidance to a new `docs/ops-runbook.md`

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: dead-letter queues, error classification patterns, SQLite, Python CLI tooling (`typer` or `click`), exception handling in async pipelines
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python backend engineer with experience building production-grade data ingestion pipelines with robust error handling and observability. Familiarity with dead-letter queue patterns from message broker systems (SQS DLQ, Kafka DLQ) and experience with SQLite-backed operational stores. Strong Python exception handling and CLI tooling skills.
