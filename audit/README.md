# audit/

This directory implements **scoring decision provenance** — an event-sourced,
cryptographically tamper-evident record of every risk scoring decision made by
LedgerLens, including the full feature snapshot and model version that produced
it.

| File | Purpose |
|------|---------|
| [`scoring_events.py`](#scoring_eventspy) | Async SQLite-backed append-only store for scoring decisions, with SHA-256 chain hashing |

---

## How this differs from `storage/audit_log.py`

There are two audit-related modules in this repository. They solve different
problems and should not be confused:

| | `audit/scoring_events.py` (this directory) | `storage/audit_log.py` |
|---|---|---|
| **Purpose** | ML decision ledger — records every scoring decision with the exact feature values that produced it | Operations security log — records API key usage, admin config changes, and score computations |
| **Primary audience** | Regulators, compliance teams, data scientists | Security teams, incident responders |
| **Chain type** | SHA-256 over `(prev_hash, event_id, wallet, score, features, occurred_at)` | HMAC-SHA256 over the full row using a server-side secret |
| **Feature snapshot** | ✅ Full feature vector captured per event | ❌ Not captured |
| **Storage** | `scoring_events` table in SQLite | `audit_log` table in SQLite |
| **Async** | ✅ `aiosqlite` | ❌ Synchronous `sqlite3` |
| **Default retention** | 7 years (FATF AML minimum) | Configurable |

Both modules write to the same SQLite database (`LEDGERLENS_DB_PATH`) but to
different tables.

---

## scoring_events.py

### Design

`scoring_events.py` implements an **event-sourced** store where the current
risk score for any wallet is derivable by replaying its event history. The
store is append-only at both the application layer and the database layer
(enforced by SQLite `BEFORE UPDATE` / `BEFORE DELETE` triggers).

**Core classes:**

- **`ScoringEvent`** — dataclass containing `event_id`, `wallet`, `score`,
  `feature_snapshot` (full feature dict), `model_version`, `triggered_by`,
  `actor_id`, `chain_hash`, and `occurred_at`.
- **`ScoringEventStore`** — async store; `append()` computes the chain hash
  and inserts; `replay()` returns events in chronological order.
- **`ChainHashVerifier`** — walks the chain, recomputes each hash, and returns
  a `ChainVerificationResult` with status `VALID`, `TAMPERED`, or
  `NO_EVENTS`.

### Chain hash

Each event's `chain_hash` is:

```
SHA-256(canonical_json({
    "prev": previous_chain_hash | "GENESIS",
    "event_id": event_id,
    "wallet": wallet,
    "score": score,
    "features": {sorted feature_snapshot},
    "occurred_at": occurred_at.isoformat()
}))
```

The feature snapshot is included in the hash so that retroactively modifying
a feature value (without creating a new event) invalidates the chain.

### `triggered_by` values

The `triggered_by` field is validated against a fixed enum — it is **not** a
free string:

| Value | Meaning |
|-------|---------|
| `ingestion` | Score triggered by the automated ingestion pipeline |
| `manual_recompute` | Score triggered by an operator recompute |
| `feedback_boost` | Score adjusted by analyst feedback |
| `admin_override` | Score overridden by an administrator (requires non-null `actor_id`) |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIT_LOG_ENABLED` | `true` | Enable the audit log |
| `AUDIT_FEATURE_SNAPSHOT_MAX_KEYS` | `50` | Maximum feature keys per event |
| `AUDIT_VERIFY_ON_READ` | `false` | Verify chain on every `GET /audit` call |
| `AUDIT_RETENTION_DAYS` | `2555` | Minimum retention (7 years) |

### API endpoints

The `api/audit_router.py` exposes three admin-authenticated endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/audit/wallet/{wallet}` | Full event history for a wallet (oldest first) |
| `GET` | `/audit/wallet/{wallet}/verify` | Chain integrity verification |
| `GET` | `/audit/summary` | 24-hour event count and unique wallets |

---

## Further reading

- [docs/audit_log.md](../docs/audit_log.md) — full design rationale, chain
  hash specification, database schema, API endpoint reference, and regulatory
  retention requirements
- [storage/README.md](../storage/README.md) — the operations security audit
  log (`storage/audit_log.py`) and data retention engine (`storage/retention.py`)
- [docs/database_schema.md](../docs/database_schema.md) — `scoring_events`
  table DDL and trigger definitions
- [docs/database_migrations.md](../docs/database_migrations.md) — Alembic
  migration `0002_scoring_events` which created this table
