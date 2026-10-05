# Unified API Policy Model

Every protocol LedgerLens exposes — REST, GraphQL, gRPC and WebSocket — enforces
authentication, scope checks and rate limits through **one** function:
`api.policy.enforce()`. No protocol handler may implement its own auth or
rate-limit logic; doing so is how policy-bypass bugs appear (one surface ends
up weaker than the others).

## The decision

```python
from api import policy

decision = policy.enforce(
    required_scope,            # e.g. "read:scores", "admin"; None = public
    admin_key=...,             # raw credential strings from the transport
    api_key=...,
    compliance_key=...,
)
decision.status   # policy.OK | UNAUTHENTICATED | FORBIDDEN | RATE_LIMITED
decision.key_meta # resolved key record (tier, namespace, scopes, ...)
decision.headers  # Retry-After / X-LedgerLens-Quota-Reset on RATE_LIMITED
```

`enforce()` runs, in order:

1. **Credential resolution** (`resolve_credentials`) — admin key, then
   compliance key, then a scoped API key from the canonical `api_keys` store.
2. **Scope check** (`check_scope`) — the `admin` scope grants every scope.
3. **Quota** (`check_quota`) — per-key per-minute limit (distributed, Redis
   backed), then daily / monthly per-key and per-namespace quotas.

The per-key budget is shared: a request on any protocol spends the same
counter, so switching protocols never buys extra quota.

## Protocol mapping

Each surface only translates the decision into its wire format:

| Status            | REST | GraphQL                 | gRPC                 | WebSocket     |
|-------------------|------|-------------------------|----------------------|---------------|
| `UNAUTHENTICATED` | 401  | `Unauthorized: ...`     | `UNAUTHENTICATED`    | close 1008    |
| `FORBIDDEN`       | 403  | `Forbidden: ...`        | `PERMISSION_DENIED`  | close 1008    |
| `RATE_LIMITED`    | 429  | `Rate limit exceeded`   | `RESOURCE_EXHAUSTED` | close 1008    |

| Surface   | Entry point                                   | Credentials read from                                   |
|-----------|-----------------------------------------------|---------------------------------------------------------|
| REST      | `api.gateway.GatewayMiddleware`               | `X-LedgerLens-{Admin,Api,Compliance}-Key` headers        |
| GraphQL   | `api.graphql_schema._enforce` (per field)     | `X-LedgerLens-{Admin,Api}-Key` headers                   |
| gRPC      | `api.grpc_scoring_service._authenticate`      | `x-ledgerlens-{admin,api}-key` metadata                  |
| WebSocket | `api.ws_router.ws_alerts` (scope `admin`)     | `api_key` query parameter                                |

GraphQL caches the decision per request and scope, so a query that touches
several protected fields is charged once, not once per field.

## Tiers

Every API key has a `tier` (`free` by default; `standard`, `enterprise`).
Unauthenticated traffic is `anonymous`; admin / compliance keys are `admin`
(unlimited). A tier sets:

- `requests_per_minute` — WAF rate limit (`api/waf_middleware.py`). Keyed by
  API key when one is presented, by client IP otherwise.
- `graphql_max_cost` — GraphQL query-cost budget (`api/graphql_cost.py`).

`0` means unlimited. Defaults live in `policy.DEFAULT_TIER_LIMITS`. Limits can
be changed **without a redeploy**:

- point `LEDGERLENS_TIER_LIMITS_FILE` at a JSON file — it is re-read whenever
  its mtime changes, and an invalid file keeps the last good values:

  ```json
  {"free": {"requests_per_minute": 60, "graphql_max_cost": 250}}
  ```

- or call `policy.set_tier_limits({...})` in-process; this takes precedence
  over the file.

The WAF limiter can be disabled with `LEDGERLENS_WAF_TIER_RATE_LIMIT_ENABLED=false`.

## GraphQL query cost

Queries are costed statically before execution: every field costs 1, and a
list-returning field multiplies its sub-selection's cost by 10. Queries deeper
than 8 levels are rejected for every tier (`QUERY_TOO_DEEP`); queries over the
tier budget are rejected with `QUERY_COST_EXCEEDED` (the error extensions
carry `cost` and `budget`). No resolver runs for a rejected query.

## Security alerts

`policy.emit_security_alert(event, **details)` logs at CRITICAL on the
`ledgerlens.security` logger and calls every hook in
`policy.SECURITY_ALERT_HOOKS` (register PagerDuty / Slack / SIEM forwarders
there). Current events:

- `api_key_cycling` — one /24 (IPv4) or /48 (IPv6) range presented at least
  `LEDGERLENS_WAF_KEY_CYCLING_THRESHOLD` distinct API keys within a minute.
- `refresh_token_reuse` — a rotated-out refresh token was presented; its
  whole token family has been revoked (see `api.auth.TokenService`).

## Adding a new protocol or endpoint

1. Extract the raw credentials from the transport.
2. Call `policy.enforce(required_scope, admin_key=..., api_key=..., compliance_key=...)`.
3. Map `decision.status` to the transport's error using the table above.
4. Add an adapter for the new surface to `tests/test_policy_parity.py`; the
   parity tests then assert that it makes the same decisions as the others.

Never compare keys, parse scopes or call the rate limiter directly from a
protocol handler.
