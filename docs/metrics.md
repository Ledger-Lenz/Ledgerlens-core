# LedgerLens Prometheus Metrics

LedgerLens exposes a Prometheus-compatible metrics endpoint at `GET /metrics`
(configurable via `METRICS_ENDPOINT`, default `/metrics`). All metric names are
prefixed with `ledgerlens_`.

## Quick Start

```bash
# Requires X-LedgerLens-Admin-Key when LEDGERLENS_ADMIN_API_KEY is set
curl -H "X-LedgerLens-Admin-Key: your-admin-key" http://localhost:8000/metrics
```

The response is the standard [Prometheus text exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/#text-based-format), compatible with Prometheus `scrape_configs`, Grafana datasources, and any OpenMetrics-compliant collector.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `METRICS_ENABLED` | `true` | Set to `false` to disable all metric collection. Returns HTTP 503 from `/metrics`. |
| `METRICS_ENDPOINT` | `/metrics` | URL path for the Prometheus scrape endpoint. Must start with `/`. |
| `LEDGERLENS_ADMIN_API_KEY` | _(unset)_ | When set, `/metrics` requires `X-LedgerLens-Admin-Key` header. **Always set in production.** |

> **Security**: if `LEDGERLENS_ADMIN_API_KEY` is unset, `/metrics` is publicly accessible and a `WARNING` is logged at startup. Metrics expose operational intelligence (queue depths, error rates, request counts) that should not be visible to unauthenticated users in production.

## Metric Catalogue

### Ingestion — Streamer Metrics

These metrics are emitted by `ingestion/horizon_streamer.py`.

#### `ledgerlens_ingestion_events_received_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | `source` |
| Description | Total trade events received from Horizon SSE |

Label values for `source`:
- `horizon_sse` — events from the real-time SSE stream
- `historical_rest` — events from historical REST ingestion (future)

**Alert**: if this counter is not increasing over a 5-minute window, the streamer has stalled.

```promql
# SSE ingestion stall alert
rate(ledgerlens_ingestion_events_received_total{source="horizon_sse"}[5m]) == 0
```

---

#### `ledgerlens_ingestion_events_queued_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | `source` |
| Description | Total trade events successfully placed on the bounded processing queue |

**Note**: `events_received_total - events_queued_total = events_dropped_total` (the identity holds per source).

---

#### `ledgerlens_ingestion_events_dropped_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | `source`, `reason` |
| Description | Total trade events discarded due to queue overflow |

Label values for `reason`:
- `drop_newest` — the incoming event was discarded (queue is full, `STREAMER_OVERFLOW_STRATEGY=drop_newest`)
- `drop_oldest` — the oldest queued event was discarded to make room for the new one (`drop_oldest`)

**Alert threshold**: > 100 drops per minute indicates the downstream consumer cannot keep up.

```promql
# Queue overflow alert
rate(ledgerlens_ingestion_events_dropped_total[1m]) * 60 > 100
```

---

#### `ledgerlens_ingestion_sse_reconnects_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | _(none)_ |
| Description | Total SSE stream reconnections (triggered by transport errors) |

**Alert threshold**: > 5 reconnects in 10 minutes suggests persistent Horizon instability.

```promql
increase(ledgerlens_ingestion_sse_reconnects_total[10m]) > 5
```

---

#### `ledgerlens_ingestion_queue_depth`

| Property | Value |
|---|---|
| Type | Gauge |
| Labels | `source` |
| Description | Current number of trades waiting in the bounded queue |

**Alert threshold**: queue depth consistently above 80% of `STREAMER_QUEUE_MAXSIZE` indicates sustained backpressure.

```promql
ledgerlens_ingestion_queue_depth / 1000 > 0.8   # assuming maxsize=1000
```

---

#### `ledgerlens_ingestion_queue_depth_peak`

| Property | Value |
|---|---|
| Type | Gauge |
| Labels | `source` |
| Description | High-water mark of queue depth since the last process restart |

Useful for capacity planning: if `queue_depth_peak` frequently approaches `STREAMER_QUEUE_MAXSIZE`, increase queue size or downstream processing throughput.

---

### HTTP Client Metrics

These metrics are emitted by `ingestion/http_client.py` for every outgoing request to the Stellar Horizon API.

#### `ledgerlens_http_requests_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | `endpoint`, `method`, `status_code` |
| Description | Total HTTP requests to Horizon, by normalised endpoint, HTTP method, and response status code |

The `endpoint` label is **normalised** by `_normalise_endpoint()`:
- Stellar wallet addresses are replaced with `{account_id}`
- Numeric paging tokens / ledger IDs are replaced with `{id}`
- Query strings are stripped

This prevents high-cardinality label explosion from wallet addresses appearing in Horizon URL paths.

`status_code` is the HTTP response code as a string (e.g. `"200"`, `"429"`, `"503"`) or `"error"` for transport-level failures (connection refused, timeout before response headers received).

**Alert**: sustained 429 rate > 10% of total requests indicates rate limit pressure.

```promql
rate(ledgerlens_http_requests_total{status_code="429"}[5m])
  / rate(ledgerlens_http_requests_total[5m]) > 0.1
```

---

#### `ledgerlens_http_request_duration_seconds`

| Property | Value |
|---|---|
| Type | Histogram |
| Labels | `endpoint` |
| Buckets | 10ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s |
| Description | HTTP request latency from first byte sent to last byte received |

**Alert**: p99 latency above 5 seconds degrades "real-time" scoring quality.

```promql
histogram_quantile(0.99, rate(ledgerlens_http_request_duration_seconds_bucket[5m])) > 5
```

---

#### `ledgerlens_http_rate_limit_hits_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | _(none)_ |
| Description | Total HTTP 429 (Too Many Requests) responses received from Horizon |

Equivalent to filtering `ledgerlens_http_requests_total{status_code="429"}` but provided as a convenience counter for simple alerting rules.

---

#### `ledgerlens_http_retries_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | `reason` |
| Description | Total retry attempts for failed HTTP requests |

Label values for `reason`:
- `5xx` — retrying after a 5xx server error
- `429` — retrying after a rate-limit response
- `timeout` — retrying after a transport error (connection timeout, DNS failure)

**Alert**: sustained retry rate > 20% suggests a systemic upstream issue.

```promql
rate(ledgerlens_http_retries_total[5m]) > 0.2
```

---

### Pipeline Latency

#### `ledgerlens_ledger_close_to_score_seconds`

| Property | Value |
|---|---|
| Type | Histogram |
| Labels | _(none)_ |
| Buckets | 1s, 5s, 10s, 30s, 60s, 120s, 300s |
| Description | End-to-end latency from Horizon `ledger_close_time` to `RiskScore` written to the local SQLite store |

This is the primary SLO metric for LedgerLens's "real-time" claim.

**Alert**: p95 latency above 60 seconds means LedgerLens is not processing trades in real time.

```promql
histogram_quantile(0.95, rate(ledgerlens_ledger_close_to_score_seconds_bucket[10m])) > 60
```

---

### Dead-Letter Queue (DLQ)

#### `ledgerlens_dlq_entries_total`

| Property | Value |
|---|---|
| Type | Counter |
| Labels | `error_class` |
| Description | Total records permanently failed and moved to the dead-letter queue |

Common `error_class` values:
- `SCHEMA_ERROR` — event failed schema validation (e.g. tampered bridge event)
- `PARSE_ERROR` — JSON decode failure
- `SUBMISSION_ERROR` — Soroban on-chain submission failed after all retries

**Alert**: any DLQ growth indicates a class of events that cannot be processed.

```promql
increase(ledgerlens_dlq_entries_total[1h]) > 0
```

---

#### `ledgerlens_dlq_depth`

| Property | Value |
|---|---|
| Type | Gauge |
| Labels | _(none)_ |
| Description | Current number of pending entries in the dead-letter queue |

**Alert threshold**: > 0 entries means there are unprocessed failures requiring manual investigation.

```promql
ledgerlens_dlq_depth > 0
```

---

## Recommended Alert Rules

Copy these into your `monitoring/alerts.yml` (see also the pre-built alert rules in `monitoring/alerts.yml`):

```yaml
groups:
  - name: ledgerlens_ingestion
    rules:
      - alert: IngestionStalled
        expr: rate(ledgerlens_ingestion_events_received_total{source="horizon_sse"}[5m]) == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "LedgerLens SSE ingestion has stalled"
          description: "No events received in the last 5 minutes. Check Horizon connectivity and circuit breaker state."

      - alert: QueueOverflowHigh
        expr: rate(ledgerlens_ingestion_events_dropped_total[1m]) * 60 > 100
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "LedgerLens queue dropping > 100 events/min"
          description: "{{ $value | printf \"%.0f\" }} events/min dropped. Increase STREAMER_QUEUE_MAXSIZE or downstream throughput."

      - alert: HorizonRateLimitHigh
        expr: rate(ledgerlens_http_requests_total{status_code="429"}[5m]) / rate(ledgerlens_http_requests_total[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Horizon rate limiting > 10% of requests"
          description: "Reduce HORIZON_RATE_LIMIT or upgrade the Horizon API tier."

      - alert: ScoringLatencyHigh
        expr: histogram_quantile(0.95, rate(ledgerlens_ledger_close_to_score_seconds_bucket[10m])) > 60
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "LedgerLens p95 scoring latency > 60s"
          description: "The real-time scoring guarantee is compromised. Check model inference throughput."

      - alert: DLQBacklog
        expr: ledgerlens_dlq_depth > 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "LedgerLens dead-letter queue is non-empty"
          description: "{{ $value }} unprocessed failures in the DLQ. Run `GET /webhooks/dead-letters` to inspect."

      - alert: SSEReconnectStorm
        expr: increase(ledgerlens_ingestion_sse_reconnects_total[10m]) > 5
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "LedgerLens SSE reconnecting frequently"
          description: "{{ $value | printf \"%.0f\" }} reconnects in 10 minutes. Check Horizon network stability."
```

## Cardinality Notes

High label cardinality is the primary scaling failure mode for Prometheus metrics. LedgerLens enforces two critical protections:

1. **Endpoint normalisation** (`_normalise_endpoint`): Stellar wallet addresses, paging tokens, and transaction hashes appearing in Horizon URL paths are replaced with stable placeholders (`{account_id}`, `{id}`) before use as label values. Without this, every unique wallet address would create a new time series.

2. **No PII in labels**: Wallet addresses, API keys, and transaction hashes must never appear as label values. This is a hard rule enforced in code review.

## Scrape Configuration

Add LedgerLens to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "ledgerlens"
    scrape_interval: 15s
    static_configs:
      - targets: ["localhost:8000"]
    metrics_path: "/metrics"
    authorization:
      type: Bearer   # or use basic_auth; depends on your auth setup
    # Pass admin key as a custom header
    # (Prometheus does not natively support custom headers; use a proxy or
    #  set LEDGERLENS_ADMIN_API_KEY="" to allow unauthenticated scraping
    #  in isolated networks only)
```

For production deployments where the admin key must be passed, place a reverse-proxy (nginx, Caddy, or Envoy) in front of the metrics endpoint to inject the `X-LedgerLens-Admin-Key` header, keeping the key out of the Prometheus config file.
