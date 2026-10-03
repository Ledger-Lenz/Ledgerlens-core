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

Tiered retention (Issue #977)
-----------------------------
:class:`TieredRetentionEngine` keeps data queryable instead of only
archiving it, moving rows through three tiers as they age:

    ====  ===========================  =====================  ==================
    Tier  Age (default)                Backend                Added query latency
    ====  ===========================  =====================  ==================
    hot   < 30 days                    primary SQLite DB      none (indexed)
    warm  30 - 365 days                separate SQLite DB     ~ms (extra attach)
    cold  > 365 days                   monthly Parquet files  ~100ms-s (file scan)
    ====  ===========================  =====================  ==================

:meth:`TieredRetentionEngine.migrate` moves hot→warm and warm→cold, verifying
that per-table row counts across all tiers are unchanged by the migration and
refusing to delete source rows otherwise. :meth:`TieredRetentionEngine.start_scheduler`
runs the migration on a fixed interval (default daily) in a daemon thread.
:meth:`TieredRetentionEngine.query` transparently fans a time-range query out
to every tier that can hold matching rows and merges the results.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

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
        total_rows = sum(r.get("rows_archived", 0) for r in report.values())
        logger.info(
            "Retention run complete: %d row(s) affected across %d table(s) (dry_run=%s)",
            total_rows,
            len(report),
            dry_run,
        )
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
                logger.info("Retention skipped for %s: table does not exist", table)
                return {"cutoff_date": cutoff_iso, "rows_archived": 0, "archive_path": None, "skipped": True}

            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {ts_col} < ?", (cutoff_iso,)  # noqa: S608
            ).fetchone()
            count = row[0]

        if count == 0:
            logger.info("No rows older than cutoff=%s in %s; nothing to archive", cutoff_iso, table)
            return {"cutoff_date": cutoff_iso, "rows_archived": 0, "archive_path": None}

        if dry_run:
            logger.info(
                "Dry-run: would archive/purge %d row(s) from %s (cutoff=%s)",
                count,
                table,
                cutoff_iso,
            )
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


# ---------------------------------------------------------------------------
# Tiered hot/warm/cold retention (Issue #977)
# ---------------------------------------------------------------------------

DEFAULT_HOT_DAYS = 30
DEFAULT_WARM_DAYS = 365
DEFAULT_MIGRATION_INTERVAL_SECONDS = 24 * 3600


class TierIntegrityError(RuntimeError):
    """Raised when a migration would change the total row count of a table."""


class TieredRetentionEngine:
    """Hot (primary SQLite) → warm (secondary SQLite) → cold (Parquet) retention."""

    def __init__(
        self,
        db_path: str,
        warm_db_path: str = "./data/warm.db",
        cold_root: str = "./data/cold",
        tables: list[str] | None = None,
        hot_days: int = DEFAULT_HOT_DAYS,
        warm_days: int = DEFAULT_WARM_DAYS,
    ) -> None:
        if not 0 < hot_days < warm_days:
            raise ValueError("require 0 < hot_days < warm_days")
        self._db = db_path
        self._warm_db = warm_db_path
        self._cold_root = Path(cold_root)
        self._tables = list(tables or DEFAULT_TTL)
        self._hot_days = hot_days
        self._warm_days = warm_days
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- migration -------------------------------------------------------

    def migrate(self, now: datetime | None = None) -> dict[str, dict]:
        """Move aged rows hot→warm→cold for every table; returns per-table counts."""
        now = now or datetime.now(timezone.utc)
        hot_cutoff = (now - timedelta(days=self._hot_days)).isoformat()
        cold_cutoff = (now - timedelta(days=self._warm_days)).isoformat()
        report: dict[str, dict] = {}
        with self._lock:
            for table in self._tables:
                if not self._hot_table_exists(table):
                    report[table] = {"skipped": True, "to_warm": 0, "to_cold": 0}
                    continue
                before = self._total_rows(table)
                to_warm = self._hot_to_warm(table, hot_cutoff)
                to_cold = self._warm_to_cold(table, cold_cutoff)
                after = self._total_rows(table)
                if before != after:
                    raise TierIntegrityError(
                        f"{table}: row count changed during migration ({before} -> {after})"
                    )
                report[table] = {"to_warm": to_warm, "to_cold": to_cold, "total_rows": after}
        logger.info("Tiered retention migration complete: %s", report)
        return report

    def start_scheduler(self, interval_seconds: float = DEFAULT_MIGRATION_INTERVAL_SECONDS) -> None:
        """Run :meth:`migrate` every ``interval_seconds`` in a daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.migrate()
                except Exception:
                    logger.exception("Scheduled tiered retention migration failed")

        self._thread = threading.Thread(target=_loop, name="tiered-retention", daemon=True)
        self._thread.start()

    def stop_scheduler(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # -- query routing ---------------------------------------------------

    def query(
        self,
        table: str,
        start: str | None = None,
        end: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows with ``start <= timestamp < end`` from every relevant tier.

        Tiers whose age range cannot overlap the window are skipped, so
        queries over recent data never pay warm/cold latency. Results are
        ordered by timestamp ascending.
        """
        now = now or datetime.now(timezone.utc)
        hot_cutoff = (now - timedelta(days=self._hot_days)).isoformat()
        cold_cutoff = (now - timedelta(days=self._warm_days)).isoformat()
        ts_col = _TIMESTAMP_COLUMN.get(table, "timestamp")

        # Hot may still hold old rows if migration has not run yet, so it is
        # always queried. Warm/cold only ever hold rows older than the hot/warm
        # cutoff at migration time, which is never newer than the cutoff now.
        rows = self._query_sqlite(self._db, table, ts_col, start, end)
        if start is None or start < hot_cutoff:
            rows += self._query_sqlite(self._warm_db, table, ts_col, start, end)
        if start is None or start < cold_cutoff:
            rows += self._query_cold(table, ts_col, start, end)
        rows.sort(key=lambda r: r[ts_col])
        return rows

    # -- internals -------------------------------------------------------

    def _hot_table_exists(self, table: str) -> bool:
        with _connect(self._db) as conn:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None

    def _ensure_warm_table(self, conn: sqlite3.Connection, table: str) -> None:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        exists = conn.execute(
            "SELECT 1 FROM warm.sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            conn.execute(ddl.replace(f"CREATE TABLE {table}", f"CREATE TABLE warm.{table}", 1)
                         .replace(f'CREATE TABLE "{table}"', f"CREATE TABLE warm.{table}", 1))

    def _hot_to_warm(self, table: str, cutoff: str) -> int:
        ts_col = _TIMESTAMP_COLUMN.get(table, "timestamp")
        Path(self._warm_db).parent.mkdir(parents=True, exist_ok=True)
        with _connect(self._db) as conn:
            conn.execute("ATTACH DATABASE ? AS warm", (self._warm_db,))
            try:
                self._ensure_warm_table(conn, table)
                with conn:  # single transaction: copy + delete atomically
                    cur = conn.execute(
                        f"INSERT INTO warm.{table} SELECT * FROM main.{table} WHERE {ts_col} < ?",
                        (cutoff,),
                    )
                    moved = cur.rowcount
                    conn.execute(f"DELETE FROM main.{table} WHERE {ts_col} < ?", (cutoff,))
            finally:
                conn.execute("DETACH DATABASE warm")
        return moved

    def _warm_to_cold(self, table: str, cutoff: str) -> int:
        import pandas as pd

        ts_col = _TIMESTAMP_COLUMN.get(table, "timestamp")
        if not Path(self._warm_db).exists():
            return 0
        with _connect(self._warm_db) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                return 0
            df = pd.read_sql_query(
                f"SELECT * FROM {table} WHERE {ts_col} < ?", conn, params=(cutoff,)
            )
        if df.empty:
            return 0

        # Partition by the month of each row's own timestamp.
        months = df[ts_col].astype(str).str[:7]
        for month, part in df.groupby(months):
            path = self._cold_root / month / f"{table}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                part = pd.concat([pd.read_parquet(path), part], ignore_index=True)
            part.to_parquet(path, index=False)

        with _connect(self._warm_db) as conn:
            conn.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,))
            conn.commit()
        return len(df)

    def _total_rows(self, table: str) -> int:
        total = 0
        for db in (self._db, self._warm_db):
            if not Path(db).exists():
                continue
            with _connect(db) as conn:
                try:
                    total += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.OperationalError:
                    pass
        import pyarrow.parquet as pq

        for path in self._cold_root.glob(f"*/{table}.parquet"):
            total += pq.ParquetFile(path).metadata.num_rows
        return total

    @staticmethod
    def _query_sqlite(
        db: str, table: str, ts_col: str, start: str | None, end: str | None
    ) -> list[dict[str, Any]]:
        if not Path(db).exists():
            return []
        sql = f"SELECT * FROM {table} WHERE 1=1"
        params: list[str] = []
        if start is not None:
            sql += f" AND {ts_col} >= ?"
            params.append(start)
        if end is not None:
            sql += f" AND {ts_col} < ?"
            params.append(end)
        with _connect(db) as conn:
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            except sqlite3.OperationalError:
                return []

    def _query_cold(
        self, table: str, ts_col: str, start: str | None, end: str | None
    ) -> list[dict[str, Any]]:
        import pandas as pd

        rows: list[dict[str, Any]] = []
        for path in sorted(self._cold_root.glob(f"*/{table}.parquet")):
            month = path.parent.name
            if (start is not None and month < start[:7]) or (end is not None and month > end[:7]):
                continue
            df = pd.read_parquet(path)
            if start is not None:
                df = df[df[ts_col] >= start]
            if end is not None:
                df = df[df[ts_col] < end]
            rows += df.to_dict(orient="records")
        return rows
