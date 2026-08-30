# utils/

Shared, dependency-free utility helpers used across the LedgerLens codebase.

This package is for **infrastructure-level primitives** — small, general-purpose building blocks that are not specific to any one domain (ingestion, detection, API). Domain-specific code belongs in its own package (`ingestion/`, `detection/`, `api/`, etc.). If a helper needs to import from one of those packages, it does not belong here.

## Contents

### `circuit_breaker.py`

A thread-safe circuit breaker for wrapping calls to unreliable external dependencies.

Used today by:
- `ingestion/horizon_streamer.py` — guards connections to the Stellar Horizon SSE endpoint.
- `detection/feature_store.py` — guards the Redis feature store.

**State machine**

```
         ┌─────────────────────────────────────────────────┐
         │  failure_count >= failure_threshold              │
CLOSED ──┤────────────────────────────────────────────────▶ OPEN
         │                                                  │
         │◀──── probe succeeds ──── HALF_OPEN ◀────────────┤
         │                              │                   │
         │                              └─── probe fails ──▶│
         └─────────────────────────────────────────────────┘
                                                 ↑ recovery_timeout elapsed
```

| State       | Behaviour                                                                 |
|-------------|---------------------------------------------------------------------------|
| `CLOSED`    | Normal operation. Consecutive failures are counted.                       |
| `OPEN`      | Every call raises `CircuitOpenError` immediately — no I/O attempted.      |
| `HALF_OPEN` | One probe call is allowed through. Success → `CLOSED`; failure → `OPEN`. |

**Basic usage**

```python
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError

breaker = CircuitBreaker(
    name="horizon",
    failure_threshold=5,    # open after 5 consecutive failures
    recovery_timeout=60.0,  # try a probe after 60 seconds
)

# Option 1 — convenience wrapper
try:
    result = breaker.call(my_function, arg1, arg2)
except CircuitOpenError:
    # circuit is open; fast-fail or return a cached/default response
    ...

# Option 2 — manual check (used by async callers that manage their own I/O)
if not breaker.allow_request():
    raise CircuitOpenError(breaker.name)
try:
    result = await my_async_call()
    breaker.record_success()
except Exception:
    breaker.record_failure()
    raise
```

**Lifecycle hooks**

Pass `on_open` and `on_close` callbacks to emit metrics or alerts when the circuit transitions:

```python
from ingestion.metrics import get_metrics

_m = get_metrics()

breaker = CircuitBreaker(
    name="redis",
    on_open=lambda: _m.circuit_breaker_open.labels(name="redis").set(1),
    on_close=lambda: _m.circuit_breaker_open.labels(name="redis").set(0),
)
```

**Thread safety**

All state transitions are serialised under a single `threading.Lock`, so the breaker is safe for use across multiple streaming workers or request-handler threads sharing one instance.

## Adding new utilities

A helper belongs in `utils/` if it satisfies **all** of these criteria:

1. It is general-purpose — not specific to ingestion, detection, the API, or any other domain.
2. It has no imports from other LedgerLens packages (`ingestion`, `detection`, `api`, `config`, …).
3. It has minimal external dependencies (ideally none beyond the standard library).
4. It is expected to be reused in at least two different LedgerLens modules.

If the helper needs LedgerLens configuration or domain types, place it in the most specific package that owns its primary concern instead.

## Further Reading

- [docs/observability.md](../docs/observability.md) — how circuit breaker state is surfaced via Prometheus metrics.
- [docs/threat_model.md](../docs/threat_model.md) — circuit breakers as a DoS mitigation in the STRIDE analysis.
- `ingestion/horizon_streamer.py` — primary consumer of `CircuitBreaker`.
- `detection/soroban_publisher.py` — Soroban submission circuit breaker (owns its own instance).
