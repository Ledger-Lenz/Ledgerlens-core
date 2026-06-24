---
title: "Build API Key Management System with Scoped Permissions and Per-Key Rate Limits"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary
LedgerLens currently uses a single `LEDGERLENS_API_KEY` environment variable for authentication, preventing key rotation, per-consumer rate limiting, and granular permission scopes. An API key management system with per-key scopes (`read:scores`, `write:suppressions`, `admin`), rate limits, and key rotation enables production-grade access control.

## Objectives
- [ ] Implement `api_keys` table in SQLite with: key_hash, namespace_id, scopes, rate_limit_per_minute, created_at, expires_at, last_used_at
- [ ] `POST /admin/api-keys` creates a new key; response returns the plaintext key once (not stored)
- [ ] `DELETE /admin/api-keys/{key_id}` revokes a key immediately
- [ ] Scope enforcement: `read:scores` required for `GET /scores/`; `admin` required for admin routes
- [ ] Per-key rate limiting using a sliding window counter in Redis

## Definition of Done
- [ ] Revoked key returns 401 immediately (no caching delay)
- [ ] Rate limit returns 429 with `Retry-After` header
- [ ] Scope mismatch returns 403 with which scope is required
- [ ] Tests cover: revocation, scope enforcement, rate limiting, key expiry
