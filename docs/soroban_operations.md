# Soroban Operations Guide

Operational runbook for the LedgerLens Soroban circuit breaker, health endpoint, dead-letter queue (DLQ), and manual reset procedures.

## Circuit Breaker States

The `SorobanPublisher` maintains a three-state circuit breaker:

| State | Meaning |
|-------|---------|
| `closed` | Normal operation; submissions proceed. |
| `open` | Submissions are blocked; failed items are written to the DLQ. |
| `half-open` | Probe mode; one submission attempt is allowed to test recovery. |

### State Transitions

```
[closed] ---(≥ threshold consecutive failures)---> [open]
[open]   ---(SOROBAN_CIRCUIT_RESET_SECONDS elapsed)---> [half-open]
[half-open] ---(probe success)---> [closed]
[half-open] ---(probe failure)---> [open]  (timer restarts)
[any]    ---(POST /admin/soroban/reset)---> [closed]
```

The threshold is controlled by `SOROBAN_CIRCUIT_BREAKER_THRESHOLD` (default: 5).  
The reset timeout is `SOROBAN_CIRCUIT_RESET_SECONDS` (default: 300 seconds).

---

## Health Endpoint

`GET /admin/soroban/health` — requires `X-LedgerLens-Admin-Key`.

Returns:

```json
{
  "circuit_state": "closed",
  "consecutive_failures": 0,
  "last_error": null,
  "circuit_opened_at": null,
  "seconds_until_reset": null,
  "dlq_pending_count": 0
}
```

### Field Interpretation

| Field | Description |
|-------|-------------|
| `circuit_state` | One of `closed`, `open`, `half-open`. |
| `consecutive_failures` | Failures since the last success or manual reset. |
| `last_error` | The most recent error message. `null` when healthy. |
| `circuit_opened_at` | ISO 8601 timestamp when the circuit opened. `null` if closed. |
| `seconds_until_reset` | Seconds until the circuit transitions to `half-open`. `null` if closed. |
| `dlq_pending_count` | Unprocessed items in `soroban_dead_letters`. |

**HTTP 503** is returned if `LEDGERLENS_ADMIN_API_KEY` is not configured.

---

## Manual Reset

`POST /admin/soroban/reset` — requires `X-LedgerLens-Admin-Key`.

Immediately closes the circuit, clears `consecutive_failures` and `last_error`, and returns the new health snapshot. Rate-limited to **10 calls/minute**.

```bash
curl -X POST http://localhost:8000/admin/soroban/reset \
  -H "X-LedgerLens-Admin-Key: $LEDGERLENS_ADMIN_API_KEY"
```

**When to use**: after fixing the underlying issue (e.g., re-funding the service account, redeploying the contract). Without this, the circuit waits `SOROBAN_CIRCUIT_RESET_SECONDS` before entering `half-open`.

The reset event is logged at `WARNING` level including the requesting IP for auditability.

---

## Dead-Letter Queue (DLQ)

When the circuit is `open`, submissions are not silently dropped — they are written to the `soroban_dead_letters` SQLite table with `status = 'pending'`.

### Schema

```sql
CREATE TABLE soroban_dead_letters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    asset_pair      TEXT NOT NULL,
    score           INTEGER NOT NULL,
    ledger_timestamp INTEGER NOT NULL,
    error_message   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'replayed', 'failed')),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    replayed_at     TIMESTAMP,
    replay_tx_hash  TEXT
);
```

### Inspecting the DLQ

```bash
curl http://localhost:8000/admin/soroban/dead-letters \
  -H "X-LedgerLens-Admin-Key: $LEDGERLENS_ADMIN_API_KEY"

# Filter by status:
curl "http://localhost:8000/admin/soroban/dead-letters?status=pending&page=1&page_size=50" \
  -H "X-LedgerLens-Admin-Key: $LEDGERLENS_ADMIN_API_KEY"
```

### DLQ Replay Procedure

After fixing the Soroban service issue and optionally resetting the circuit:

```bash
# Dry run — print pending items without submitting
python cli.py dlq-replay --dry-run

# Replay up to 100 pending items
python cli.py dlq-replay --limit 100
```

Each item is marked `replayed` (with `replay_tx_hash`) on success or `failed` on persistent failure. Rows are never deleted.

### Row Cap

`SOROBAN_DLQ_MAX_ROWS` (default: 10 000) caps the table size. Oldest rows are pruned when exceeded.

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `SOROBAN_CIRCUIT_BREAKER_THRESHOLD` | `5` | Consecutive failures before circuit opens. |
| `SOROBAN_CIRCUIT_RESET_SECONDS` | `300` | Seconds until auto-transition to `half-open`. |
| `SOROBAN_DLQ_MAX_ROWS` | `10000` | Hard cap on `soroban_dead_letters` rows. |

---

## CLI Reference Addition

| Command | Description |
|---------|-------------|
| `python cli.py dlq-replay` | Replay pending Soroban DLQ submissions. |
| `python cli.py dlq-replay --dry-run` | Print pending DLQ items without submitting. |
| `python cli.py dlq-replay --limit N` | Replay at most N items per run. |
