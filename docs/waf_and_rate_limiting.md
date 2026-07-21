# WAF and Rate Limiting

This document describes the Web Application Firewall (WAF) and the distributed
per-API-key rate limiter that protect the LedgerLens API.

## Overview

The implementation includes two main components:
1. **WAF Middleware**: Protects against common web attacks (SQLi, XSS, oversized bodies, slowloris)
2. **Distributed Rate Limiting**: Enforces per-key request-per-minute limits against a
   single, shared source of truth — correct across horizontally-scaled REST
   replicas and the separate gRPC process (see "Distributed Rate Limiting" below)

Both components are designed to be lightweight, configurable, and fail-open to avoid self-inflicted DoS.

## WAF Middleware

The WAF middleware is implemented in `api/waf_middleware.py` and provides the following protections:

### Features
- **SQL Injection (SQLi) Protection**: Blocks requests with known SQLi patterns in query parameters and JSON bodies
- **Cross-Site Scripting (XSS) Protection**: Blocks requests with known XSS patterns in query parameters and JSON bodies
- **Oversized Body Protection**: Rejects requests with bodies larger than `WAF_MAX_BODY_BYTES`
- **Slowloris Protection**: Times out slow requests (taking longer than `WAF_SLOW_REQUEST_TIMEOUT_SECONDS` to send the body)
- **Fail-Open Design**: If the WAF encounters an internal error, it logs the error and allows the request to proceed
- **Wallet Address Masking**: Blocked request logs have wallet addresses masked to comply with privacy requirements

### Configuration
The WAF is configurable via these environment variables (defined in `config/settings.py` and `.env.example`):
- `WAF_ENABLED`: Enable/disable the WAF (default: true)
- `WAF_MAX_BODY_BYTES`: Maximum allowed request body size in bytes (default: 1048576 / 1MB)
- `WAF_SLOW_REQUEST_TIMEOUT_SECONDS`: Timeout for slow requests in seconds (default: 10)

### Usage
The WAF middleware is automatically added to the FastAPI application in `api/main.py` and runs before other middleware.

### Endpoints
- `GET /admin/waf/blocked-requests`: Returns recent requests blocked by the WAF (admin-key gated)

## Distributed Rate Limiting

### The problem this replaces

Before this fix, "per-API-key rate limiting" was actually **three independent,
non-communicating in-process counters**:

| Enforcement path | Counter | Reachable from a real request? |
|---|---|---|
| `GatewayMiddleware._check_quota` (`api/gateway.py`) | its own `_rate_windows` dict | No — no route in `api/main.py` carries the `@scope_required`/`@ScopedRoute` annotation `ann()` requires, so this check never fired on any live route (dead-but-not-obviously-so; the *module* worked, it was just never reached). Fixed regardless, since it's the long-term intended mechanism per `docs/api_gateway.md` and any newly-annotated route must inherit the distributed fix automatically. |
| `api/api_key_router.py`'s `require_scope` dependency (the *actual* live REST enforcement on e.g. `GET /v1/scores/{wallet}`) | `detection.api_key_store`'s `_rate_windows` dict | Yes |
| `api/grpc_scoring_service.py`'s `_authenticate` | the *same* `detection.api_key_store` dict, but in a **separate OS process** (`cli.py serve-grpc`, not the `uvicorn api.main:app` process) | Yes |

Two compounding problems fell out of this:

1. **REST and gRPC don't share a process**, so even though both called the
   same Python function name, each ran against its own copy of the
   module-level dict — a client alternating between REST and gRPC got up to
   ~2x its configured budget in a single logical deployment.
2. **None of the in-process dicts survive horizontal scaling.** The
   project's own documented topology (`helm/ledgerlens/values.yaml`:
   `replicaCount: 2`, autoscaling to `maxReplicas: 10`,
   `docs/kubernetes_deployment.md`) runs multiple independent REST pods.
   Each pod's dict is invisible to every other pod, so the real ceiling for
   one key was `configured_limit x N_processes_touching_that_key` — up to
   `limit x 20` (10 REST replicas + gRPC) with no cross-replica visibility
   and no alerting, since each replica individually believed it was
   correctly enforcing the limit.

Separately, `api/adaptive_rate_limiter.py` ("tighten a key's limit after
repeated 4xx/WAF-block signals") was **removed**, not fixed. It was
unreachable from any real request (its only caller, `api/auth.py`'s
`require_api_key_scope`, was itself dead code — never imported by any
router) and was independently broken: it referenced three functions
(`_check_rate_limit_redis`, `_check_rate_limit_local`, `_rate_check`) that
were never defined anywhere, so invoking it would have raised `NameError`.
Rewiring it into the real request path would have meant standing up a
second, parallel, per-process abuse-signal counter with the exact same
distributed-state problem this fix solves for the primary limiter — and its
original purpose (tightening a key after it racks up 4xx responses) is
largely subsumed once the primary limiter itself correctly and strictly
enforces the configured limit across every replica and protocol. Removal
was the simpler, more maintainable choice; see `api/auth.py`'s module
docstring for the same rationale in code.

### Design: Redis-backed sliding-window counter

Implemented in `detection/rate_limiter.py`, and consumed by every enforcement
path through `detection.api_key_store.check_rate_limit(key_id, limit)` — the
single, canonical, shared function called by `api/gateway.py`,
`api/api_key_router.py`, and `api/grpc_scoring_service.py` alike. There is
exactly one enforcement primitive now, not three.

**Algorithm**: sliding-window counter (two fixed 60s buckets — current and
previous — combined with a linear time-weight), executed as a single atomic
Redis Lua script (`EVAL`) per check: one round trip, one `GET`+`GET`+`INCR`
executed atomically so there is no INCR/EXPIRE race window. This was chosen
over the alternatives:

- **Sliding window log** (a Redis ZSET holding every request timestamp) is
  exactly accurate but costs O(limit) memory per key and an O(log N)
  operation touching potentially thousands of members for a high-limit key —
  disproportionate cost for an abuse-prevention control (this is not a
  billing-grade metering system; daily/monthly quotas already get exact
  accounting via SQLite `COUNT` queries).
- **Token bucket** gives equivalent guarantees but needs fractional token +
  last-refill-timestamp state and isn't meaningfully simpler or cheaper in
  Lua than the counter approach.
- **Sliding window counter** (chosen): O(1) memory (two integers + TTL) and
  exactly one round trip per check. Worst-case overshoot is mathematically
  bounded at 2x the configured limit — a full burst at the very end of one
  window immediately followed by a full burst at the very start of the
  next; any traffic not adversarially timed to that exact boundary sees much
  tighter overshoot. This is the standard approach documented in
  Cloudflare's and Stripe's public rate-limiting engineering writeups and is
  an appropriate accuracy/cost trade for a per-minute abuse control.

Keys are hash-tagged (`ll:ratelimit:{key_id}:<bucket>`) so a Redis Cluster
deployment routes both keys of one check to the same slot — required for the
multi-key Lua script to work under Redis Cluster.

### Failure mode: deliberate fail-open with a bounded, observable degradation

Unlike `GatewayMiddleware`'s auth/quota-backend-unreachable path (503, fails
*closed*, because an unreachable backend there means the caller's identity
cannot be verified at all), an unreachable Redis here degrades — it does not
block traffic. Rate limiting is a cost/abuse control, not an authentication
boundary: taking the whole API down because the shared rate-limit store is
briefly unavailable would be a worse, self-inflicted denial of service than
a temporarily-generous limit. This matches this codebase's existing WAF and
`FeatureStore` precedent (both fail open).

The fallback is not silent, though:
- A `utils.circuit_breaker.CircuitBreaker` guards every Redis call. After 3
  consecutive failures it opens (stops attempting Redis for 15s, then
  probes once) so a Redis outage degrades to per-process-only enforcement —
  *exactly* today's pre-fix behavior, never worse — rather than adding
  latency or failing requests.
- Every open/close transition is logged at `WARNING`/`INFO`
  (`ledgerlens.rate_limiter` logger).
- Every check served from the fallback increments
  `ledgerlens_rate_limiter_fallback_total` (Prometheus counter). Sustained
  non-zero values mean cross-replica/cross-protocol enforcement is currently
  degraded to per-process-only — this is the metric to alert on.

### Configuration

`GATEWAY_QUOTA_STORE` (also drives the daily/monthly SQLite quota checks,
unchanged by this fix):
- `redis` (**default**): use the shared, distributed limiter — required for
  correct enforcement under this project's documented multi-replica
  topology and the REST/gRPC process split. Falls back to a local
  in-process window (logged + metered) if Redis is unreachable.
- `sqlite`: explicit opt-out — always use the local in-process window, never
  attempt Redis. Only correct for a genuine single-process deployment (e.g.
  local dev); the name is kept for backward compatibility with the
  pre-existing setting.

`REDIS_URL` is the same setting already used by `detection/feature_store.py`
— no second, independently-configured Redis client is introduced.

### Not in scope

- **Daily/monthly quotas** (`detection.api_key_store.check_daily_quota` /
  `check_monthly_quota`) still count rows in the `gateway_request_log`
  SQLite table. Under this project's Helm chart, that table lives on a
  `ReadWriteOnce` PVC, which does not aggregate across multiple replica
  pods either — a real, separate multi-replica correctness gap, but one
  that requires a different fix (a shared durable counter store or moving
  this accounting off SQLite entirely), not the per-minute enforcement
  mechanism this fix addresses. Flagged here as a known limitation, not
  silently left unaddressed.
- `api/api_keys_router.py` (the fully-deprecated legacy router, sunset
  2027-01-31) keeps its own independent, SHA-256-keyed, in-process-only
  rate limiter untouched — it is a structurally separate key namespace
  (different hash algorithm, different table population) already scheduled
  for removal, not one of the three counters this issue was scoped against.

## Metrics

Prometheus metrics exposed (defined in `api/metrics.py`):
- `ledgerlens_waf_blocks_total{rule, namespace_id}`: Total number of requests blocked by the WAF, grouped by rule and namespace
- `ledgerlens_rate_limiter_checks_total{backend}`: Total per-key rate-limit checks, split by `backend="redis"` (shared) vs `backend="local"` (degraded fallback)
- `ledgerlens_rate_limiter_fallback_total`: Total checks served from the local fallback because Redis was unavailable — alert on sustained non-zero values

## Alerts

A new alert rule is added in `monitoring/alerts.yml`:
- `WAFAbuseDetected`: Triggers when more than 10 requests are blocked by the WAF in 5 minutes

## Alternative: ModSecurity + OWASP CRS

For production deployments, consider using an ingress-level WAF like ModSecurity with the OWASP Core Rule Set (CRS) instead of, or in addition to, the in-process WAF. This provides more comprehensive protection and offloads processing from the application.

### Example Nginx Configuration (simplified)
```nginx
server {
    listen 80;
    server_name api.ledgerlens.io;

    # Enable ModSecurity
    modsecurity on;
    modsecurity_rules_file /etc/nginx/modsecurity.conf;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Testing

WAF tests are in `tests/test_waf_middleware.py` and cover:
- SQLi/XSS blocking
- Benign request allowance
- Oversized body blocking

Distributed rate limiter tests:
- `tests/test_rate_limiter.py` — atomic correctness under concurrency, the
  bounded-overshoot guarantee, fail-open fallback (logged warning + metric),
  the `GATEWAY_QUOTA_STORE=sqlite` opt-out, and a two-independent-OS-process
  test (via `fakeredis.TcpFakeServer`, a real TCP-listening fake Redis one
  process's client can't distinguish from the real thing) proving combined
  cross-replica throughput does not exceed the configured limit.
- `tests/test_rate_limit_cross_protocol.py` — reproduces, then refutes, the
  same-process REST-vs-gRPC ~2x bypass by alternating real REST
  (`TestClient`) and real gRPC (`ScoringServiceStub`) calls against one API
  key and asserting the combined allowed count equals the configured limit.
- `tests/test_grpc_scoring_service.py::test_rate_limit_exceeded_shared_counter`
  — gRPC-only regression coverage for the same shared counter.

To run the tests:
```bash
pytest tests/test_waf_middleware.py tests/test_rate_limiter.py \
       tests/test_rate_limit_cross_protocol.py tests/test_grpc_scoring_service.py
```

## Security Considerations
- The WAF is a defense-in-depth measure, not a substitute for proper input validation and sanitization
- Signature-based detection can have false positives; monitor blocked requests and tune if needed
- The WAF fails open to avoid self-inflicted DoS; monitor logs for internal errors
- All blocked request logs have wallet addresses masked using the existing masking logic from `config.correlation`
- The rate limiter's fail-open fallback is a deliberate availability/strictness
  tradeoff, not a bypass: a client cannot induce it (it only engages when
  Redis itself is actually unreachable), and it never grants more than this
  codebase's pre-fix per-process behavior — see "Distributed Rate Limiting" above.
