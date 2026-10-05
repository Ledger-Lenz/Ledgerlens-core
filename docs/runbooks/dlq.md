# Runbook: Trade ingestion dead-letter queue

`ingestion/dlq.py` (`TradeDLQ`) stores ingestion records that failed to
process in the `dead_letter_queue` SQLite table. This runbook covers
inspecting, replaying, and handling quarantined (poison) entries.

## Entry lifecycle

| Status        | Meaning                                                            |
|---------------|--------------------------------------------------------------------|
| `pending`     | Waiting for replay.                                                |
| `replayed`    | Replayed successfully.                                             |
| `dead`        | Manually marked dead by an operator (`TradeDLQ.mark_dead`).        |
| `quarantined` | Failed replay `max_replay_failures` times (default 3); never retried automatically. |

Every failed replay increments `replay_failures` and stores `last_replay_error`.
When the limit is reached the entry is quarantined, `ledgerlens_dlq_quarantined_total`
is incremented, and a `DLQ_QUARANTINE` error is logged (custom alert hooks can be
passed as `TradeDLQ(alert_fn=...)`).

## Metrics and alerts

| Metric                                     | Description                          |
|--------------------------------------------|--------------------------------------|
| `ledgerlens_dlq_depth`                     | Pending entries                      |
| `ledgerlens_dlq_oldest_entry_age_seconds`  | Age of the oldest pending entry      |
| `ledgerlens_dlq_quarantined_total`         | Entries quarantined, by `error_class` |

The gauges are refreshed by `TradeDLQ.refresh_metrics()` (called by every
`trade-dlq` CLI command). Both are charted on the **LedgerLens Core Detection & API**
Grafana dashboard ("Trade DLQ Depth", "Trade DLQ Oldest Entry Age").

Alerts (`monitoring/alerts.yml`):

- `TradeDLQPoisonMessageQuarantined` — an entry was quarantined in the last 15 minutes.
- `TradeDLQBacklogAging` — the oldest pending entry is older than 1 hour.

## Procedures

List entries and current depth / age:

```bash
python cli.py trade-dlq list --status pending
python cli.py trade-dlq list --status quarantined
```

Inspect an entry, including its raw record and last replay error:

```bash
python cli.py trade-dlq inspect 42
```

Replay selected entries through a handler (`module:function`, called with the
decoded JSON record). Each outcome (`replayed`, `failed`, `quarantined`, `skipped`)
is printed; the command exits non-zero if any entry did not replay:

```bash
python cli.py trade-dlq replay 42 43 --handler mypkg.ingest:process_trade
```

### Handling a quarantined entry

1. `trade-dlq inspect <id>` and read `last_replay_error`.
2. If the record is malformed, leave it quarantined — it is kept for audit.
3. If the root cause was fixed (e.g. schema update), reset it to pending and replay:

   ```sql
   UPDATE dead_letter_queue SET status = 'pending', replay_failures = 0 WHERE id = <id>;
   ```
