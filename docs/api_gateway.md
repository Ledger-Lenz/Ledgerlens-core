# API Gateway — Consolidated Auth, Quota, and Access Logging

## Motivation

Before this change, authentication, per-key scoping, and rate-limiting were
implemented **three times independently** in this codebase, with real drift
between the implementations:

| Module | Auth mechanism | Backing store | Hash algo | Key prefix |
|--------|---------------|---------------|-----------|------------|
| `api/auth.py` | Single admin key (`require_admin_key`) | Env var (`LEDGERLENS_ADMIN_API_KEY`) | `secrets.compare_digest` | n/a |
| `api/auth.py` | Scoped API key (`require_api_key_scope`) | `detection.storage` (imports `get_api_key_by_hash`) | BLAKE2b-256 | n/a |
| `api/api_key_router.py` | Scoped API key (`require_scope`) | `detection.api_key_store` (canonical) | BLAKE2b-256 | `ll_` |
| `api/api_keys_router.py` | Scoped API key (`require_scope`) | Its own `_ensure_table` / `CREATE TABLE api_keys` | SHA-256 | `secrets.token_urlsafe(32)` |
| `api/namespace.py` | Namespace key (`namespace_filter`) | Its own `_ensure_namespace_tables` / `CREATE TABLE api_keys` | SHA-256 | n/a |

The consequences:

- A key created via `api/api_keys_router.py`'s `POST /admin/api-keys` was not
  recognised by `api/api_key_router.py`'s `require_scope` dependency (different
  table, different hash algorithm).
- Per-key rate-limit state was tracked per-implementation rather than per-key
  globally (three independent in-memory `_rate_windows` dicts).
- There was no unified access log — each router logged (or didn't log)
  independently.
- Schema drift across the three `api_keys` tables (`api/api_keys_router.py`'s
  DDL at line 28, `api/namespace.py`'s DDL at line 27, and
  `detection/api_key_store.py`'s DDL at line 19) meant a migration could not
  simply copy rows.

## Architecture

```
┌──────────────┐     ┌─────────────────────────────┐     ┌──────────────┐
│   Client     │────▶│  GatewayMiddleware           │────▶│  Route       │
│              │     │  (api/gateway.py)            │     │  Handler     │
└──────────────┘     │                              │     └──────────────┘
                     │  1. Resolve auth (key/header) │
                     │  2. Check scope annotation    │
                     │  3. Enforce per-minute rate   │
                     │  4. Enforce daily quota       │
                     │  5. Log access record         │
                     └──────────────────────────────┘
```

The gateway is a single `BaseHTTPMiddleware` that runs on every request.
Routes declare their required scope via the `@scope_required("scope_name")`
decorator. The gateway resolves authentication once, checks quota, logs one
structured record, and either rejects the request or forwards it to the route
handler.

## Scope Annotations

Routes are annotated with their required scope using one of:

### Decorator (preferred)

```python
from api.gateway import scope_required

@router.get("/v1/scores/{wallet}")
@scope_required("read:scores")
async def get_wallet_scores(wallet: str):
    ...
```

### Route attribute (for programmatic use)

```python
from api.gateway import ScopedRoute

@router.get("/admin/models")
@ScopedRoute("admin")
async def list_models():
    ...
```

### Supported scopes

| Scope | Description |
|-------|-------------|
| `admin` | Full access — equivalent to `require_admin_key` |
| `compliance:read` | Regulatory compliance endpoints — equivalent to `require_compliance_key` |
| `read:scores` | Read risk scores and alerts |
| `write:suppressions` | Manage wallet allowlist/denylist |

A scope of `None` (no annotation) means the route is **public** — no auth
is checked, but access is still logged.

## Auth Resolution Order

The gateway checks authentication in this order (first match wins):

1. **`X-LedgerLens-Admin-Key`** header — compared against `LEDGERLENS_ADMIN_API_KEY`
2. **`X-LedgerLens-Compliance-Key`** header — compared against `LEDGERLENS_COMPLIANCE_API_KEY`
3. **`X-LedgerLens-Api-Key`** header — looked up in the canonical `api_keys` table
   via `detection.api_key_store.get_api_key_by_hash`

If none of these match, the gateway returns **401 Unauthorized**.

## Quota Model

The gateway enforces two tiers of quota:

| Tier | Granularity | Column | Check |
|------|-------------|--------|-------|
| Per-minute rate limit | Per key | `rate_limit_per_minute` | Sliding window, 60-second window |
| Daily request quota | Per key | `daily_quota` | Calendar-day counter via `gateway_request_log` |
| Daily namespace quota | Per namespace | `namespace_daily_quota` | Calendar-day counter via `gateway_request_log` |

- Keys with `daily_quota=0` or `namespace_daily_quota=0` have unlimited quota.
- Wildcard namespace (`namespace_id='*'`) keys are **exempt** from
  per-namespace quota but still subject to per-minute rate limits.
- When daily quota is exceeded, the response includes an
  `X-LedgerLens-Quota-Reset` header with the ISO-8601 datetime of the next
  quota reset (midnight UTC).
- When per-minute rate limit is exceeded, the response includes a `Retry-After`
  header with seconds until the window resets.

## Access Logging

Every request (public or authenticated) is logged to:

1. **`gateway_request_log` SQLite table** — structured records for analysis
2. **Structured log line** — via the `ledgerlens.gateway` logger

The log entry contains:
- `method` — HTTP method
- `path` — URL path (no query params)
- `status_code` — HTTP response status
- `latency_ms` — request duration in milliseconds
- `key_id` — first 8 characters of the key ID (truncated)
- `namespace` — namespace ID, or `-` for unauthenticated requests
- `scope` — required scope, or `"public"`

**Request and response bodies are NEVER logged.** The setting `GATEWAY_LOG_BODY`
defaults to `false` and exists only as a kill switch; it must never be enabled
in production.

## Configuration

| Environment variable | Default | Description |
|---------------------|---------|-------------|
| `GATEWAY_ENABLED` | `true` | Set to `false` to disable the gateway and fall back to per-route Depends |
| `GATEWAY_DEFAULT_DAILY_QUOTA` | `100000` | Default daily request quota for new API keys (0 = unlimited) |
| `GATEWAY_DEFAULT_NAMESPACE_DAILY_QUOTA` | `0` | Default per-namespace daily quota (0 = unlimited) |
| `GATEWAY_QUOTA_STORE` | `sqlite` | `sqlite` (single-process) or `redis` (multi-process) |
| `GATEWAY_LOG_BODY` | `false` | **Never set to true in production** |

## Migration Guide

### For consumers of `api/api_keys_router.py`

The duplicate `api/api_keys_router.py` router is **deprecated** and will be
removed after 31 Jan 2027.

**What changed:**
- `POST /admin/api-keys` now creates keys in the canonical store
  (`detection.api_key_store`) using BLAKE2b-256 hashing instead of SHA-256.
- The deprecated router still works and creates keys in *both* stores during
  the transition, but it emits a `Deprecation` HTTP header (RFC 8594) on every
  response.

**Migration steps:**
1. Run `python cli.py db-migrate` to consolidate any existing keys from the
   legacy schema into the canonical store.
2. Update your client to use the canonical `X-LedgerLens-Api-Key` header (not
   the deprecated `X-Api-Key` header).
3. Verify that keys created via the deprecated router's endpoint are recognised
   by the canonical store after migration.
4. Switch to using `POST /admin/api-keys` from `api/api_key_router.py`.

### For operators of deployed instances

1. Upgrade to the version of `ledgerlens-core` that includes this gateway.
2. Run `python cli.py db-migrate` — this applies schema migration 16 and
   consolidates any existing legacy API keys.
3. Verify that existing API keys still work with the gateway.
4. Review the `gateway_request_log` table for traffic patterns.

### Rollback

Set `GATEWAY_ENABLED=false` to bypass the gateway entirely and use the
original per-route `Depends(require_admin_key)` / `Depends(require_scope(...))`
mechanism. No data migration is required for rollback.

## Envoy / Kong Evaluation

For a single-process FastAPI deployment (the current topology), the gateway
middleware is the right solution — it adds zero operational overhead, shares
the process's configuration and connection pool, and requires no additional
infrastructure.

If `ledgerlens-api` splits into multiple backend services in the future,
the natural next step would be to extract the gateway into a sidecar process
or edge proxy:

| Proxy | Pros | Cons |
|-------|------|------|
| **Envoy** | Rich rate-limiting, observability, mTLS | Complex config (xDS), operational overhead |
| **Kong** | Plugin ecosystem, DB-backed config | Requires PostgreSQL or Cassandra |

Until that topology change, the FastAPI middleware is both sufficient and
strictly simpler than either external proxy.

## Security Considerations

- **Consolidation migration never regenerates or exposes plaintext keys.**
  It operates on stored hashes only. Any key whose hash cannot be unambiguously
  mapped to the canonical schema is flagged in a migration report for manual
  review — not silently dropped or silently kept active.
- `GATEWAY_LOG_BODY=false` is the **only supported setting**. Enabling body
  logging would expose wallet addresses and SHAP payloads in the access log.
- During the deprecation window, `api/api_keys_router.py`'s endpoints return a
  `Deprecation` HTTP header (RFC 8594) rather than being silently removed,
  giving integrators notice.
- The gateway **fails closed**: if the quota/auth backend (SQLite or Redis) is
  unreachable, requests to scoped routes are rejected with **503**, not allowed
  through unauthenticated. Public routes (e.g., `/health`) still succeed.
- Wildcard namespace (`namespace_id='*'`) admin keys are exempted from
  per-namespace quota but still subject to the global per-minute rate limit,
  preventing an admin key from becoming an accidental DoS bypass.
