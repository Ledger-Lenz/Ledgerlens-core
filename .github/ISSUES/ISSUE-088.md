---
title: "Build Alembic-Based Database Migration Framework with Rollback Support"
labels: ["difficulty: intermediate", "area: infrastructure", "type: feature"]
assignees: []
---

## Summary
LedgerLens currently creates database tables via `CREATE TABLE IF NOT EXISTS` scattered across module init code, with no migration history and no rollback capability. Adopting Alembic provides versioned migrations, safe schema evolution, and one-command rollback for failed deployments.

## Objectives
- [ ] Initialise Alembic in `alembic/` with `alembic init alembic`
- [ ] Write initial migration converting all existing `CREATE TABLE` statements into Alembic `op.create_table()` calls
- [ ] Add `cli.py db migrate` (runs `alembic upgrade head`) and `cli.py db rollback` (runs `alembic downgrade -1`)
- [ ] CI step runs `alembic upgrade head` and then `alembic downgrade base` on every PR to verify round-trip
- [ ] Document in `docs/database_migrations.md`

## Definition of Done
- [ ] `alembic upgrade head` creates all tables from a blank database
- [ ] `alembic downgrade base` drops all tables without error
- [ ] Every PR migration includes both `upgrade` and `downgrade` implementations
- [ ] CI migration round-trip test passes
