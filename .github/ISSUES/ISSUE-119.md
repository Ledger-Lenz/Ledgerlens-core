---
title: "Implement Distributed Tracing Context Propagation Across All Service Boundaries"
labels: ["difficulty: advanced", "area: observability", "type: enhancement"]
assignees: []
---

## Summary
The existing OpenTelemetry integration (ISSUE-046) instruments the API and detection pipeline in isolation. When a score request triggers ingestion, feature extraction, and model inference across async boundaries, traces are broken at the boundary — making it impossible to correlate latency spikes to their root component. End-to-end trace context propagation with W3C TraceContext headers connects all spans into a single distributed trace.

## Objectives
- [ ] Propagate `traceparent` / `tracestate` W3C headers from API request through to ingestion and feature store calls
- [ ] Instrument `asyncio.create_task()` callsites with `opentelemetry.context.attach()` to preserve trace context across async boundaries
- [ ] Add spans to: Horizon HTTP call, Redis feature lookup, model inference call, SQLite write
- [ ] Configure OTLP exporter to Jaeger (default) or any OTLP-compatible collector
- [ ] `docker-compose.yml` dev profile includes a Jaeger all-in-one container

## Definition of Done
- [ ] A single API scoring request produces a trace with ≥ 5 child spans in Jaeger
- [ ] Async boundaries do not break the trace: all spans share the same trace ID
- [ ] Jaeger UI accessible at `localhost:16686` via `docker compose --profile dev up`
- [ ] Tests verify trace context present in all outgoing HTTP headers
