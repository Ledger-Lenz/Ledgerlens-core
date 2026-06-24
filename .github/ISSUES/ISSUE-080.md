---
title: "Build Admin REST API for Model Lifecycle and System Configuration Management"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary
LedgerLens has no admin API for managing model versions, retraining triggers, and runtime configuration without a server restart. Adding an admin API — protected by a separate admin key — enables operators to promote model versions, adjust score thresholds, and update alert routing rules at runtime.

## Objectives
- [ ] Implement `api/admin_router.py` with routes protected by `X-LedgerLens-Admin-Key` header
- [ ] `GET /admin/models` — list all model versions with deployment status
- [ ] `POST /admin/models/{version}/promote` — promote a model version to active
- [ ] `GET /admin/config` — return current runtime configuration as JSON
- [ ] `PATCH /admin/config` — update selected config values without restart (e.g., `SCORE_ALERT_THRESHOLD`)
- [ ] `POST /admin/retrain` — trigger an async retraining job; return job ID
- [ ] All admin routes require admin key; return 403 (not 401) for missing/invalid key

## Definition of Done
- [ ] All 6 admin routes implemented with input validation
- [ ] Config updates persist to SQLite and take effect immediately without restart
- [ ] Admin routes are excluded from the public OpenAPI spec (`include_in_schema=False`)
- [ ] Tests cover admin key enforcement for every route
