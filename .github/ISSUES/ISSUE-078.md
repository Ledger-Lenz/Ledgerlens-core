---
title: "Add OpenAPI 3.1 Schema Auto-Generation and Interactive Swagger Documentation"
labels: ["difficulty: intermediate", "area: api", "type: enhancement"]
assignees: []
---

## Summary
The LedgerLens REST API lacks machine-readable OpenAPI documentation, forcing integrators to read source code to understand request/response schemas. Auto-generating an OpenAPI 3.1 spec from FastAPI route annotations and Pydantic models, then serving it via Swagger UI and ReDoc, provides a self-documenting API surface that accelerates third-party integration.

## Background & Context
FastAPI natively generates OpenAPI specs from route decorators and Pydantic response models. However, the current `api/main.py` has incomplete response model annotations and missing `summary`/`description` fields on many routes, resulting in a sparse auto-generated spec. This issue completes the annotation work and enables the auto-generated spec as the canonical API reference.

## Objectives
- [ ] Annotate all FastAPI routes in `api/main.py` with `response_model`, `summary`, `description`, and `tags`
- [ ] Add `openapi_extra` for routes with non-Pydantic responses (e.g., streaming endpoints)
- [ ] Serve Swagger UI at `GET /docs` and ReDoc at `GET /redoc`
- [ ] Export static `openapi.json` via `cli.py api export-schema --output openapi.json`
- [ ] Add CI step that fails if the exported schema diverges from the committed `docs/openapi.json`

## Definition of Done
- [ ] All 15+ routes have complete OpenAPI annotations
- [ ] Swagger UI renders all request/response schemas without errors
- [ ] CI schema-diff check passes on the main branch
- [ ] `docs/openapi.json` committed and linked from README
