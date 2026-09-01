# storage/

This directory contains the data lifecycle layer for LedgerLens: the immutable
audit log that records every system event, and the retention engine that
archives and purges data according to per-table TTL policies.

Both modules operate on the same SQLite database configured by
`LEDGERLENS_DB_PATH`, but they serve distinct roles:

| File | Purpose |
|------|---------|
| [`audit_log.py`](#audit_logpy) | Append-only, HMAC-SHA256-chained event log for operational security events |
| [`retention.py`](#retentionpy) | Archive-then-purge engine that enforces per-table data-retention TTLs |

---

## audit_log.py

Implements a tamper-evident audit trail for security-relevant system events.
Each row is cryptographically linked to the previous row via an HMAC-SHA256
chain hash:

```
entry_hash = HMAC-SHA256(key=LEDGERLENS_AUDIT_SECRET,
                         msg=canonical_json(entry_without_hash))
```

A `genesis` sentinel row (with `prev_hash = "genesis"`) is inserted
automatically when the table is first created. Any retroactive modification
to a row breaks the chain and is detected by the verifier.

**Logged event types:**

| Event | Meaning |
|-------|---------|
| `score_computed` | A risk score was computed for a wallet |
| `api_key_used` | An API key was used to authenticate a request |
| `admin_config_changed` | An admin changed a runtime configuration value |
| `suppression_rule_added` | A scoring suppression rule was added |
| `suppression_rule_removed` | A scoring suppression rule was removed |
| `audit_chain_verified` | The full audit chain was verified (integrity self-check) |

**Configuration:**

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `LEDGERLENS_AUDIT_SECRET` | dev-only fallback | HMAC key; **must be set in production** |
| `LEDGERLENS_DB_PATH` | `./ledgerlens.db` | SQLite database path |

> **Security:** Always set `LEDGERLENS_AUDIT_SECRET` to a secret value in
> production. The default fallback is intentionally weak and logs a warning at
> startup.

---

## retention.py

Enforces a two-phase archive-then-purge retention policy across three tables:

| Table | Default TTL | Notes |
|-------|-------------|-------|
| `risk_scores` | 365 days | Wallet risk score history |
| `feature_vectors` | 90 days | Trade features (referred to as "trades data") |
| `alerts` | 730 days | Alert events |

Rows older than the TTL are first written to Parquet files under
`data/archive/YYYY-MM/` (cold storage), then deleted from SQLite. The
archival is **safe by construction**: the count invariant
`parquet_rows + sqlite_rows == pre-archival_sqlite_rows` can be verified
after each run.

The retention engine is designed to be invoked nightly by the scheduler.
TTLs and the archive root are configurable at construction time.

---

## Relationship to other audit components

This directory is **not** the same as [`audit/`](../audit/README.md).

- `storage/audit_log.py` — records **operational security events** (API key
  usage, admin changes, score computations) in a lightweight HMAC chain. Think
  of this as the system-operations log.
- `audit/scoring_events.py` — records **scoring decision provenance** with
  full feature snapshots and model version, using a SHA-256 event-sourcing
  chain. Think of this as the ML decision ledger required for regulatory
  non-repudiation.

Both layers write to the same SQLite database but to different tables.

---

## Further reading

- [docs/audit_log.md](../docs/audit_log.md) — full audit log design, chain
  hash specification, API endpoints, and retention configuration
- [docs/database_schema.md](../docs/database_schema.md) — complete SQLite
  schema including the `audit_log` and `on_chain_submissions` tables
- [docs/feature_store_archival.md](../docs/feature_store_archival.md) — the
  dual-tier (hot SQLite / cold Parquet) archival architecture used by the
  feature distribution store (related to the retention pattern used here)
