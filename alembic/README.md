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
