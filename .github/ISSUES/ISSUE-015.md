---
title: "Add Real-Time Ingestion Throughput Metrics and Prometheus Export"
labels: ["difficulty: advanced", "area: ingestion", "type: observability"]
assignees: []
---

## Summary
LedgerLens has no operational observability into its ingestion pipeline — there is no way to know current trades-per-second throughput, HTTP error rates, queue depths, or latency percentiles without reading raw logs. Adding structured metrics collection throughout `horizon_streamer.py` and `http_client.py`, with a Prometheus-compatible `/metrics` endpoint on the local API, will enable real-time monitoring, alerting, and capacity planning for production deployments.

## Background & Context
The README describes a multi-layer architecture (ingestion → detection → output). In production, the ingestion layer is the entry point for all data, and its health directly determines the quality of risk scores produced by the detection engine. Without metrics, operators are blind to:
- **Throughput degradation**: if trades/sec drops from 50 to 2, wash-trade rings that trade during low-coverage periods are missed
- **Backpressure buildup**: queue depth growing toward the `STREAMER_QUEUE_MAXSIZE` limit (ISSUE-002) means events are about to be dropped
- **Error rate spikes**: a sudden increase in HTTP 429s indicates the Horizon rate limit has been hit
- **Latency spikes**: if the time from ledger close to score publish exceeds 60 seconds, the "real-time" claim of LedgerLens is compromised

Prometheus is the standard metrics format for Python services. The `prometheus_client` library provides `Counter`, `Gauge`, `Histogram`, and `Summary` metric types. The metrics must be exposed on the existing FastAPI app in `api/main.py` at `GET /metrics` in the standard Prometheus text exposition format.

The `StreamerMetrics` dataclass from ISSUE-002 provides a starting point. This issue extends it with Prometheus metric objects and integrates metrics collection across all ingestion components.

## Objectives
- [ ] Implement a `IngestionMetricsCollector` singleton in a new `ingestion/metrics.py` module that owns all `prometheus_client` metric objects and exposes a clean Python API for incrementing/recording them from ingestion code.
- [ ] Instrument `horizon_streamer.py` with counter increments for events received, events queued, events dropped, and SSE reconnects; and gauges for current queue depth.
- [ ] Instrument `http_client.py` with histograms for request latency (by endpoint), counters for request count and error count (by status code), and a gauge for the current token-bucket fill level.
- [ ] Add `GET /metrics` to `api/main.py` that returns the Prometheus text exposition format from `prometheus_client.generate_latest()`.

## Technical Requirements

**`IngestionMetricsCollector` — metric definitions:**
```python
from prometheus_client import Counter, Gauge, Histogram, REGISTRY
from prometheus_client.exposition import generate_latest, CONTENT_TYPE_LATEST

class IngestionMetricsCollector:
    _instance: "IngestionMetricsCollector | None" = None

    # --- Streamer metrics ---
    events_received_total = Counter(
        "ledgerlens_ingestion_events_received_total",
        "Total trade events received from Horizon SSE",
        ["source"],             # e.g. "horizon_sse", "historical_rest"
    )
    events_queued_total = Counter(
        "ledgerlens_ingestion_events_queued_total",
        "Total trade events successfully queued for processing",
        ["source"],
    )
    events_dropped_total = Counter(
        "ledgerlens_ingestion_events_dropped_total",
        "Total trade events dropped due to queue overflow",
        ["source", "reason"],  # reason: "drop_newest" | "drop_oldest"
    )
    sse_reconnects_total = Counter(
        "ledgerlens_ingestion_sse_reconnects_total",
        "Total SSE stream reconnections",
    )
    queue_depth = Gauge(
        "ledgerlens_ingestion_queue_depth",
        "Current trade queue depth",
        ["source"],
    )
    queue_depth_peak = Gauge(
        "ledgerlens_ingestion_queue_depth_peak",
        "Peak queue depth since last reset",
        ["source"],
    )

    # --- HTTP client metrics ---
    http_requests_total = Counter(
        "ledgerlens_http_requests_total",
        "Total HTTP requests to Horizon",
        ["endpoint", "method", "status_code"],
    )
    http_request_duration_seconds = Histogram(
        "ledgerlens_http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["endpoint"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    http_rate_limit_hits_total = Counter(
        "ledgerlens_http_rate_limit_hits_total",
        "Total HTTP 429 responses received from Horizon",
    )
    http_retries_total = Counter(
        "ledgerlens_http_retries_total",
        "Total retry attempts for failed HTTP requests",
        ["reason"],            # reason: "5xx" | "429" | "timeout"
    )

    # --- Pipeline latency ---
    ledger_close_to_score_seconds = Histogram(
        "ledgerlens_ledger_close_to_score_seconds",
        "Time from Horizon ledger close to RiskScore written",
        buckets=[1, 5, 10, 30, 60, 120, 300],
    )

    # --- DLQ metrics ---
    dlq_entries_total = Counter(
        "ledgerlens_dlq_entries_total",
        "Total records sent to the dead-letter queue",
        ["error_class"],
    )
    dlq_depth = Gauge(
        "ledgerlens_dlq_depth",
        "Current number of pending DLQ entries",
    )

    @classmethod
    def instance(cls) -> "IngestionMetricsCollector":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
```

**Instrumentation in `horizon_streamer.py`:**
```python
metrics = IngestionMetricsCollector.instance()

async def _stream_loop(self):
    async for event in self._sse_client:
        metrics.events_received_total.labels(source="horizon_sse").inc()
        queued = await self.queue.put(trade)
        if queued:
            metrics.events_queued_total.labels(source="horizon_sse").inc()
        else:
            metrics.events_dropped_total.labels(
                source="horizon_sse",
                reason=self.queue.overflow_strategy,
            ).inc()
        metrics.queue_depth.labels(source="horizon_sse").set(self.queue.depth())
```

**Instrumentation in `http_client.py`:**
```python
import time

async def _make_request(self, method, url, **kwargs):
    endpoint = self._normalise_endpoint(url)   # strip query params + path segments after 2nd /
    start = time.perf_counter()
    try:
        response = await self._client.request(method, url, **kwargs)
        duration = time.perf_counter() - start
        metrics.http_requests_total.labels(
            endpoint=endpoint, method=method, status_code=str(response.status_code)
        ).inc()
        metrics.http_request_duration_seconds.labels(endpoint=endpoint).observe(duration)
        return response
    except Exception as exc:
        duration = time.perf_counter() - start
        metrics.http_requests_total.labels(
            endpoint=endpoint, method=method, status_code="error"
        ).inc()
        metrics.http_request_duration_seconds.labels(endpoint=endpoint).observe(duration)
        raise
```

**`GET /metrics` endpoint in `api/main.py`:**
```python
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus metrics endpoint. Returns text exposition format."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
```

**Endpoint normalisation**: the `endpoint` label on HTTP metrics must strip query parameters and replace dynamic path segments with placeholders to avoid high-cardinality label explosions:
```python
def _normalise_endpoint(url: str) -> str:
    # /accounts/GABC123/transactions → /accounts/{account_id}/transactions
    # /trades?cursor=123 → /trades
    import re
    path = urlparse(url).path
    path = re.sub(r'/[A-Z][A-Z0-9]{54}', '/{account_id}', path)   # Stellar addresses
    path = re.sub(r'/[0-9]{10,}', '/{id}', path)                   # numeric IDs
    return path
```

**Configuration** (add to `config/settings.py`):
- `METRICS_ENABLED`: default `True`
- `METRICS_ENDPOINT`: default `"/metrics"`

**`prometheus_client` must be imported lazily** (only if `METRICS_ENABLED=True`) so that environments without the library can still run with metrics disabled.

## Security Considerations
- The `/metrics` endpoint must be protected if `LEDGERLENS_ADMIN_API_KEY` is set — metrics can reveal operational intelligence (queue depths, error rates) that should not be publicly accessible.
- Prometheus metric label values must not include wallet addresses, transaction hashes, or API keys — only structural values (endpoint paths, status codes, error classes). Violating this would create high-cardinality labels that degrade Prometheus performance and could leak PII.
- The endpoint normalisation function is critical for security: without it, unique wallet addresses in Horizon URLs would create millions of distinct label combinations, exhausting Prometheus memory.
- Add a startup check that warns if `METRICS_ENABLED=True` but `LEDGERLENS_ADMIN_API_KEY` is unset, as this exposes metrics publicly.

## Testing Requirements
- Unit tests covering `IngestionMetricsCollector`: singleton pattern (same instance returned on multiple calls), metric objects are not re-registered on second instantiation
- Unit tests covering `_normalise_endpoint()`: Stellar address in path replaced with `{account_id}`, numeric ID replaced with `{id}`, query parameters stripped, path with no dynamic segments unchanged
- Unit tests covering streamer instrumentation: mock `BoundedTradeQueue`; verify counter increments and gauge updates for queued, dropped (both strategies), and SSE reconnect events
- Unit tests covering HTTP client instrumentation: mock `httpx`; verify histogram observation on success and error, correct status code label, duration > 0
- Integration tests: start FastAPI test client; call `GET /metrics`; assert response is valid Prometheus text format containing `ledgerlens_ingestion_events_received_total`
- Integration tests: run mock streamer for 100 events; call `/metrics`; assert counter values match event counts
- Edge cases: metrics endpoint called before any events processed (all counters at 0 — valid Prometheus output), concurrent metric updates from multiple async workers
- Performance benchmark: recording 100,000 metric observations should add < 50 ms overhead vs baseline (metrics must not be a hot-path bottleneck)

## Documentation Requirements
- Update `README.md` Quick Start and Local API sections to mention `/metrics` endpoint and link to Prometheus docs
- Add docstrings to `IngestionMetricsCollector` and `_normalise_endpoint`
- Create `docs/metrics.md` documenting all metric names, labels, types, and what alert thresholds are recommended (e.g., `ledgerlens_ingestion_events_dropped_total > 100/min` → investigate queue depth)
- Add `prometheus_client` to `requirements.txt` with a pinned version

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: `prometheus_client`, Prometheus metric types (Counter, Gauge, Histogram), FastAPI middleware, metrics observability design
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python backend engineer with hands-on experience instrumenting production services with Prometheus metrics. Strong understanding of metric cardinality, label design, and the difference between Counter, Gauge, Histogram, and Summary types. Experience with FastAPI custom response types and middleware is a plus.
