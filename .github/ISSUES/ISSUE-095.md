---
title: "Implement Graceful Shutdown with In-Flight Request Draining and Connection Cleanup"
labels: ["difficulty: intermediate", "area: infrastructure", "type: enhancement"]
assignees: []
---

## Summary
Sending SIGTERM to the LedgerLens API server currently kills in-flight requests abruptly, causing clients to receive connection-reset errors during rolling deployments. A graceful shutdown handler that stops accepting new connections, drains in-flight requests with a configurable timeout, and cleanly closes database connections prevents client-visible errors during deployment.

## Objectives
- [ ] Register SIGTERM and SIGINT handlers in `api/main.py` using FastAPI lifespan events
- [ ] On shutdown signal: stop accepting new requests (return 503 to new connections), wait up to `SHUTDOWN_TIMEOUT` seconds (default 30) for in-flight requests to complete
- [ ] Close SQLite WAL checkpoint, flush Prometheus metrics, and close Redis connection on exit
- [ ] Log shutdown sequence with timestamps for each cleanup step
- [ ] Add `GET /health/ready` that returns 503 during shutdown (used by Kubernetes readiness probe)

## Definition of Done
- [ ] `kill -TERM $(pidof uvicorn)` during an active request: request completes, server exits cleanly
- [ ] Readiness probe returns 503 within 100ms of SIGTERM receipt
- [ ] All database connections closed before process exits (verified via lsof)
- [ ] Integration test simulates graceful shutdown under load
