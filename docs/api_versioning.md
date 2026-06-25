# LedgerLens API Versioning Policy

## Overview

All LedgerLens API routes are versioned under a `/v1/` path prefix. This allows breaking changes to be introduced in a future `/v2/` without forcing simultaneous migration by all consumers.

## Current version

| Version | Base path | Status |
|---------|-----------|--------|
| v1      | `/v1/`    | Active |

## URL structure

```
/v1/scores
/v1/scores/{wallet}
/v1/scores/{wallet}/explain
/v1/scores/{wallet}/counterfactual
/v1/wallets/{wallet}/cross-chain
/v1/alerts
/v1/assets/risk-ranking
/v1/rings
/v1/correlations
/v1/amm/pools/{pool_id}/risk
/v1/path-payments/circular
/v1/feedback
/v1/webhooks
/v1/disputes
/v1/governance/proposals
/v1/admin/drift-reports
/v1/admin/robustness-report
/v1/admin/retrain-runs
/v1/admin/federated/audit-log
/v1/model/robustness
/v1/model/weights
/v1/compliance/ivms/{wallet}
/v1/compliance/sar-package
/v1/compliance/audit-trail/{wallet}
/v1/health
```

## Deprecation headers (RFC 8594)

Legacy bare paths (e.g. `/scores`, `/health`) return **HTTP 302** redirects to their `/v1/` equivalents and carry three headers to signal the migration deadline:

```
Deprecation: Wed, 24 Sep 2026 00:00:00 GMT
Sunset:      Wed, 24 Sep 2026 00:00:00 GMT
Link:        </v1/scores>; rel="successor-version"
```

- **Deprecation** — date after which the legacy path may stop working.
- **Sunset** — same date; confirms the planned removal timestamp per [RFC 8594](https://www.rfc-editor.org/rfc/rfc8594).
- **Link** — points clients directly to the canonical versioned path.

Consumers SHOULD follow the `Location` header of the 302 and update their base URL to `/v1/` before the sunset date.

## Migration guide

Change your base URL from the bare path to the `/v1/` prefixed path:

```diff
- GET /scores?min_score=70
+ GET /v1/scores?min_score=70
```

```diff
- GET /scores/GABC.../explain?asset_pair=XLM/USDC
+ GET /v1/scores/GABC.../explain?asset_pair=XLM/USDC
```

All query parameters, request bodies, and response schemas are identical between the legacy path and the `/v1/` path.

## How a breaking change is handled

1. A new field is **added** to a response in `/v1/` — this is non-breaking; existing clients ignore unknown fields.
2. A field is **renamed or removed** — a new version `/v2/` is introduced:
   - `/v2/` routes are registered on a new `APIRouter(prefix="/v2")`.
   - `/v1/` routes continue to serve the old schema.
   - `Deprecation` and `Sunset` headers are added to `/v1/` responses to announce the removal date (minimum 90 days notice).
   - After the sunset date, `/v1/` routes are removed.

### Example: renaming `score` → `risk_score` in `GET /scores/{wallet}`

```
# Step 1 — ship /v2/ with the new field name; /v1/ unchanged
GET /v1/scores/{wallet}  →  { "score": 85, ... }       # deprecated
GET /v2/scores/{wallet}  →  { "risk_score": 85, ... }  # current

# Step 2 — after 90-day window, remove /v1/scores/{wallet}
```

## OpenAPI schema

The interactive OpenAPI docs at `/docs` only show `/v1/` routes. Legacy redirect paths are hidden (`include_in_schema=False`) to keep the schema clean.
