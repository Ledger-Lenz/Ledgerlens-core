# Ingestion

## Horizon cursor checkpointing

The live Horizon trade consumer persists the last successfully processed
`paging_token` to `CURSOR_CHECKPOINT_PATH` (default
`./data/horizon_cursor.json`). On restart it resumes from that token; when no
valid checkpoint exists it uses `HORIZON_DEFAULT_CURSOR` (default `now`).

Checkpoint writes occur after `CURSOR_FLUSH_EVENTS` processed events (default
100) or `CURSOR_FLUSH_SECONDS` elapsed seconds (default 10), whichever happens
first. A final checkpoint is written on a clean stream exit. The writer creates
a sibling temporary file with mode `0600`, then atomically replaces the live
JSON file. A sidecar advisory lock serializes readers and writers on POSIX.

The checkpoint contains only the paging token, recording time, and optional
ledger sequence. It contains no account, wallet, or API credentials.

### Failure and recovery

- Missing, empty, unreadable, malformed, or invalid-token files are logged and
  treated as absent. Streaming starts from `HORIZON_DEFAULT_CURSOR`.
- A checkpoint with permissions wider than `0600` produces a warning.
- A failed temporary write or replacement leaves the prior checkpoint intact;
  ingestion continues and retries at the next flush.
- If Horizon returns HTTP 404 or 410 for a saved position, the streamer deletes
  the unusable checkpoint and reconnects with `cursor=now`.
- `CURSOR_CHECKPOINT_PATH` must resolve inside `DATA_DIR`, preventing an
  environment-provided path from escaping the runtime data directory.
- Run `python cli.py stream --reset-cursor` to intentionally delete the saved
  position before startup.

The durability window is bounded by the flush policy. A hard crash can replay
at most the events processed since the latest checkpoint; it does not skip
events after the durable token.

## Flow control and backpressure

The async Horizon consumer places parsed trades in a `BoundedTradeQueue`.
`STREAMER_QUEUE_MAXSIZE` (default `1000`) is a hard memory bound. When queue
usage reaches `STREAMER_HIGH_WATER_RATIO` (default `0.8`), the producer sleeps
with exponential backoff from 50 ms up to a two-second cap before enqueueing.
Queue depth, peak depth, throttling time, and aggregate drop counts are exposed
through `HorizonStreamer.metrics_snapshot()`; snapshots never contain trade or
wallet data.

Select the overflow policy with `STREAMER_OVERFLOW_STRATEGY` or the CLI
`--overflow-strategy` option:

| Strategy | Behavior | Use when |
|---|---|---|
| `block` | Wait for queue capacity; no event loss | Completeness is mandatory and SSE disconnect risk is acceptable |
| `drop_newest` | Discard the incoming trade when full | Existing queued work should finish in order and gaps can be backfilled |
| `drop_oldest` | Discard the oldest queued trade and retain the newest | Low-latency, real-time scoring values recency over completeness |

`drop_oldest` is the default. A hostile or noisy stream can use it to evict
older high-value events, so high-security deployments should prefer `block`
and use durable cursor checkpoints to recover after reconnects. Both drop
strategies require historical gap backfill when complete coverage is needed.
Changing a queue's strategy after construction is intentionally unsupported.

Dropped-event warnings are rate-limited to every 100 events. Operators should
alert on non-zero drop counts and sustained high-water-mark hits.

## Parallel historical loading

`python cli.py historical-load` divides an inclusive-start, exclusive-end time
range into independent chunks and fetches them concurrently through the shared
retrying Horizon client. Each response is validated into the canonical
Pydantic `Trade` model before a page-sized SQLite batch is written with
`INSERT OR IGNORE`.

Chunk completion is stored atomically in `HISTORICAL_PROGRESS_PATH` (default
`./data/historical_progress.json`). With `--resume`, completed chunks make no
HTTP requests; failed and interrupted chunks are retried. The progress path
must remain inside `DATA_DIR`.

Defaults are controlled by `HISTORICAL_LOADER_CONCURRENCY=4`,
`HISTORICAL_CHUNK_HOURS=6.0`, and
`HISTORICAL_MAX_LOOKBACK_DAYS=365`. Larger concurrency improves throughput
until Horizon's per-IP rate limit is reached. Start conservatively, monitor
429 responses, and reduce concurrency when retries dominate. Smaller chunks
improve load balancing and restart granularity but increase progress metadata
and initial request overhead.

## API version management

The Stellar Horizon API evolves over time.  Between major versions, field
names can be renamed, types can change, and new required envelope keys can
be added.  Without explicit version checking, a Horizon upgrade can silently
corrupt ingested data or produce cryptic Pydantic parse failures deep in the
pipeline with no indication that the root cause is an API version mismatch.

LedgerLens addresses this with a **VersionGuard** middleware layer inside
`RetryingHorizonClient` (defined in `ingestion/http_client.py`).

### How it works

Every Horizon response includes the `X-Stellar-Horizon-Version` header
(e.g. `"2.28.0"`).  On each HTTP response, `VersionGuard.check()`:

1. Parses the header value using `packaging.version.Version` (semantic
   versioning).
2. Checks the parsed version against the `[HORIZON_MIN_VERSION,
   HORIZON_MAX_VERSION)` range from `config/settings.py`.
3. Raises `HorizonVersionError` if the version is outside the range —
   before the response body reaches any Pydantic model.
4. Emits a `WARNING` log if the version is in range but differs from
   `HORIZON_TESTED_VERSION` (the version the current data models were
   validated against).

The validated version is cached in memory for the lifetime of the client
instance so there is no per-response overhead after the first check.

Pre-release versions (e.g. `"2.28.0-rc1"`) have their suffix stripped
before comparison, with a `WARNING` log noting the pre-release suffix.

If the header is absent (some proxy configurations strip it), the check is
a no-op.

### Structural response validation

In addition to version header checking, two helper functions validate that
response bodies contain expected structural keys before Pydantic parsing:

| Helper | Checked keys | When to use |
|---|---|---|
| `validate_list_response(body, url)` | `_embedded`, `_embedded.records` | List endpoints (`/trades`, `/operations`, …) |
| `validate_single_record_response(body, url)` | `id`, `paging_token` | Single-resource endpoints |

Both raise `HorizonSchemaError` with the name of the missing key and the
request URL when a structural key is absent.

### Configuration

Four environment variables control version checking (all settable in `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `HORIZON_MIN_VERSION` | `"2.0.0"` | Inclusive lower bound of supported range |
| `HORIZON_MAX_VERSION` | `"4.0.0"` | Exclusive upper bound of supported range |
| `HORIZON_TESTED_VERSION` | `"2.28.0"` | Version against which data models were validated |
| `HORIZON_VERSION_CHECK_ENABLED` | `true` | Set to `false` to disable checking (see below) |

### Checking which Horizon version is in use

Call `probe_server_version()` on a client instance to fetch the Horizon root
endpoint and log the server version:

```python
from ingestion.http_client import AsyncHorizonClient

async with AsyncHorizonClient(settings.horizon_url) as client:
    version = await client.probe_server_version()
    # Logs: INFO "Connected to Horizon 2.28.0 at https://horizon.stellar.org"
    print(version)  # "2.28.0"
```

This is useful at pipeline startup to confirm exactly which Horizon instance
is being used.

### Updating the tested version range after a Horizon upgrade

1. **Review the changelog** for the new version:
   <https://github.com/stellar/go/blob/master/services/horizon/CHANGELOG.md>

2. **Verify schema compatibility** — check whether any fields used by
   `ingestion/data_models.py` have been renamed, re-typed, or removed.

3. **Update `data_models.py`** if the new version introduces a breaking
   change.

4. **Bump `HORIZON_TESTED_VERSION`** in `.env` (or `config/settings.py`
   default) to the new version string, e.g.:
   ```
   HORIZON_TESTED_VERSION=2.30.0
   ```

5. **Widen the range** if the new version crosses a major-version boundary:
   ```
   HORIZON_MIN_VERSION=2.0.0
   HORIZON_MAX_VERSION=5.0.0   # if you are now supporting Horizon 4.x
   ```

6. **Run the test suite** to confirm no regressions:
   ```bash
   pytest tests/test_version_guard.py
   ```

### What to do when a HorizonVersionError is raised in production

A `HorizonVersionError` means the live Horizon node is running a version
outside `[HORIZON_MIN_VERSION, HORIZON_MAX_VERSION)`.  The pipeline will
refuse to ingest data until the issue is resolved.  Options:

- **If you control the Horizon node**: upgrade or downgrade it to a version
  within the supported range.
- **If using a third-party Horizon node**: contact the provider, or switch to
  a node running a supported version.
- **If you have verified schema compatibility manually** and want to widen the
  range temporarily, update `HORIZON_MIN_VERSION` / `HORIZON_MAX_VERSION` in
  `.env` and restart the pipeline.  Always update `HORIZON_TESTED_VERSION` at
  the same time.
- **Do not** set `HORIZON_VERSION_CHECK_ENABLED=false` as a permanent fix —
  this silences the warning but does not resolve the underlying schema
  compatibility risk.

### Disabling version checking

Set `HORIZON_VERSION_CHECK_ENABLED=false` in `.env` for private or custom
Horizon nodes that strip the `X-Stellar-Horizon-Version` header.

> **Warning**: when disabled, a `WARNING` log is emitted at startup:
> ```
> Horizon version checking disabled — schema compatibility not guaranteed
> ```
> This is intentional: operators must acknowledge the risk rather than
> silently disabling the check in production.

### Exception reference

| Exception | Raised when |
|---|---|
| `HorizonVersionError` | The `X-Stellar-Horizon-Version` header is present and outside `[min, max)` |
| `HorizonSchemaError` | A response body is missing a required structural key |

Both exceptions include the request URL in their message.
`HorizonVersionError` never includes response body content (security).
