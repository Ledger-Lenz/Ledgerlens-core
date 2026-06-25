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
