---
title: "Implement Multi-Tenant Namespace Isolation with Row-Level Security and Cross-Namespace Federation"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary

LedgerLens namespaces share a single SQLite database with no enforced row-level isolation — a misconfigured query can read another tenant's scores, alerts, and suppression lists. Complete namespace isolation with SQLite-layer enforcement, API middleware scoping, and an opt-in cross-namespace federation API (for compliance networks where multiple institutions share ring-member signals) is required before LedgerLens can be offered as a multi-tenant SaaS platform.

## Background & Context

The current multi-tenancy model is application-layer only: every query includes a `WHERE namespace_id = ?` clause that is added by the developer and enforced by convention. Three failure modes are known:

1. **Missing WHERE clause**: a new feature endpoint omits the namespace filter. All tenants' data is exposed.
2. **Cross-namespace JOIN**: a developer writes `SELECT * FROM wallet_scores ws JOIN ring_members rm ON ws.wallet = rm.wallet` without including `namespace_id` in the JOIN condition. Ring membership data leaks across tenants.
3. **Bulk operations**: the nightly recompute job runs `UPDATE wallet_scores SET ... WHERE recompute_needed = 1` — no namespace filter. All tenants are recomputed even if only one tenant triggered the job.

The solution is a two-layer enforcement model:

- **DB layer**: SQLite views with embedded `namespace_id = current_setting('app.namespace_id')` filters. All application code queries these views, not the base tables. Direct base table reads are disallowed by an application-layer assertion.
- **API layer**: a FastAPI dependency `require_namespace()` that reads the namespace from the validated API key and sets `current_setting('app.namespace_id')` on the SQLite connection before any query executes.

Additionally, a federation API allows two namespaces with an explicit mutual agreement to share ring-member signals — useful for consortium compliance networks — without sharing scores or features.

## Objectives

- [ ] Create namespaced SQLite views for `wallet_scores`, `alerts`, `ring_members`, `suppression_list`, and `analyst_feedback`
- [ ] Implement `NamespaceContext` — a FastAPI dependency that sets `app.namespace_id` on the DB connection for the request lifetime
- [ ] Add an assertion in `db.py` that rejects any raw query against base tables (enforced by checking the SQL string against a blocklist of base table names)
- [ ] Implement `NamespaceFederationAgreement` table and `POST /federation/agreements` endpoint allowing two namespace admins to create a mutual signal-sharing agreement
- [ ] Implement `GET /federation/ring-signals` returning ring-member wallets shared by federated namespaces (wallet address + shared confidence score only — no features, scores, or analyst notes)
- [ ] Implement `GET /admin/namespaces` returning namespace metadata: namespace_id, created_at, api_key_count, wallet_count, last_activity_at
- [ ] Write tests for: namespace isolation (cross-tenant query blocked), federation agreement creation, federation signal exposure limits, `NamespaceContext` injection

## Technical Requirements

### Namespaced views

```sql
-- migrations/007_namespace_views.sql

-- Each view embeds the namespace filter. Application code only queries these views.
CREATE VIEW IF NOT EXISTS v_wallet_scores AS
SELECT * FROM wallet_scores
WHERE namespace_id = (SELECT value FROM pragma_namespace WHERE name = 'app.namespace_id');

CREATE VIEW IF NOT EXISTS v_alerts AS
SELECT * FROM alerts
WHERE namespace_id = (SELECT value FROM pragma_namespace WHERE name = 'app.namespace_id');

-- (repeat for ring_members, suppression_list, analyst_feedback)

-- SQLite does not support session variables natively. Use a single-row config table
-- per connection (set at connection open time, cleared at connection close).
CREATE TABLE IF NOT EXISTS _session_config (key TEXT PRIMARY KEY, value TEXT);
```

### Namespace context dependency

```python
# api/dependencies.py

import re
from fastapi import Depends, HTTPException, Request
from contextlib import asynccontextmanager

NAMESPACE_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{2,63}$')

class NamespaceContext:
    """
    FastAPI dependency that:
    1. Extracts namespace_id from the validated API key (set by AuthMiddleware).
    2. Validates namespace_id against NAMESPACE_ID_PATTERN (rejects injection attempts).
    3. Sets `INSERT OR REPLACE INTO _session_config VALUES ('app.namespace_id', ?)` on the
       current DB connection before the request handler runs.
    4. Clears _session_config on response teardown (even on exception).
    """
    async def __call__(self, request: Request) -> str:
        namespace_id = request.state.namespace_id   # set by AuthMiddleware
        if not NAMESPACE_ID_PATTERN.match(namespace_id):
            raise HTTPException(status_code=400, detail="Invalid namespace_id format")
        db = request.state.db
        await db.execute(
            "INSERT OR REPLACE INTO _session_config VALUES ('app.namespace_id', ?)",
            (namespace_id,)
        )
        try:
            yield namespace_id
        finally:
            await db.execute("DELETE FROM _session_config WHERE key = 'app.namespace_id'")
```

### Base table query blocklist

```python
# db.py — assertion wrapper around aiosqlite execute()

import re

BASE_TABLES = frozenset({
    "wallet_scores", "alerts", "ring_members", "suppression_list",
    "analyst_feedback", "scoring_events",
})

# Pattern: FROM/JOIN followed by a base table name (not prefixed with v_)
_BASE_TABLE_PATTERN = re.compile(
    r'\b(?:FROM|JOIN)\s+(' + '|'.join(BASE_TABLES) + r')\b',
    re.IGNORECASE
)

async def safe_execute(db, sql: str, params=()) -> Any:
    """
    Raise AssertionError if `sql` references a base table directly.
    All application queries must use v_ views or non-tenant tables (e.g. api_keys).
    """
    if _BASE_TABLE_PATTERN.search(sql):
        raise AssertionError(
            f"Direct base table query blocked by namespace isolation policy. "
            f"Use the corresponding v_ view. SQL: {sql[:200]}"
        )
    return await db.execute(sql, params)
```

### Federation schema

```sql
CREATE TABLE IF NOT EXISTS namespace_federation_agreements (
    agreement_id    TEXT PRIMARY KEY,
    initiator_ns    TEXT NOT NULL,
    acceptor_ns     TEXT NOT NULL,
    agreed_at       TIMESTAMP,
    initiator_accepted_at TIMESTAMP,
    acceptor_accepted_at  TIMESTAMP,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | active | revoked
    signal_types    TEXT NOT NULL DEFAULT '["ring_member"]',  -- JSON array
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (initiator_ns, acceptor_ns)
);
```

### Federation API

```python
@router.post("/federation/agreements")
async def create_federation_agreement(
    body: FederationAgreementRequest,
    namespace_id: str = Depends(NamespaceContext()),
) -> FederationAgreementResponse:
    """
    Create a pending agreement. Both namespace admins must call this endpoint
    (or POST /federation/agreements/{agreement_id}/accept) to activate.
    One-sided agreement returns status='pending'.
    """
    ...

@router.get("/federation/ring-signals")
async def get_federated_ring_signals(
    namespace_id: str = Depends(NamespaceContext()),
    min_confidence: float = Query(0.7, ge=0.0, le=1.0),
) -> list[FederatedRingSignal]:
    """
    Return ring-member wallets shared by all active federation partners.
    Returns: wallet, shared_confidence, source_namespace_count.
    Does NOT return: scores, features, analyst notes, namespace identities of sources.
    """
    ...

@dataclass
class FederatedRingSignal:
    wallet: str
    shared_confidence: float   # mean of all partners' ring_membership_score for this wallet
    source_count: int          # how many partners flagged this wallet (not which ones)
```

### Configuration

```
NAMESPACE_ISOLATION_ENABLED=true
NAMESPACE_ISOLATION_ASSERT_ON_BASE_TABLE=true   # fail hard in dev; log in prod
FEDERATION_MAX_PARTNERS_PER_NAMESPACE=10
FEDERATION_MIN_CONFIDENCE_FLOOR=0.5   # federation API enforces this floor regardless of caller query param
```

## Security Considerations

- **Namespace injection via SQL**: `NAMESPACE_ID_PATTERN` blocks characters that could be used to escape the `_session_config` value into a SQL injection. The regex `^[a-z0-9][a-z0-9_-]{2,63}$` must be enforced at both the API layer and the DB write layer
- **Federation signal anonymity**: `GET /federation/ring-signals` must never reveal which specific partner namespace contributed a signal. Return only `source_count` (number of partners) and `shared_confidence` (mean score). Individual namespace identities are never exposed
- **Federation agreement mutual consent**: a federation agreement is only `active` when both namespaces have accepted it. One-sided agreements in `pending` state share no data. The `agreed_at` timestamp is set only when both parties have accepted
- **Base table assertion in production**: `NAMESPACE_ISOLATION_ASSERT_ON_BASE_TABLE=true` raises in development; production should log and alert rather than raise, to avoid a bug in the blocklist pattern crashing production. The `safe_execute` wrapper must support both modes
- **Session config cleanup on exception**: the `NamespaceContext` dependency's `finally` block must clear `_session_config` even when the request handler raises an unhandled exception, to prevent namespace context leaking to a subsequent request on the same connection from the pool

## Testing Requirements

- [ ] `tests/test_namespace_isolation.py`
- [ ] Test: querying `v_wallet_scores` with `app.namespace_id=ns_a` returns only `ns_a` rows even when `ns_b` rows exist in the base table
- [ ] Test: `safe_execute` with a raw `SELECT * FROM wallet_scores` raises `AssertionError`
- [ ] Test: `safe_execute` with `SELECT * FROM v_wallet_scores` succeeds
- [ ] Test: `NamespaceContext.__call__` sets `_session_config` and clears it after the request (including on exception)
- [ ] Test: invalid `namespace_id` format (e.g. containing `'` or `--`) rejected by `NAMESPACE_ID_PATTERN` with 400
- [ ] Test: federation agreement requires mutual acceptance before status becomes `active`
- [ ] Test: `GET /federation/ring-signals` with a pending (not yet active) agreement returns an empty list
- [ ] Test: `GET /federation/ring-signals` does not reveal which partner namespace contributed a signal
- [ ] Test: `FEDERATION_MIN_CONFIDENCE_FLOOR` cannot be bypassed by the caller via query param

## Documentation Requirements

- [ ] Docstrings on `NamespaceContext`, `safe_execute`, `FederatedRingSignal`
- [ ] `docs/multi_tenancy.md`: isolation model (views + session config), base table blocklist rationale, federation agreement lifecycle (pending → active → revoked), signal anonymity guarantees, known limitations (SQLite WAL mode and connection pool implications — each connection needs its own session config)
- [ ] `docs/federation_api.md`: federation use case (consortium compliance networks), API walkthrough (create agreement, accept, query signals), privacy guarantees, revocation
- [ ] Update `docs/database_schema.md` with new views and federation table
- [ ] Update `.env.example` with the four new configuration variables

## Definition of Done

- [ ] Namespaced views for all five tenant tables created in migration
- [ ] `NamespaceContext` dependency injected into all tenant-data endpoints
- [ ] `safe_execute` blocking raw base table queries deployed in `db.py`
- [ ] Federation agreement CRUD and `GET /federation/ring-signals` endpoints live
- [ ] Cross-namespace query isolation verified by test
- [ ] Federation signal anonymity verified by test
- [ ] `docs/multi_tenancy.md` and `docs/federation_api.md` authored

## For Contributors

**Ideal contributor profile**: You have experience designing and implementing multi-tenant data isolation in relational databases — particularly row-level security patterns at the application and database layers. Familiarity with SQLite views, session variables, and connection pool isolation is required. Experience with privacy-preserving federated systems (e.g., privacy-preserving analytics, differential privacy, or secure multi-party computation) is a strong advantage for the federation component.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "multi-tenant SaaS isolation", "database row-level security", "privacy-preserving federated systems"
2. **Relevant experience** — multi-tenant isolation systems you have built; SQLite or PostgreSQL row-level security implementations; any federated data-sharing system design
3. **Approach / initial thoughts** — your assessment of the SQLite session config approach (vs PostgreSQL's `SET LOCAL`); how you would handle the connection pool isolation problem; concerns about the base table blocklist regex approach
4. **Estimated time** — breakdown by component (views/migration, namespace context, safe_execute, federation schema, federation API, tests, docs)
