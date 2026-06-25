# Database Migrations

LedgerLens uses [Alembic](https://alembic.sqlalchemy.org/) for versioned, rollback-capable database migrations.

## Quick reference

| Command | Effect |
|---------|--------|
| `python cli.py db migrate` | Apply all pending migrations (`alembic upgrade head`) |
| `python cli.py db rollback` | Roll back one step (`alembic downgrade -1`) |
| `alembic upgrade head` | Same as `db migrate` (direct Alembic CLI) |
| `alembic downgrade base` | Roll back to a blank database |
| `alembic current` | Show the current revision |
| `alembic history` | List all revisions |

## Configuration

The database path is resolved from the `LEDGERLENS_DB_PATH` environment variable (default `./ledgerlens.db`). Alembic's `env.py` reads this automatically — no changes to `alembic.ini` are needed.

## Writing a new migration

1. Create a new file in `alembic/versions/` named `NNNN_short_description.py` where `NNNN` is the next sequential number.

2. Set `revision` to the new ID and `down_revision` to the previous revision ID:

```python
revision = "0002"
down_revision = "0001"
```

3. Implement both `upgrade()` and `downgrade()`:

```python
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.add_column("risk_scores", sa.Column("new_field", sa.Text))

def downgrade() -> None:
    with op.batch_alter_table("risk_scores") as batch_op:
        batch_op.drop_column("new_field")
```

> **SQLite note**: SQLite does not support `DROP COLUMN` or most `ALTER TABLE` variants directly. Always use `op.batch_alter_table()` for structural changes to existing tables — Alembic handles the table-rebuild internally.

4. Verify the round-trip locally before opening a PR:

```bash
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

## CI validation

Every PR that touches `alembic/versions/` should run the migration round-trip to catch errors before merge:

```bash
# In CI (fresh database)
alembic upgrade head
alembic downgrade base
```

## Relationship to the legacy migration system

`detection/storage.py` contains an older in-process migration system (`_MIGRATIONS` list + `migrate_db()`). That system remains active for backward compatibility with existing deployments. For new schema changes, prefer Alembic migrations. The Alembic `0001_initial_schema.py` migration encodes the full schema as of issue #168; subsequent structural changes should be Alembic-only.

## Rollback procedure

If a deployment fails after `alembic upgrade head`:

```bash
# Roll back one step
alembic downgrade -1

# Or roll back to a specific known-good revision
alembic downgrade 0001
```

Each migration script must include a correct `downgrade()` implementation — PRs without a working downgrade will not be merged.
