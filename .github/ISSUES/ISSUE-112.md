---
title: "Build JavaScript/TypeScript SDK with Zod Schema Validation for Frontend Integration"
labels: ["difficulty: intermediate", "area: sdk", "type: feature"]
assignees: []
---

## Summary
Exchange dashboards and compliance portals built on React or Vue need a typed JavaScript client for the LedgerLens API. A TypeScript SDK with Zod runtime validation, auto-generated from the OpenAPI spec, provides type safety and schema validation for frontend consumers.

## Objectives
- [ ] Generate TypeScript client from `docs/openapi.json` using `openapi-typescript-codegen`
- [ ] Wrap generated client with `LedgerLensClient` class and Zod validators for all response schemas
- [ ] Publish as `@ledgerlens/sdk` on npm
- [ ] Include browser and Node.js builds (ESM + CJS)
- [ ] Example: `const { score } = await client.getScore("G...")` with full TypeScript inference

## Definition of Done
- [ ] `npm install @ledgerlens/sdk` installs in a blank TypeScript project
- [ ] All API responses validated by Zod at runtime; unknown fields stripped
- [ ] SDK bundle size < 50KB gzipped
- [ ] Integration test runs against local API server using `node`
