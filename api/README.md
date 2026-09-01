# `api/` — Local FastAPI Application

This directory contains the **local read-only FastAPI application** (`main.py`) and all the routers, middleware, and supporting modules that make up LedgerLens Core's HTTP/WebSocket/GraphQL/gRPC surface.

It is a **local development stand-in** for the production [`ledgerlens-api`](https://github.com/Ledger-Lenz/ledgerlens-api) repository. It serves whatever `RiskScore` records have been written to the local SQLite store by `run_pipeline.py` or `cli.py score`, and exposes the same endpoint contracts that downstream consumers (dashboard, protocol integrations) expect.

For deeper documentation see:
- [REST API Reference](../docs/api_reference.md) — endpoint catalogue, request/response schemas
- [API Gateway Architecture](../docs/api_gateway.md) — auth resolution, quota enforcement, migration guide

---

## File inventory

| File | Purpose |
|------|---------|
| `main.py` | FastAPI application factory, lifespan handler, and all core route definitions (`/scores`, `/alerts`, `/rings`, `/webhooks`, `/disputes`, `/health`, `/metrics`, etc.) |
| `gateway.py` | `GatewayMiddleware` — single-pass auth resolution, quota enforcement, and access logging for every authenticated request. Replaces the previous per-router auth duplication. |
| `auth.py` | Lightweight `require_admin_key` and `require_compliance_key` FastAPI dependencies used by routes that pre-date the consolidated gateway. |
| `admin_router.py` | `/admin/*` endpoints for model lifecycle management, drift reports, retrain run history, and runtime configuration. Admin-key gated. |
| `analyst.py` | `/analyst/*` endpoints for the analyst review dashboard: wallet claim/release, feedback submission, case-management SLA stats, and review queue. |
| `api_key_router.py` | Scoped API key auth and rate-limit enforcement (backed by `detection/api_key_store.py`). Kept for backward compatibility — new code should go through `gateway.py`. |
| `api_keys_router.py` | Deprecated duplicate scoped API key management router. Endpoints delegate to `detection.api_key_store` and return a `Deprecation` header pointing to the migration guide. |
| `allowlist_router.py` | `/allowlist` and `/denylist` wallet override management endpoints with audit trail (backed by `detection/wallet_override_store.py`). |
| `audit_router.py` | `/audit/wallet/{wallet}` chronological scoring event history and Merkle-tree integrity verification. |
| `batch_router.py` | `/batch/score` async job queue for bulk wallet scoring requests. |
| `cross_chain_router.py` | `/cross-chain/links/{wallet}` Bayesian cross-chain wallet link hypotheses and evidence breakdowns. |
| `export_router.py` | `/export` CSV and Parquet export endpoints for risk score data. |
| `gnn_router.py` | `/gnn/ring-score/{wallet}` GNN ring membership scores and nearest-neighbour wallet similarity. |
| `graphql_schema.py` | Optional Strawberry GraphQL schema (enabled when `strawberry-graphql` is installed). |
| `grpc_scoring_service.py` | gRPC `InternalScoringService` sidecar for low-latency internal score delivery. Run via `cli.py grpc-serve`. |
| `metrics.py` | Prometheus metric definitions (`Counter`, `Gauge`, `Histogram`) prefixed with `ledgerlens_`. |
| `namespace.py` | Multi-tenant namespace isolation — `namespace_filter` dependency that scopes all data queries to the caller's `namespace_id`. |
| `streaming.py` | `ScorePublisher` and `SSEConnectionManager` for real-time score streaming over Server-Sent Events backed by Redis Pub/Sub. |
| `streaming_router.py` | `/stream/scores` SSE endpoint and `/stream/stats` connection health endpoint. |
| `temporal_router.py` | `/temporal/analysis/{wallet}` temporal anomaly detection endpoints with optional chart output. |
| `waf_middleware.py` | Web Application Firewall middleware — SQL injection / XSS pattern matching, request-size limits, and header sanitisation. |
| `webhook_sender.py` | `WebhookRetryQueue` — asyncio retry scheduler (3 attempts, exponential backoff) with dead-letter SQLite storage and HMAC-SHA256 re-signing on each attempt. |
| `ws_router.py` | `/ws/alerts` WebSocket push channel for real-time risk score alerts. |

---

## Running locally

```bash
# Serve the local API (hot-reload)
python cli.py serve --reload

# Run the gRPC sidecar
python cli.py grpc-serve
```

The app binds to `http://localhost:8000` by default. See `docs/api_reference.md` for the full endpoint catalogue and authentication instructions.
