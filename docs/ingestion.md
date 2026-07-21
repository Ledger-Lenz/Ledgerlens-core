# Ingestion

## Horizon cursor checkpointing

`python cli.py stream` persists two things that must never durably desync:
the last successfully processed Horizon `paging_token` (the resume position
for the SSE stream) and the per-wallet rolling-window state the wash-trading
detector scores against. Before this was a single checkpoint, the two were
flushed independently — the cursor on a time-or-count trigger, the window
state on a count-only trigger. Under sustained throughput below the
count threshold, the cursor's timer kept advancing while the window-state
flush stalled indefinitely; a crash in that gap meant restarting from a
cursor *ahead of* the trades the detector's windows actually reflected,
silently and permanently dropping them from detection.

`ingestion/stream_checkpoint.py`'s `StreamCheckpointCoordinator` replaces
both triggers with one unified SQLite checkpoint (`stream_checkpoint` table,
same database as `rolling_window_checkpoints`): each flush writes the cursor
and every wallet's window state in a single `BEGIN IMMEDIATE` transaction, so
either both land durably or neither does. The cursor can therefore never be
ahead of the window state it depends on, by construction rather than by
narrowing a timing window.

Flushes are triggered by `STREAM_CHECKPOINT_INTERVAL` processed trades
(default 100, previously the window-state-only batch size) or
`CURSOR_FLUSH_SECONDS` elapsed seconds (default 10, previously the
cursor-only durability bound), whichever happens first — preserving the
pre-fix checkpoint cadence under high throughput while closing the low
throughput crash gap. A final checkpoint is written on clean exit and on
`SIGTERM`/`SIGINT`.

### Migration from the legacy JSON cursor file

Deployments upgrading from before the unified checkpoint keep their existing
`CURSOR_CHECKPOINT_PATH` JSON file (default `./data/horizon_cursor.json`) as
a one-time seed: if no unified checkpoint row exists yet, its cursor is read
once, logged, and used to resume. The legacy file is never written to again
once the first unified checkpoint is written, and never read again after
that. It also still backs the existing "delete stale checkpoint on HTTP
404/410" fallback used while reconnecting mid-stream.

### Failure and recovery

- Missing, empty, unreadable, malformed, or invalid-token legacy checkpoints
  are logged and treated as absent; streaming starts from
  `HORIZON_DEFAULT_CURSOR` (default `now`) when no unified checkpoint exists
  either.
- A malformed cursor passed to the unified checkpoint's `flush()` is refused
  (logged, not written); the prior durable checkpoint is left intact.
- A failed unified-checkpoint write (e.g. `OSError`, `sqlite3.Error`) is
  logged and returns rather than raising — the stream keeps running on the
  prior durable checkpoint and retries at the next flush.
- If Horizon returns HTTP 404 or 410 for a saved position, the streamer
  deletes the legacy checkpoint file and reconnects with `cursor=now`.
- `CURSOR_CHECKPOINT_PATH` must resolve inside `DATA_DIR`, preventing an
  environment-provided path from escaping the runtime data directory.
- Run `python cli.py stream --reset-cursor` to delete both the legacy file
  and the unified checkpoint row before startup.
- **Desync detection (defense in depth):** on load, the wallet count
  recorded in the unified checkpoint is compared against the wallet count
  actually loaded from `rolling_window_checkpoints`. A mismatch can only
  happen if the database was altered outside the atomic transaction (manual
  editing, filesystem-level corruption); it is logged at `ERROR` and
  incremented on `ledgerlens_checkpoint_desync_detected_total` rather than
  raised, since the checkpoint layer treats storage corruption as a
  recoverable, operator-visible condition. A nonzero counter means manual
  reconciliation against Horizon ledger history is recommended before
  trusting the detector's window state.

The durability window is bounded by the flush policy. A hard crash can replay
at most the events processed since the latest checkpoint; it does not skip
events after the durable token, and the rolling-window state it scores
against is always consistent with that token.

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
