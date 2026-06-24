---
title: "Add Multi-Tenant Namespace Isolation for White-Label Exchange Partner Deployments"
labels: ["difficulty: expert", "area: infrastructure", "type: feature"]
assignees: []
---

## Summary
Exchange partners deploying LedgerLens as a white-label solution currently share a single namespace: risk scores, alert rules, and suppressions from one exchange are visible to all. Row-level namespace isolation in the database and API key-to-namespace binding enables multiple exchanges to share a single LedgerLens deployment while keeping their data strictly isolated.

## Objectives
- [ ] Add `namespace_id` column to all data tables (risk_scores, alert_events, wallet_overrides, etc.)
- [ ] Bind each API key to a namespace in `api_keys` table
- [ ] All queries automatically scoped to the API key's namespace (via FastAPI dependency)
- [ ] Admin key with `namespace: *` can query all namespaces (for the LedgerLens operator)
- [ ] `GET /admin/namespaces` lists all namespaces with record counts
- [ ] Migration script backfills existing data to `namespace_id = "default"`

## Definition of Done
- [ ] API key for namespace A cannot retrieve data from namespace B (403 response)
- [ ] Namespace isolation verified by integration test with two separate API keys
- [ ] Admin wildcard key sees data from all namespaces
- [ ] Migration script runs without downtime on existing database
