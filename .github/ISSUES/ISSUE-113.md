---
title: "Implement Immutable Audit Log for All Score Computation and API Access Events"
labels: ["difficulty: advanced", "area: compliance", "type: feature"]
assignees: []
---

## Summary
Regulators and internal auditors require a tamper-evident log of every risk score computation, API access, and administrative action. An append-only audit log where each entry is chained to the previous via HMAC ensures that no historical record can be deleted or modified without detection.

## Objectives
- [ ] Implement `AuditLogger` in `storage/audit_log.py` writing to `audit_log` SQLite table
- [ ] Each entry: timestamp, event_type, actor, wallet (if applicable), score (if applicable), prev_hash, entry_hash
- [ ] `entry_hash = HMAC-SHA256(key=AUDIT_SECRET, msg=canonical_json(entry_without_hash))`
- [ ] `prev_hash` is the hash of the preceding entry; first entry has `prev_hash = "genesis"`
- [ ] `cli.py audit verify` checks the full chain from genesis; reports first broken link
- [ ] Events to log: score computed, API key used, admin config changed, suppression rule added/removed

## Definition of Done
- [ ] Chain verification passes on a 10k-entry log
- [ ] Deleting or modifying any entry causes `cli.py audit verify` to report the tampered entry
- [ ] All six event types generate correctly chained entries
- [ ] Tests verify chain integrity after 1000 consecutive writes
