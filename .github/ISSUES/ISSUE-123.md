---
title: "Implement Event Sourcing and Immutable Audit Log for All Scoring Decisions"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary

LedgerLens scoring decisions are stored as the current state of a `wallet_scores` row — overwriting the previous score on every recompute. Regulatory compliance requires a cryptographically tamper-evident, append-only audit trail of every scoring decision: what score was assigned, which feature values drove it, which model version was used, and who (or what automated process) triggered the recompute. An event-sourced audit log with Merkle-chain integrity satisfies this requirement.

## Background & Context

Financial intelligence units and compliance auditors require that risk scoring systems demonstrate:

1. **Non-repudiation**: it must be impossible to silently change a past score without evidence of tampering.
2. **Decision provenance**: every score must be traceable to an exact set of feature values and a specific model version — "why was this wallet scored 87 on 2024-03-15?" must be answerable from the audit log alone.
3. **Actor attribution**: whether a score was triggered by automated ingestion, analyst feedback, or a manual admin override must be recorded and non-modifiable.

The current `wallet_scores` table stores only the current score. Recomputes silently overwrite it. There is no way to reconstruct the scoring history or prove it has not been tampered with.

An event-sourced design introduces a `scoring_events` append-only table. Each row is a `ScoringEvent` containing the full feature vector snapshot, model version, actor, and a SHA-256 chain hash linking it to the preceding event for that wallet. The current score is always derivable by replaying events — it is never stored as mutable state.

## Objectives

- [ ] Implement `ScoringEvent` dataclass and `scoring_events` append-only SQLite table with a `chain_hash` column
- [ ] Implement `ScoringEventStore` with `append(event)` and `replay(wallet) → list[ScoringEvent]` methods
- [ ] Implement `ChainHashVerifier` that walks the event chain for a wallet and verifies each `chain_hash`
- [ ] Replace the `UPDATE wallet_scores SET score = ...` pattern in `model_inference.py` with `ScoringEventStore.append()`
- [ ] Expose `GET /audit/wallet/{wallet}` returning the full scoring event history in chronological order
- [ ] Expose `GET /audit/wallet/{wallet}/verify` returning chain integrity status (valid / tampered / hash at which tampering detected)
- [ ] Expose `GET /audit/summary` returning: events in last 24h, unique wallets scored, integrity violations detected
- [ ] Write tests covering: append, replay, chain verification, tampering detection

## Technical Requirements

### Event schema

```python
# audit/scoring_events.py

import hashlib, json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class ScoringEvent:
    event_id: str             # UUID v4
    wallet: str
    namespace_id: str
    score: int                # 0–100
    previous_score: Optional[int]
    feature_snapshot: dict    # full FEATURE_NAMES → value at time of scoring
    model_version: str        # from models/model_version.txt
    triggered_by: str         # "ingestion" | "manual_recompute" | "feedback_boost" | "admin_override"
    actor_id: Optional[str]   # API key ID or None for automated triggers
    chain_hash: str           # see compute_chain_hash()
    occurred_at: datetime = field(default_factory=datetime.utcnow)

    @staticmethod
    def compute_chain_hash(
        previous_chain_hash: Optional[str],
        event_id: str,
        wallet: str,
        score: int,
        feature_snapshot: dict,
        occurred_at: datetime,
    ) -> str:
        """
        SHA-256 over the canonical JSON of:
          {"prev": previous_chain_hash or "GENESIS",
           "event_id": event_id,
           "wallet": wallet,
           "score": score,
           "features": feature_snapshot,   # keys sorted
           "occurred_at": occurred_at.isoformat()}
        Returns hex digest.
        """
        payload = json.dumps({
            "prev": previous_chain_hash or "GENESIS",
            "event_id": event_id,
            "wallet": wallet,
            "score": score,
            "features": dict(sorted(feature_snapshot.items())),
            "occurred_at": occurred_at.isoformat(),
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()
```

### Scoring event store

```python
class ScoringEventStore:
    def __init__(self, db_path: str): ...

    async def append(self, event: ScoringEvent) -> None:
        """
        Insert ScoringEvent into scoring_events table.
        Table must have a CHECK constraint preventing UPDATE/DELETE:
          enforced at application layer — no UPDATE or DELETE is ever called.
        Compute chain_hash from the previous event's chain_hash for this wallet.
        """
        prev = await self._get_latest_event(event.wallet)
        event.chain_hash = ScoringEvent.compute_chain_hash(
            previous_chain_hash=prev.chain_hash if prev else None,
            event_id=event.event_id,
            wallet=event.wallet,
            score=event.score,
            feature_snapshot=event.feature_snapshot,
            occurred_at=event.occurred_at,
        )
        await self._insert(event)

    async def replay(self, wallet: str) -> list[ScoringEvent]:
        """Return all events for wallet in chronological order (oldest first)."""
        ...

    async def current_score(self, wallet: str) -> Optional[int]:
        """Return the score from the most recent event, or None if no events exist."""
        ...
```

### Database schema

```sql
CREATE TABLE IF NOT EXISTS scoring_events (
    event_id         TEXT PRIMARY KEY,
    wallet           TEXT NOT NULL,
    namespace_id     TEXT NOT NULL,
    score            INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    previous_score   INTEGER,
    feature_snapshot TEXT NOT NULL,   -- JSON blob
    model_version    TEXT NOT NULL,
    triggered_by     TEXT NOT NULL,
    actor_id         TEXT,
    chain_hash       TEXT NOT NULL UNIQUE,
    occurred_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Prevent accidental mutation — application enforces append-only semantics
-- but these triggers add a DB-level safeguard
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

### Chain hash verifier

```python
class ChainHashVerifier:
    def __init__(self, store: ScoringEventStore): ...

    async def verify(self, wallet: str) -> ChainVerificationResult:
        """
        Walk events in chronological order.
        Recompute each event's chain_hash from its fields + the previous hash.
        Return VALID if all match, TAMPERED with the first failing event_id otherwise.
        """
        ...

@dataclass
class ChainVerificationResult:
    wallet: str
    status: str                      # "valid" | "tampered" | "no_events"
    total_events: int
    first_tampered_event_id: Optional[str]
    verified_at: datetime
```

### API endpoints

```python
@router.get("/audit/wallet/{wallet}")
async def get_wallet_audit_log(
    wallet: str,
    limit: int = Query(100, le=1000),
    since: Optional[datetime] = Query(None),
) -> list[ScoringEventResponse]: ...

@router.get("/audit/wallet/{wallet}/verify")
async def verify_wallet_chain(wallet: str) -> ChainVerificationResult: ...

@router.get("/audit/summary")
async def get_audit_summary() -> AuditSummaryResponse: ...
```

### Configuration

```
AUDIT_LOG_ENABLED=true
AUDIT_FEATURE_SNAPSHOT_MAX_KEYS=50   # truncate snapshot if feature set expands beyond this
AUDIT_VERIFY_ON_READ=false           # if true, verify chain on every GET /audit call (expensive)
AUDIT_RETENTION_DAYS=2555            # 7 years — regulatory minimum
```

## Security Considerations

- **Append-only at DB level**: SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers raise `ABORT` if any modification is attempted. The application layer must also never call `UPDATE` or `DELETE` on this table. Tests must verify the triggers fire
- **Chain hash covers feature snapshot**: the feature snapshot is included in the chain hash computation — not just the score. This ensures that retroactively changing the recorded features (to obscure a model error) invalidates the chain
- **Genesis event**: the first event for a wallet uses `"GENESIS"` as the `previous_chain_hash`. Verify this sentinel is not injectable via the API (the `triggered_by` field is an enum, not a free string)
- **Actor attribution for manual overrides**: admin override events must always include a non-null `actor_id` (the API key ID that submitted the override). Reject admin override events with `actor_id=None` at the `ScoringEventStore.append` layer
- **Retention enforcement**: a nightly job must delete events older than `AUDIT_RETENTION_DAYS` only if explicitly configured. The default is 7 years; never auto-delete without explicit operator opt-in and secondary confirmation

## Testing Requirements

- [ ] `tests/test_scoring_events.py`
- [ ] Test: `ScoringEvent.compute_chain_hash` is deterministic — same inputs always produce the same hash
- [ ] Test: `ScoringEventStore.append` computes correct `chain_hash` by chaining to the previous event
- [ ] Test: `ScoringEventStore.replay` returns events in chronological order with no gaps
- [ ] Test: `ScoringEventStore.current_score` returns the score from the latest event
- [ ] Test: `ChainHashVerifier.verify` returns `VALID` for an unmodified chain
- [ ] Test: mutating any field of a stored event (simulated by direct SQL UPDATE bypassing the trigger) causes `ChainHashVerifier.verify` to return `TAMPERED` at the correct event
- [ ] Test: SQLite `BEFORE UPDATE` trigger raises `ABORT` when `UPDATE scoring_events` is attempted directly
- [ ] Test: `GET /audit/wallet/{wallet}` returns events in correct order with correct schema
- [ ] Test: `GET /audit/wallet/{wallet}/verify` returns `TAMPERED` after simulated chain break

## Documentation Requirements

- [ ] Docstrings on `ScoringEvent`, `ScoringEventStore`, `ChainHashVerifier`
- [ ] `docs/audit_log.md`: design rationale (append-only + chain hash), hash computation spec (canonical JSON sort order, GENESIS sentinel), regulatory retention requirements, how to verify a wallet's chain integrity, known limitations (SQLite triggers are advisory — a root-level DB edit bypasses them)
- [ ] Update `docs/database_schema.md` with `scoring_events` table schema and triggers
- [ ] Update `.env.example` with the four new configuration variables

## Definition of Done

- [ ] `ScoringEvent`, `ScoringEventStore`, `ChainHashVerifier` fully implemented
- [ ] `scoring_events` table with `BEFORE UPDATE` / `BEFORE DELETE` triggers in all migrations
- [ ] `model_inference.py` appends a `ScoringEvent` on every score computation (no more direct `UPDATE wallet_scores`)
- [ ] `GET /audit/wallet/{wallet}`, `/verify`, and `/summary` endpoints live
- [ ] Chain tampering detected by verifier test
- [ ] `docs/audit_log.md` authored
- [ ] All tests pass

## For Contributors

**Ideal contributor profile**: You have experience designing append-only / event-sourced data stores and understand the trade-offs between event sourcing and mutable state for compliance use cases. Familiarity with Merkle chains, content-addressed hashing, and tamper-evident log design is essential. Experience with SQLite triggers and Python `aiosqlite` is expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "event sourcing / CQRS", "tamper-evident audit logs", "compliance engineering"
2. **Relevant experience** — append-only systems you have built; regulatory audit log implementations; experience with Merkle or hash-chain designs in production
3. **Approach / initial thoughts** — your view on the chain hash design (what's missing, what would you strengthen); how you would handle the performance impact of replaying a wallet with 10,000+ events to get the current score; concerns about the SQLite trigger approach
4. **Estimated time** — breakdown by component (event dataclass, store, verifier, DB migration, inference integration, API, tests, docs)
