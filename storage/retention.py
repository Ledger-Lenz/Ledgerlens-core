"""Data retention policy engine (Issue #180).

Archives records older than per-table TTLs to Parquet files under
``data/archive/YYYY-MM/`` and then purges them from SQLite.  Designed
to be invoked nightly by the existing scheduler.

Default TTLs:
    risk_scores    365 days
    trades         90  days (mapped to ``feature_vectors`` table)
    alert_events   730 days (mapped to ``alerts`` table)

The archival is *safe by construction*: rows are written to Parquet
before they are deleted, and the count invariant
    parquet_rows + sqlite_rows == pre-archival_sqlite_rows
is verifiable after each run.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ledgerlens.retention")

# Default TTL map: SQLite table name → days to retain
DEFAULT_TTL: dict[str, int] = {
    "risk_scores": 365,
    "feature_vectors": 90,   # "trades" data lives here
    "alerts": 730,            # "alert_events"
}

# Timestamp column per table (used for cutoff comparison)
_TIMESTAMP_COLUMN: dict[str, str] = {
    "risk_scores": "timestamp",
    "feature_vectors": "timestamp",
    "alerts": "timestamp",
}


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


class RetentionEngine:
    """Archive-then-purge retention engine with per-table TTL configuration."""

    def __init__(
        self,
        db_path: str,
        archive_root: str = "./data/archive",
        ttl_days: Optional[dict[str, int]] = None,
    ) -> None:
        self._db = db_path
        self._archive_root = Path(archive_root)
        self._ttl = {**DEFAULT_TTL, **(ttl_days or {})}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, dry_run: bool = False) -> dict[str, dict]:
        """Run the retention job across all configured tables.

        Returns a report dict keyed by table name, each entry containing:
            cutoff_date, rows_archived, archive_path (or None on dry-run)
        """
        report: dict[str, dict] = {}
        for table, days in self._ttl.items():
            result = self._process_table(table, days, dry_run=dry_run)
            report[table] = result
        return report

    def storage_stats(self) -> dict:
        """Return current DB size, row counts per retained table, and next archival date."""
        db_path = Path(self._db)
        size_bytes = db_path.stat().st_size if db_path.exists() else 0

        row_counts: dict[str, int] = {}
        with _connect(self._db) as conn:
            for table in self._ttl:
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
                    row_counts[table] = row[0]
                except sqlite3.OperationalError:
                    row_counts[table] = 0

        # Next archival = midnight UTC tomorrow
        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        return {
            "db_path": str(self._db),
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
            "row_counts": row_counts,
            "next_archival_utc": next_run.isoformat(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_table(self, table: str, days: int, *, dry_run: bool) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()
        ts_col = _TIMESTAMP_COLUMN.get(table, "timestamp")

        with _connect(self._db) as conn:
            # Check table exists
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                return {"cutoff_date": cutoff_iso, "rows_archived": 0, "archive_path": None, "skipped": True}

            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {ts_col} < ?", (cutoff_iso,)  # noqa: S608
            ).fetchone()
            count = row[0]

        if count == 0:
            return {"cutoff_date": cutoff_iso, "rows_archived": 0, "archive_path": None}

        if dry_run:
            return {"cutoff_date": cutoff_iso, "rows_archived": count, "archive_path": None, "dry_run": True}

        # Archive to Parquet
        import pandas as pd

        archive_path = self._archive_path(table, cutoff)
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        with _connect(self._db) as conn:
            df = pd.read_sql_query(
                f"SELECT * FROM {table} WHERE {ts_col} < ?", conn, params=(cutoff_iso,)  # noqa: S608
            )

        if archive_path.exists():
            existing = pd.read_parquet(archive_path)
            df = pd.concat([existing, df], ignore_index=True)

        df.to_parquet(archive_path, index=False)
        logger.info("Archived %d rows from %s to %s", count, table, archive_path)

        # Purge from SQLite
        with _connect(self._db) as conn:
            conn.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff_iso,))  # noqa: S608
            conn.commit()

        logger.info("Purged %d rows from %s (cutoff=%s)", count, table, cutoff_iso)
        return {"cutoff_date": cutoff_iso, "rows_archived": count, "archive_path": str(archive_path)}

    def _archive_path(self, table: str, cutoff: datetime) -> Path:
        month_dir = cutoff.strftime("%Y-%m")
        return self._archive_root / month_dir / f"{table}.parquet"
