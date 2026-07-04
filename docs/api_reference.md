# REST API Reference

The LedgerLens local API is a FastAPI application serving risk scores, alerts,
and analyst review data from the local SQLite store.

## Authentication

Two API keys gate protected endpoints:

| Header | Scope |
|--------|-------|
| `X-LedgerLens-Admin-Key` | Admin endpoints (drift reports, retrain runs, analyst dashboard) |
| `X-LedgerLens-Compliance-Key` | Compliance endpoints (IVMS, SAR packages) |

Set these in your `.env` file:

```bash
LEDGERLENS_ADMIN_API_KEY=your-admin-key
LEDGERLENS_COMPLIANCE_API_KEY=your-compliance-key
```

## Core Endpoints

### GET /health

Returns component health status.

```json
{"status": "ok", "db": "ok", "models": "ok"}
```

Returns `503` when any component is degraded.

### GET /scores

List all latest risk scores with optional filtering.

Query params: `min_score`, `limit`, `offset`, `benford_flag`, `ml_flag`, `sort_by`

### GET /scores/{wallet}

Latest scores for a specific wallet, with cross-chain links.

### GET /scores/{wallet}/explain

Top-5 SHAP feature contributions for a wallet/asset-pair.

### GET /alerts

Manipulation alerts. Use `alert_type` to filter by type
(`WASH_TRADING`, `SANDWICH_ATTACK`, `CIRCULAR_ROUTE`, etc.).

## Analyst Dashboard

The following endpoints all require `X-LedgerLens-Admin-Key`.

### GET /analyst/wallet/{wallet}

Combined analyst view: risk score, SHAP top-10, trade timeline,
ring membership, score trend, open alerts.

### POST /analyst/wallet/{wallet}/feedback

Submit analyst verdict (`confirmed_wash` | `false_positive` | `needs_review`).

### GET /analyst/queue

Top 20 wallets awaiting analyst review, sorted by score descending.

### GET /analyst/stats

Aggregate stats: cases reviewed today, false positive rate (30d), avg review time.

### GET /analyst/feedback?since=ISO_TIMESTAMP

Export feedback records for the active learning loop.

## Admin Endpoints

All require `X-LedgerLens-Admin-Key`.

| Endpoint | Description |
|----------|-------------|
| `GET /admin/drift-reports` | Recent drift check results |
| `GET /admin/retrain-runs` | Per-model retrain outcomes |
| `GET /admin/robustness-report` | Latest adversarial robustness report |
| `GET /admin/federated/audit-log` | Federated learning round audit records |

## Compliance Endpoints

All require `X-LedgerLens-Compliance-Key` and are excluded from the public
OpenAPI schema. See [compliance_export.md](compliance_export.md) for full
details, regulatory context, and a legal disclaimer.

| Endpoint | Description |
|----------|-------------|
| `GET /compliance/ivms/{wallet}` | IVMS 101 Travel-Rule risk-augmentation block |
| `POST /compliance/sar-package` | SAR evidence ZIP (narrative + supporting evidence) |
| `GET /compliance/audit-trail/{wallet}` | Full chronological event log for a wallet |

Both export endpoints accept `?dry_run=true` to skip writing to the
`compliance_exports` audit table (the response itself is unaffected).
`POST /compliance/sar-package` additionally requires the wallet's current
risk score to be at least `COMPLIANCE_SAR_MIN_SCORE` (400 otherwise) and is
rate-limited to `COMPLIANCE_EXPORT_RATE_LIMIT_PER_HOUR` exports/hour (429
otherwise).

## OpenAPI / Swagger UI

The full interactive OpenAPI spec is available at `/docs` when the server is running.
