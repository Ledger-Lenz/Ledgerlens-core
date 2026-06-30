# Event Sourcing Audit Log  (Issue #297)

## Design Rationale

Financial intelligence units require that risk scoring systems demonstrate:

1. **Non-repudiation** — it must be impossible to silently change a past score.
2. **Decision provenance** — every score must be traceable to exact feature values and model version.
3. **Actor attribution** — whether a score was triggered by automated ingestion, analyst feedback, or admin override must be recorded immutably.

LedgerLens solves this with an event-sourced design: every scoring decision is
appended to `scoring_events` as a `ScoringEvent`. The current score is always
derivable by replaying events — it is never stored as mutable state.

## Chain Hash Specification

Each event's `chain_hash` is a tamper-evident link to the preceding event:

```python
SHA-256(canonical_json({
    "prev": previous_chain_hash | "GENESIS",
    "event_id": event_id,
    "wallet": wallet,
    "score": score,
    "features": {sorted(feature_snapshot.items())},
    "occurred_at": occurred_at.isoformat()
}))
```

**Canonical JSON rules:**
- Keys are sorted alphabetically at every nesting level.
- No whitespace between tokens (`separators=(",", ":")`).
- UTF-8 encoded before hashing.

**GENESIS sentinel:** The first event for a wallet uses `"GENESIS"` as the
`prev` value. This sentinel cannot be injected via the API because
`triggered_by` is validated against a fixed enum, and `chain_hash` is
computed server-side only.

## Database Schema

```sql
CREATE TABLE scoring_events (
    event_id         TEXT PRIMARY KEY,
    wallet           TEXT NOT NULL,
    namespace_id     TEXT NOT NULL,
    score            INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    previous_score   INTEGER,
    feature_snapshot TEXT NOT NULL,   -- JSON blob (keys sorted)
    model_version    TEXT NOT NULL,
    triggered_by     TEXT NOT NULL,   -- enum validated at application layer
    actor_id         TEXT,
    chain_hash       TEXT NOT NULL UNIQUE,
    occurred_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- DB-level safeguards (application enforces append-only semantics too)
CREATE TRIGGER prevent_scoring_event_update
BEFORE UPDATE ON scoring_events
BEGIN
    SELECT RAISE(ABORT, 'scoring_events is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER prevent_scoring_event_delete
BEFORE DELETE ON scoring_events
BEGIN
    SELECT RAISE(ABORT, 'scoring_events is append-only: DELETE is not permitted');
END;
```

## Verifying a Wallet's Chain Integrity

Using the API:

```bash
curl -H "X-LedgerLens-Admin-Key: $ADMIN_KEY" \
  "http://localhost:8000/audit/wallet/GABCD...XYZ/verify"
```

Response (tamper-free):
```json
{
  "wallet": "GABCD...XYZ",
  "status": "valid",
  "total_events": 42,
  "first_tampered_event_id": null,
  "verified_at": "2026-06-30T07:00:00+00:00"
}
```

Response (tampered):
```json
{
  "wallet": "GABCD...XYZ",
  "status": "tampered",
  "total_events": 42,
  "first_tampered_event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "verified_at": "2026-06-30T07:00:00+00:00"
}
```

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/audit/wallet/{wallet}` | Admin key | Full event history (oldest first) |
| `GET` | `/audit/wallet/{wallet}/verify` | Admin key | Chain integrity verification |
| `GET` | `/audit/summary` | Admin key | 24h event count, unique wallets |

## Regulatory Retention

Default retention is **2555 days (7 years)** — the FATF anti-money laundering
minimum. Events are **never auto-deleted** without explicit operator opt-in.
Set `AUDIT_RETENTION_DAYS` and run the retention job manually when reducing the
retention period.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `AUDIT_LOG_ENABLED` | `true` | Enable the audit log |
| `AUDIT_FEATURE_SNAPSHOT_MAX_KEYS` | `50` | Maximum feature snapshot keys per event |
| `AUDIT_VERIFY_ON_READ` | `false` | Verify chain on every GET /audit call |
| `AUDIT_RETENTION_DAYS` | `2555` | Minimum retention (7 years) |

## Known Limitations

- **SQLite triggers are advisory**: a root-level database edit with a SQLite
  client bypasses the triggers. For production deployments, restrict filesystem
  access to the SQLite file to the LedgerLens service account only.
- **Replay performance**: replaying a wallet with 10,000+ events incurs a
  sequential table scan. The `idx_se_wallet` index on `(wallet, occurred_at)`
  makes this efficient for per-wallet queries. For full-table verification,
  use the `/verify` endpoint which streams lazily.
