---
title: "Implement Wallet Allowlist and Denylist Management API with Audit Trail"
labels: ["difficulty: intermediate", "area: api", "type: feature"]
assignees: []
---

## Summary
Exchange operators need to permanently flag certain wallets as trusted (allowlisted) or as confirmed bad actors (denylisted) independently of the ML risk score. An allowlist/denylist management API that overrides the computed score for flagged wallets — and maintains a full audit trail of who added or removed each entry — provides human-in-the-loop risk overrides.

## Objectives
- [ ] `POST /admin/allowlist` and `POST /admin/denylist` with `{wallet, reason, added_by}`
- [ ] `GET /admin/allowlist` and `GET /admin/denylist` with pagination
- [ ] Allowlisted wallet: `GET /scores/{wallet}` returns score 0 with `override: "allowlisted"` field
- [ ] Denylisted wallet: `GET /scores/{wallet}` returns score 100 with `override: "denylisted"` field
- [ ] `DELETE /admin/allowlist/{wallet}` removes entry; audit log records removal with timestamp and actor
- [ ] SQLite `wallet_overrides` table with full history (soft delete, not hard delete)

## Definition of Done
- [ ] Override takes effect immediately after POST
- [ ] Audit trail shows all additions and removals with timestamp
- [ ] Soft delete preserves history: removed entries still appear with `removed_at` timestamp
- [ ] Tests cover allowlisted score override, denylisted score override, and audit log integrity
