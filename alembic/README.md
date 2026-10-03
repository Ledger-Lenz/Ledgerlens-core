# Alembic migration workflow

Run these commands from the repository root. Alembic stores migration scripts
in `alembic/versions/` and reads its configuration from `alembic.ini`.

## Select the development database

`alembic/env.py` builds a SQLite URL from `LEDGERLENS_DB_PATH` when
`sqlalchemy.url` in `alembic.ini` is blank. The default is
`./ledgerlens.db`. Use a disposable development path when testing migrations:

```bash
export LEDGERLENS_DB_PATH=/tmp/ledgerlens-dev.db
```

An explicit non-empty `sqlalchemy.url` in `alembic.ini` takes precedence over
the environment variable. Never test migrations against a production path.

## Create a migration

Create a manual revision template with a meaningful message:

```bash
alembic revision -m "add review status"
```

Then implement both `upgrade()` and `downgrade()` in the generated file.
Do **not** use `--autogenerate`: `alembic/env.py` currently sets
`target_metadata = None`, so application models are not available for schema
comparison. For SQLite table alterations, use `op.batch_alter_table()`.

## Apply and inspect migrations

```bash
alembic current
alembic history
alembic upgrade head
```

The application wrapper `python cli.py db migrate` also upgrades to `head`.

## Roll back

Roll back one revision, then reapply it to verify both directions:

```bash
alembic downgrade -1
alembic upgrade head
```

Use `alembic downgrade <revision>` for a specific revision or
`alembic downgrade base` to remove the complete Alembic-managed schema. Review
the selected database path before either command.

## Migration safety check

`.github/workflows/migration-safety.yml` runs on every pull request that
touches `alembic/`. It calls `scripts/migration_safety_check.py`, which:

1. Builds (or restores from the CI cache) an anonymized, production-sized
   SQLite snapshot: the schema at `SEED_REVISION`, filled with synthetic rows
   at the volumes in `ROW_COUNTS`. No production data is copied; every value
   is generated from the row index.
2. Applies each migration after `SEED_REVISION` one revision at a time and
   records its duration. SQLite holds the write lock for the whole migration,
   so duration equals lock duration.
3. Fails if any migration exceeds the lock-duration budget
   (`--budget-seconds`, default **5 seconds**).

To sign off on an over-budget migration (for example, a one-off backfill
scheduled for a maintenance window), a maintainer applies the
`migration-lock-approved` label to the pull request; the job re-runs and
reports the migration instead of failing.

Run it locally with:

```bash
python scripts/migration_safety_check.py --snapshot .cache/migration-snapshot.db
```

### Refreshing the snapshot

Update the snapshot whenever the schema or production volumes change
significantly (at least once per release):

1. Set `SEED_REVISION` in `scripts/migration_safety_check.py` to the latest
   revision on `main`, so tables added since the last refresh are seeded.
2. Update `ROW_COUNTS` (and `DEFAULT_ROWS`) from production
   `SELECT COUNT(*)` figures, rounded up. Never copy production rows.
3. Run the script with `--rebuild` locally to confirm the snapshot builds.

Editing the script changes the CI cache key, so the next run rebuilds the
snapshot automatically.
