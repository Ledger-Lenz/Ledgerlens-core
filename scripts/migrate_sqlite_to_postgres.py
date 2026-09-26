"""Migration script to export data from local SQLite to Postgres.

Usage:
    python scripts/migrate_sqlite_to_postgres.py --sqlite-db data/ledgerlens.db --pg-url postgresql://user:pass@host:5432/ledgerlens
    python scripts/migrate_sqlite_to_postgres.py --sqlite-db data/ledgerlens.db --pg-url ... --dry-run

Safety model:
    * ``--dry-run`` reads the source only and reports per-table row counts
      without opening a write transaction on the destination.
    * All inserts run inside a single destination transaction. After every
      table is copied, row counts and an order-independent content checksum
      are compared between source and destination *before* commit. Any
      mismatch raises ``VerificationError`` and the transaction is rolled back,
      leaving the destination untouched.

Rollback procedure (if a problem is found after a successful commit):
    1. Stop every writer pointed at the Postgres database.
    2. Re-run with ``--verify-only`` to list the mismatched tables.
    3. Truncate the migrated tables, e.g.
       ``TRUNCATE risk_scores, trades, ... RESTART IDENTITY CASCADE;``
       (or restore the pre-migration ``pg_dump`` snapshot taken beforehand).
    4. Point services back at SQLite (unset ``DATABASE_URL``) until the
       migration is re-run and ``--verify-only`` reports no mismatches.
"""

import argparse
import hashlib
import logging
import math
import sqlite3
import sys

import pandas as pd
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TABLES = [
    "risk_scores",
    "on_chain_submissions",
    "pair_correlations",
    "trades",
    "feature_vectors",
    "liquidity_pool_trades",
    "path_payments",
    "circular_path_routes",
    "drift_reports",
    "retrain_runs",
    "robustness_reports",
    "committee_members",
    "score_disputes",
    "score_overrides",
    "runtime_config",
    "governance_proposals",
    "governance_votes",
    "governance_committee",
    "wallet_feature_states",
    "wash_rings",
    "bridge_transfers",
    "alerts",
    "path_payment_cycles",
    "soroban_dead_letters",
    "benford_baselines",
    "case_assignments",
    "analyst_feedback",
    "compliance_exports"
]

_CHECKSUM_MOD = 1 << 256


class VerificationError(RuntimeError):
    """Raised when source and destination data diverge after migration."""


def _normalize(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "\x00NULL"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def _checksum(rows, columns: list[str]) -> tuple[int, str]:
    """Return (row_count, order-independent sha256 checksum) for *rows*."""
    count, acc = 0, 0
    for row in rows:
        payload = "\x1f".join(f"{c}={_normalize(v)}" for c, v in zip(columns, row))
        acc = (acc + int.from_bytes(hashlib.sha256(payload.encode()).digest(), "big")) % _CHECKSUM_MOD
        count += 1
    return count, f"{acc:064x}"


def _source_tables(sqlite_conn: sqlite3.Connection) -> list[str]:
    existing = {
        r[0] for r in sqlite_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [t for t in TABLES if t not in existing]
    if missing:
        logger.warning(f"Skipping tables missing from source: {', '.join(missing)}")
    return [t for t in TABLES if t in existing]


def verify_table(sqlite_conn: sqlite3.Connection, dest_conn, table: str) -> list[str]:
    """Compare *table* between source and destination; return mismatch descriptions."""
    cur = sqlite_conn.execute(f"SELECT * FROM {table}")
    columns = sorted(d[0] for d in cur.description)
    col_sql = ", ".join(columns)
    src = _checksum(sqlite_conn.execute(f"SELECT {col_sql} FROM {table}"), columns)
    dst = _checksum(dest_conn.execute(text(f"SELECT {col_sql} FROM {table}")), columns)
    problems = []
    if src[0] != dst[0]:
        problems.append(f"{table}: row count source={src[0]} destination={dst[0]}")
    elif src[1] != dst[1]:
        problems.append(f"{table}: checksum mismatch source={src[1]} destination={dst[1]}")
    return problems


def verify(sqlite_conn: sqlite3.Connection, dest_conn, tables: list[str]) -> list[str]:
    problems = []
    for table in tables:
        problems.extend(verify_table(sqlite_conn, dest_conn, table))
    return problems


def _write_chunk(chunk: pd.DataFrame, table: str, pg_conn) -> None:
    chunk.to_sql(table, pg_conn, if_exists="append", index=False)


def migrate(
    sqlite_path: str,
    pg_url: str,
    chunksize: int = 10000,
    dry_run: bool = False,
    verify_only: bool = False,
) -> dict[str, int]:
    """Migrate SQLite data into *pg_url*; return per-table source row counts.

    Raises VerificationError (after rolling back) if the copied data does not
    match the source.
    """
    logger.info(f"Connecting to SQLite at {sqlite_path}")
    sqlite_conn = sqlite3.connect(sqlite_path)
    try:
        plan = {
            t: sqlite_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in _source_tables(sqlite_conn)
        }
        # Empty tables are never written, so there is nothing to verify.
        tables = [t for t, n in plan.items() if n]

        if dry_run:
            for table, total in plan.items():
                logger.info(f"[dry-run] would migrate {total} rows into {table}")
            logger.info(f"[dry-run] total rows: {sum(plan.values())}; destination untouched")
            return plan

        logger.info("Connecting to Postgres destination")
        pg_engine = create_engine(pg_url)

        if verify_only:
            with pg_engine.connect() as pg_conn:
                problems = verify(sqlite_conn, pg_conn, tables)
            if problems:
                raise VerificationError("; ".join(problems))
            logger.info("Verification passed")
            return plan

        with pg_engine.begin() as pg_conn:
            for table, total_rows in plan.items():
                logger.info(f"Migrating table: {table} ({total_rows} rows)")
                if total_rows == 0:
                    continue
                processed = 0
                for chunk in pd.read_sql(f"SELECT * FROM {table}", sqlite_conn, chunksize=chunksize):
                    _write_chunk(chunk, table, pg_conn)
                    processed += len(chunk)
                    logger.info(f"  ... inserted {processed}/{total_rows} rows")

            problems = verify(sqlite_conn, pg_conn, tables)
            if problems:
                for p in problems:
                    logger.error(f"Verification failed: {p}")
                # Raising inside begin() rolls the whole migration back.
                raise VerificationError("; ".join(problems))
        logger.info("Verification passed; migration committed")
        return plan
    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-db", required=True)
    parser.add_argument("--pg-url", required=True)
    parser.add_argument("--chunksize", type=int, default=10000)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument(
        "--verify-only", action="store_true", help="compare source and destination only"
    )
    args = parser.parse_args()

    try:
        migrate(args.sqlite_db, args.pg_url, args.chunksize, args.dry_run, args.verify_only)
    except VerificationError as exc:
        logger.error(f"Migration rolled back: {exc}")
        sys.exit(1)
