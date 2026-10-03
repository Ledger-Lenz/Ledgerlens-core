# Batch Idempotency, Streaming Export & Tiered Retention

## Batch scoring idempotency (`POST /scores/batch`)

Send an `Idempotency-Key` header (max 255 characters, for example a UUID) with
each batch submission. If a retry arrives with the same key inside the dedup
window, the API returns the **original** job (`job_id`, current `status`) and
does not queue the batch again. Requests without the header behave as before.

- Window: `BATCH_IDEMPOTENCY_WINDOW_HOURS` (default **24**). Keys older than
  the window are purged and can be reused.
- Keys are global, not scoped to a request body. Use a new key for each
  logical batch.
- TypeScript SDK: `client.submitBatch(wallets, { idempotencyKey })`.

## Streaming exports (`GET /export/scores.csv`, `GET /export/scores.parquet`)

Exports are now streamed with chunked transfer encoding. The server reads
`EXPORT_CHUNK_SIZE` rows at a time (default **1000**) and holds only one chunk
in memory. CSV is written a chunk at a time. Parquet is written as one row
group per chunk.

Memory guardrail: exports that match more than `EXPORT_MAX_ROWS` rows (default
**1,000,000**) are rejected up front with **413**. This happens before any
bytes are sent, so a response never fails partway through because of the cap.

### Migration for existing consumers

URLs, query parameters, and output formats have not changed. Clients that read
the whole body keep working. To get the memory benefit on the client side too,
read the body incrementally (for example `response.body` in the TypeScript
SDK's `exportScoresCsv`, `requests.get(..., stream=True).iter_content()` in
Python). Clients that ask for very large windows should handle **413** by
splitting the date range.

## Tiered retention (`storage.retention.TieredRetentionEngine`)

| Tier | Default age | Backend | Added query latency |
|---|---|---|---|
| hot | < 30 days | primary SQLite DB | none |
| warm | 30-365 days | separate SQLite DB (`./data/warm.db`) | milliseconds |
| cold | > 365 days | monthly Parquet files (`./data/cold/YYYY-MM/<table>.parquet`) | 100 ms to seconds (file scan) |

- `migrate()` moves hot to warm in a single transaction (via `ATTACH`), then
  warm to cold. It checks that each table's total row count across all tiers
  is unchanged, and raises `TierIntegrityError` if it is not. Running it again
  is safe (idempotent).
- `start_scheduler(interval_seconds=86400)` runs `migrate()` daily in a daemon
  thread. `stop_scheduler()` stops it.
- `query(table, start, end)` returns matching rows from every tier that can
  hold them, merged and sorted by timestamp. Queries that only cover the last
  `hot_days` never touch warm or cold storage.
