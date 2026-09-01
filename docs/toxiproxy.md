# Toxiproxy & Chaos Testing

LedgerLens uses [Toxiproxy](https://github.com/Shopify/toxiproxy) to inject
network faults (latency, connection refusal, partitions) between the service and
its external dependencies during the chaos-engineering test suite under
`tests/chaos/`.

The proxy topology is declared once in **`toxiproxy.json`** at the repository
root and loaded into the Toxiproxy container at startup. Individual tests then
attach and detach *toxics* (latency, timeout, disable) to these proxies at
runtime through the Toxiproxy REST API on port `8474`.

## Proxy definitions

`toxiproxy.json` defines three proxies:

| Proxy name         | Listen address   | Upstream                  | What it simulates |
| ------------------ | ---------------- | ------------------------- | ----------------- |
| `horizon_proxy`     | `0.0.0.0:18000` | `horizon.stellar.org:443` | The Stellar **Horizon API** connection for the scoring pipeline. Tests inject downstream latency here to verify p99 scoring latency stays bounded and recovers after the fault clears. |
| `horizon_partition` | `0.0.0.0:18001` | `horizon.stellar.org:443` | A **partial network partition** to Horizon (a second, independent Horizon path). Tests inject a large latency/timeout toxic here to drive the `SorobanPublisher` **circuit breaker** open within its failure threshold and confirm it auto-resets. |
| `redis_proxy`       | `0.0.0.0:16379` | `redis:6379`              | The **Redis hot-tier** connection for the feature store. Tests *disable* this proxy entirely (connection refused) to verify the feature store falls back to its in-process cold tier without raising, then resumes hot writes once Redis returns. |

All three proxies are `enabled: true` in the committed config, i.e. they pass
traffic straight through until a test adds a toxic.

> **Note on `redis_proxy` upstream.** The committed config points at `redis:6379`
> (the Docker Compose service name), which is what the container resolves. The
> `tests/chaos/test_redis_fallback.py` fixture also calls `create_proxy(...)`
> with `localhost:6379`; because Toxiproxy returns HTTP 409 for an existing
> proxy name, the fixture reuses the config-loaded proxy rather than overriding
> it. The port (`16379`) is the contract that matters.

## Which tests use which proxy

Verified by grepping `tests/chaos/` for the proxy names and listen ports:

| Test file | Proxy used | Port | Toxics applied |
| --------- | ---------- | ---- | -------------- |
| `tests/chaos/test_horizon_latency.py`  | `horizon_proxy`     | `18000` | `latency` (500 ms + jitter), then removed to assert recovery |
| `tests/chaos/test_circuit_breaker.py`  | `horizon_partition` | `18001` | `latency`/`timeout` (5000 ms) to trip the circuit breaker; the pure unit-level circuit-breaker tests in the same file need no proxy |
| `tests/chaos/test_redis_fallback.py`   | `redis_proxy`       | `16379` | proxy `disable`/`enable` to simulate connection-refused and recovery |
| `tests/chaos/test_sqlite_wal_lock.py`  | none                | –       | Holds a DB write lock directly; does not touch Toxiproxy |

`tests/chaos/conftest.py` owns the shared `ToxiproxyClient` helper and an
`autouse`, session-scoped `require_toxiproxy` fixture that **skips the entire
chaos suite** if the Toxiproxy REST API (`http://localhost:8474/version`) is not
reachable.

## Starting Toxiproxy with this configuration

### Via Docker Compose (recommended)

The `toxiproxy` service lives in the `chaos` profile of `docker-compose.yml` and
bind-mounts `toxiproxy.json` to `/etc/toxiproxy/config.json`:

```yaml
toxiproxy:
  image: ghcr.io/shopify/toxiproxy:2.9.0
  profiles: [chaos]
  ports:
    - "8474:8474"     # Toxiproxy REST API
    - "18000:18000"   # horizon_proxy      (latency scenario)
    - "18001:18001"   # horizon_partition  (circuit-breaker scenario)
    - "16379:16379"   # redis_proxy        (fallback scenario)
  command: ["-host", "0.0.0.0", "-config", "/etc/toxiproxy/config.json"]
  volumes:
    - ./toxiproxy.json:/etc/toxiproxy/config.json:ro
```

Start just the proxy:

```bash
docker compose --profile chaos up -d toxiproxy
```

Or run the whole suite (brings up the `chaos` profile, runs the tests, tears
down) via the Makefile:

```bash
make test-chaos
```

which executes:

```bash
docker compose --profile chaos up -d --wait
pytest tests/chaos/ -m chaos -v --tb=short --timeout=120
docker compose --profile chaos down
```

### Via the standalone binary

If you have `toxiproxy-server` installed locally:

```bash
toxiproxy-server -host 0.0.0.0 -config toxiproxy.json
```

## Verifying it is up

```bash
curl -s http://localhost:8474/version
curl -s http://localhost:8474/proxies | jq 'keys'
# ["horizon_partition", "horizon_proxy", "redis_proxy"]
```

## Related

- `tests/chaos/` — the chaos-engineering test suite
- `docker-compose.yml` — `toxiproxy` service (`chaos` profile)
- `Makefile` — `test-chaos` target
- `chaos-mesh/` — Kubernetes-level Chaos Mesh experiments (separate from Toxiproxy)
