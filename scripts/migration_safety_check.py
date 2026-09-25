"""Apply Alembic migrations against a production-sized snapshot and enforce a lock budget.

The snapshot is a SQLite database migrated to ``SEED_REVISION`` and filled with
synthetic, anonymized rows at production-scale volumes (``ROW_COUNTS``). No
production data is ever copied: every value is generated from the row index, so
the snapshot can be cached and shared freely in CI.

Each migration after ``SEED_REVISION`` is then applied one revision at a time
and timed. SQLite holds its database write lock for the whole migration
transaction, so the wall-clock duration of a revision is its lock duration. Any
revision exceeding ``--budget-seconds`` fails the check unless ``--allow-over-budget``
is passed (CI sets it when a maintainer applies the ``migration-lock-approved``
label to the pull request).

See ``alembic/README.md`` ("Migration safety check") for the refresh process.

Usage::

    python scripts/migration_safety_check.py --snapshot .cache/migration-snapshot.db
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent

# Revision the snapshot is seeded at. Bump this to the latest revision on main
# when new tables should be populated in the snapshot (see alembic/README.md).
SEED_REVISION = "0001_initial_schema"

# Approximate production row counts per table. Tables not listed get
# DEFAULT_ROWS. Update these from production ``SELECT COUNT(*)`` figures.
ROW_COUNTS = {
    "trades": 2_000_000,
    "risk_scores": 500_000,
    "feature_vectors": 500_000,
    "liquidity_pool_trades": 250_000,
    "path_payments": 250_000,
}
DEFAULT_ROWS = 50_000
DEFAULT_BUDGET_SECONDS = 5.0
_BATCH = 50_000


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _synthetic_value(col_name: str, col_type: str, i: int):
    col_type = col_type.upper()
    if "INT" in col_type:
        return i
    if any(t in col_type for t in ("REAL", "FLOA", "DOUB", "NUMERIC")):
        return (i % 10_000) / 10.0
    name = col_name.lower()
    if name.endswith(("_at", "_time")) or name in {"timestamp", "date"}:
        return f"2026-01-01T00:00:{i % 60:02d}+00:00"
    return f"{name}_{i:010d}"


def _seed_table(conn: sqlite3.Connection, table: str, rows: int) -> None:
    cols = [
        (name, ctype)
        for _cid, name, ctype, _notnull, _default, pk in conn.execute(f'PRAGMA table_info("{table}")')
        if not (pk and "INT" in ctype.upper())  # let INTEGER PRIMARY KEY autoincrement
    ]
    if not cols:
        return
    col_sql = ", ".join(f'"{c}"' for c, _ in cols)
    placeholders = ", ".join("?" for _ in cols)
    sql = f'INSERT OR IGNORE INTO "{table}" ({col_sql}) VALUES ({placeholders})'
    try:
        for start in range(0, rows, _BATCH):
            batch = range(start, min(start + _BATCH, rows))
            conn.executemany(sql, ([_synthetic_value(c, t, i) for c, t in cols] for i in batch))
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        print(f"  warning: could not seed {table}: {exc}", file=sys.stderr)


def build_snapshot(snapshot: Path) -> None:
    """Create the anonymized snapshot at SEED_REVISION with synthetic rows."""
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.unlink(missing_ok=True)
    command.upgrade(_alembic_config(snapshot), SEED_REVISION)
    conn = sqlite3.connect(snapshot)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        for table in tables:
            rows = ROW_COUNTS.get(table, DEFAULT_ROWS)
            print(f"  seeding {table}: {rows} rows")
            _seed_table(conn, table, rows)
    finally:
        conn.close()


def run_check(snapshot: Path, budget: float, allow_over_budget: bool) -> int:
    if not snapshot.exists():
        print(f"Building snapshot at {snapshot} (seed revision {SEED_REVISION})")
        build_snapshot(snapshot)

    work_db = snapshot.with_suffix(".work.db")
    shutil.copyfile(snapshot, work_db)
    cfg = _alembic_config(work_db)
    script = ScriptDirectory.from_config(cfg)
    revisions = [r.revision for r in reversed(list(script.walk_revisions()))]
    pending = revisions[revisions.index(SEED_REVISION) + 1 :]
    # Warm up env.py imports so they are not attributed to the first migration.
    command.current(cfg)

    over_budget = []
    for rev in pending:
        start = time.monotonic()
        command.upgrade(cfg, rev)
        elapsed = time.monotonic() - start
        status = "OK" if elapsed <= budget else "OVER BUDGET"
        print(f"{rev}: {elapsed:.2f}s (budget {budget:.2f}s) {status}")
        if elapsed > budget:
            over_budget.append(rev)
    work_db.unlink(missing_ok=True)

    if over_budget and not allow_over_budget:
        print(
            f"Migrations exceeded the {budget:.2f}s lock budget: {', '.join(over_budget)}. "
            "Rewrite them to avoid long locks, or have a maintainer apply the "
            "'migration-lock-approved' label to sign off.",
            file=sys.stderr,
        )
        return 1
    if over_budget:
        print(f"Over-budget migrations explicitly approved: {', '.join(over_budget)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, default=REPO_ROOT / ".cache" / "migration-snapshot.db")
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--allow-over-budget", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the snapshot even if it exists")
    args = parser.parse_args()
    if args.rebuild:
        build_snapshot(args.snapshot)
    return run_check(args.snapshot, args.budget_seconds, args.allow_over_budget)


if __name__ == "__main__":
    sys.exit(main())
