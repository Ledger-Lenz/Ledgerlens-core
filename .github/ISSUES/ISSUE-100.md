---
title: "Build Data Retention Policy Engine with Per-Table TTL and Scheduled Archival"
labels: ["difficulty: intermediate", "area: infrastructure", "type: feature"]
assignees: []
---

## Summary
The LedgerLens SQLite database grows unboundedly as risk scores, alert events, and trade features accumulate. Without a retention policy, the database will exhaust disk space within months on a production node. A configurable retention policy engine that archives old records to Parquet and purges them from SQLite on a nightly schedule keeps disk usage bounded.

## Objectives
- [ ] Implement `RetentionEngine` in `storage/retention.py` with per-table TTL configuration
- [ ] Default TTLs: `risk_scores` 365 days, `trades` 90 days, `alert_events` 730 days
- [ ] Nightly job: archive records older than TTL to `data/archive/YYYY-MM/table_name.parquet`, then DELETE from SQLite
- [ ] `cli.py db retention --dry-run` shows what would be archived without making changes
- [ ] `GET /admin/storage` returns current database size, row counts, and next archival date

## Definition of Done
- [ ] Nightly archival job runs via the existing scheduler
- [ ] Parquet archives readable by pandas after archival
- [ ] No data loss: row counts in Parquet + SQLite = pre-archival SQLite count
- [ ] Tests verify dry-run reports correctly and actual archival deletes correct rows
