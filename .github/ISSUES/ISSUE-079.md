---
title: "Implement API Versioning with /v1/ Path Prefix and Deprecation Headers"
labels: ["difficulty: intermediate", "area: api", "type: enhancement"]
assignees: []
---

## Summary
The LedgerLens API has no versioning strategy, meaning any breaking change to an endpoint forces immediate migration for all consumers. Adding a `/v1/` path prefix and a `Deprecation` response header framework enables backward-compatible evolution and gives integrators a migration window before breaking changes land.

## Background & Context
As the API surface grows, schema changes to endpoints like `GET /scores/{wallet}` are inevitable. Without versioning, a response field rename breaks every downstream consumer simultaneously. The industry standard for REST APIs is URL-based versioning (`/v1/`, `/v2/`) with `Deprecation` and `Sunset` headers (RFC 8594) to signal planned breaking changes.

## Objectives
- [ ] Mount all existing routes under `/v1/` prefix via FastAPI `APIRouter(prefix="/v1")`
- [ ] Keep `/scores/{wallet}` etc. as aliases (302 redirect) for a 90-day deprecation period
- [ ] Implement `DeprecationMiddleware` that adds `Deprecation` and `Sunset` headers to aliased routes
- [ ] Document versioning policy in `docs/api_versioning.md`
- [ ] Update all integration tests and the OpenAPI spec for the `/v1/` prefix

## Definition of Done
- [ ] All routes accessible under `/v1/`
- [ ] Legacy paths return 302 with `Deprecation` header
- [ ] Version policy documented with concrete example of how a breaking change would be handled
- [ ] No existing integration tests broken after the rename
