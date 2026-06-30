"""Incremental Parquet snapshot exports from the LedgerLens SQLite store.

Exports :class:`~ingestion.data_models.Trade` records to date-partitioned
Parquet files compatible with the ``ledgerlens-data`` repository schema.
Partitions are laid out in Hive-style directories::

    <output_dir>/
    ├── manifest.json
    └── trades/
        └── year=2026/
            └── month=06/
                └── day=01/
                    └── asset_pair=XLM_USDC/
                        └── trades_20260601_XLM_USDC.parquet

Delta detection compares each partition's record count and maximum
``paging_token`` against the last exported ``manifest.json``, skipping
partitions that have not changed since the previous export.  Use
``force=True`` to bypass delta detection and re-export everything.

Usage::

    import sqlite3
    from pathlib import Path
    from ingestion.parquet_exporter import ParquetExporter

    conn = sqlite3.connect("./ledgerlens.db")
    exporter = ParquetExporter(db_conn=conn, output_dir=Path("./export"))
    result = exporter.export(since=date(2026, 1, 1), until=date(2026, 6, 30))
    print(result)

Security notes
--------------
- ``output_dir`` is validated to sit within the application working
  directory.  Paths that escape via ``..`` are rejected with
  ``ValueError``.
- Parquet files are created with ``0o600`` permissions via a temp-file
  approach so they are never world-readable.
- All SQLite queries use parameterised statements; ``asset_pair`` values
  from the CLI are never interpolated directly into SQL.
- The SHA-256 manifest hash is computed over the *final* file contents
  after all writes complete.
- Wallet addresses and account IDs are excluded from the manifest and
  export metadata; only aggregate statistics appear outside Parquet files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("ledgerlens.parquet_exporter")

# ---------------------------------------------------------------------------
# Schema field list (usable without importing pyarrow)
# ---------------------------------------------------------------------------

PARQUET_SCHEMA_FIELDS: list[str] = [
    "id",
    "paging_token",
    "ledger_close_time",
    "base_account",
    "counter_account",
    "base_amount",
    "counter_amount",
    "base_asset_type",
    "base_asset_code",
    "base_asset_issuer",
    "counter_asset_type",
    "counter_asset_code",
    "counter_asset_issuer",
    "price",
    "base_is_seller",
    "trade_type",
]


def _build_schema():
    """Return the canonical PyArrow schema for Trade Parquet exports.

    Wrapped in a function so the module imports cleanly in environments
    where PyArrow is not installed; callers that need the schema must
    handle ``ImportError`` themselves.

    The schema matches :class:`~ingestion.data_models.Trade` field names
    exactly to maintain the shared contract with ``ledgerlens-data``.
    Decimal128(22, 7) matches Stellar's 7-decimal-place amount precision.
    """
    import pyarrow as pa  # noqa: PLC0415

    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("paging_token", pa.string()),
        pa.field("ledger_close_time", pa.timestamp("us", tz="UTC")),
        pa.field("base_account", pa.string()),
        pa.field("counter_account", pa.string()),
        pa.field("base_amount", pa.decimal128(22, 7)),
        pa.field("counter_amount", pa.decimal128(22, 7)),
        pa.field("base_asset_type", pa.string()),
        pa.field("base_asset_code", pa.string()),
        pa.field("base_asset_issuer", pa.string()),
        pa.field("counter_asset_type", pa.string()),
        pa.field("counter_asset_code", pa.string()),
        pa.field("counter_asset_issuer", pa.string()),
        pa.field("price", pa.decimal128(22, 7)),
        pa.field("base_is_seller", pa.bool_()),
        pa.field("trade_type", pa.string()),
    ])


# Expose as a module-level alias so code can write ``PARQUET_SCHEMA``
PARQUET_SCHEMA = _build_schema  # callable — call to get the actual schema


# ---------------------------------------------------------------------------
# Asset pair normalisation
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9]")


def normalise_asset_pair(asset_pair: str) -> str:
    """Normalise an asset pair string for use as a Parquet partition key.

    Replaces ``/`` and all non-alphanumeric characters with ``_``, then
    upper-cases the result to avoid filesystem issues from special characters
    in asset codes.

    Examples::

        >>> normalise_asset_pair("XLM/USDC")
        'XLM_USDC'
        >>> normalise_asset_pair("xlm/usdc")
        'XLM_USDC'
        >>> normalise_asset_pair("XLM/USD-C")
        'XLM_USD_C'
        >>> normalise_asset_pair("XLM/USDC:GBBD47IF")
        'XLM_USDC_GBBD47IF'

    Args:
        asset_pair: Raw asset pair string, e.g. ``"XLM/USDC"``.

    Returns:
        Normalised partition key string safe for directory names.
    """
    return _UNSAFE_CHARS.sub("_", asset_pair).upper()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PartitionResult:
    """Statistics for a single exported Parquet partition.

    Attributes:
        partition_date: Calendar date of this partition.
        asset_pair_raw: Original asset pair string (e.g. ``"XLM/USDC"``).
        asset_pair_key: Normalised partition key (e.g. ``"XLM_USDC"``).
        relative_path: Path relative to ``output_dir``, stored in manifest.
        records: Number of records written.
        size_bytes: File size in bytes after write.
        sha256: Hex-encoded SHA-256 of the final file contents.
        max_paging_token: Maximum ``paging_token`` value in this partition.
        exported_at: UTC timestamp when the file was written.
    """

    partition_date: date
    asset_pair_raw: str
    asset_pair_key: str
    relative_path: str
    records: int
    size_bytes: int
    sha256: str
    max_paging_token: str
    exported_at: datetime


@dataclass
class ExportResult:
    """Aggregate statistics for a complete :meth:`ParquetExporter.export` run.

    Attributes:
        total_partitions: Total (date × asset_pair) partitions examined.
        exported_partitions: Partitions written (new or changed).
        skipped_partitions: Partitions skipped by delta detection.
        total_records_exported: Sum of records across exported partitions.
        total_size_bytes: Sum of file sizes across exported partitions.
        duration_seconds: Wall-clock time for the entire export run.
        manifest_path: Absolute path to the written ``manifest.json``.
    """

    total_partitions: int
    exported_partitions: int
    skipped_partitions: int
    total_records_exported: int
    total_size_bytes: int
    duration_seconds: float
    manifest_path: Path


# ---------------------------------------------------------------------------
# ParquetExporter
# ---------------------------------------------------------------------------


class ParquetExporter:
    """Export Trade records from SQLite to date-partitioned Parquet files.

    Maintains a ``manifest.json`` in ``output_dir`` recording the SHA-256
    hash, record count, and maximum paging token for each partition.  On
    subsequent runs, reads the manifest and skips unchanged partitions
    (delta detection), making incremental exports fast for large datasets.

    Args:
        db_conn: Open ``sqlite3.Connection`` to the LedgerLens database.
            The caller is responsible for closing it after export.
        output_dir: Root directory for Parquet output.  Must resolve to a
            path within the project working directory (validated at init).
        compression: Parquet compression codec — ``"snappy"`` (default,
            fastest), ``"zstd"`` (best ratio), ``"gzip"``, or ``"none"``.
        row_group_size: Rows per Parquet row group.  Default 100,000.
            Larger values improve scan performance at higher write memory cost.

    Raises:
        ValueError: If ``output_dir`` escapes the project working directory.
        ImportError: If ``pyarrow`` is not installed.
    """

    _MANIFEST_FILENAME = "manifest.json"
    _SCHEMA_VERSION = "1.0"

    def __init__(
        self,
        db_conn: sqlite3.Connection,
        output_dir: Path,
        compression: str = "snappy",
        row_group_size: int = 100_000,
    ) -> None:
        self.db_conn = db_conn
        self.output_dir = self._validate_output_dir(output_dir)
        self.compression = compression
        self.row_group_size = row_group_size
        self._manifest_path = self.output_dir / self._MANIFEST_FILENAME

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def export(
        self,
        since: date | None = None,
        until: date | None = None,
        asset_pair: str | None = None,
        force: bool = False,
    ) -> ExportResult:
        """Export Trade records to Parquet, skipping unchanged partitions.

        Queries SQLite for distinct ``(date, asset_pair)`` combinations in
        the requested date range, then exports each changed partition.
        Unchanged partitions (same record count and ``max_paging_token``
        as the last manifest entry) are skipped unless ``force=True``.

        Args:
            since: Earliest date inclusive.  ``None`` exports from the
                earliest record in the database.
            until: Latest date inclusive.  ``None`` exports up to the most
                recent record.
            asset_pair: Filter to one asset pair, e.g. ``"XLM/USDC"``.
                ``None`` exports all pairs.
            force: Bypass delta detection and re-export every partition.

        Returns:
            :class:`ExportResult` with aggregate statistics and the path to
            the written ``manifest.json``.
        """
        import time as _time  # noqa: PLC0415

        started = _time.perf_counter()
        manifest = self._load_manifest()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        partitions = list(self._iter_partitions(since, until, asset_pair))
        results: list[PartitionResult] = []
        skipped = 0

        for part_date, pair_raw in partitions:
            if not force and not self._is_partition_changed(part_date, pair_raw, manifest):
                skipped += 1
                logger.debug("Skipping unchanged partition %s / %s", part_date, pair_raw)
                continue
            result = self._export_partition(part_date, pair_raw)
            if result is not None:
                results.append(result)
                logger.info(
                    "Exported partition %s / %s: %d records, %d bytes",
                    part_date,
                    pair_raw,
                    result.records,
                    result.size_bytes,
                )
            else:
                # Partition has zero records — treat as skipped
                skipped += 1

        self._write_manifest(results, existing_manifest=manifest)

        duration = _time.perf_counter() - started
        return ExportResult(
            total_partitions=len(partitions),
            exported_partitions=len(results),
            skipped_partitions=skipped,
            total_records_exported=sum(r.records for r in results),
            total_size_bytes=sum(r.size_bytes for r in results),
            duration_seconds=duration,
            manifest_path=self._manifest_path,
        )

    # ------------------------------------------------------------------
    # Delta detection
    # ------------------------------------------------------------------

    def _is_partition_changed(
        self,
        partition_date: date,
        asset_pair: str,
        manifest: dict,
    ) -> bool:
        """Return ``True`` when the partition needs to be re-exported.

        Queries SQLite for the current record count and maximum
        ``paging_token`` for this ``(date, asset_pair)`` combination, then
        compares against the last exported values stored in ``manifest``.

        Returns ``True`` (export needed) when:
        - The partition has no manifest entry (first export).
        - The record count has changed.
        - The maximum paging token has advanced.

        Returns ``False`` when the partition is empty (nothing to export).
        All SQL uses parameterised statements to prevent injection.

        Args:
            partition_date: Calendar date of the partition.
            asset_pair: Raw asset pair string (e.g. ``"XLM/USDC"``).
            manifest: Previously loaded manifest dict (may be empty ``{}``).

        Returns:
            ``True`` if re-export is needed, ``False`` if unchanged or empty.
        """
        cur = self.db_conn.execute(
            """
            SELECT COUNT(*), MAX(paging_token)
            FROM trades
            WHERE DATE(ledger_close_time) = ?
              AND (base_asset_code || '/' || counter_asset_code) = ?
            """,
            (partition_date.isoformat(), asset_pair),
        )
        count, max_token = cur.fetchone()
        if not count:
            return False  # empty partition — nothing to export

        key = f"{partition_date}/{normalise_asset_pair(asset_pair)}"
        index = manifest.get("partition_index", {})
        if key not in index:
            return True
        prev = index[key]
        return prev.get("records") != count or prev.get("max_paging_token") != max_token

    # ------------------------------------------------------------------
    # Partition export
    # ------------------------------------------------------------------

    def _export_partition(
        self,
        partition_date: date,
        asset_pair: str,
    ) -> PartitionResult | None:
        """Write one (date × asset_pair) partition to a Parquet file.

        Fetches Trade rows from SQLite, converts to a PyArrow Table using
        the canonical :func:`_build_schema`, and writes the file atomically
        (temp file → :func:`os.replace`) with ``0o600`` permissions.

        Args:
            partition_date: Calendar date of the partition.
            asset_pair: Raw asset pair string (e.g. ``"XLM/USDC"``).

        Returns:
            :class:`PartitionResult` on success, or ``None`` if the
            partition contains zero records.
        """
        rows = self._fetch_rows(partition_date, asset_pair)
        if not rows:
            return None

        schema = _build_schema()
        table = self._rows_to_table(rows, schema)

        pair_key = normalise_asset_pair(asset_pair)
        y, m, d = partition_date.year, partition_date.month, partition_date.day
        filename = f"trades_{partition_date.strftime('%Y%m%d')}_{pair_key}.parquet"
        rel_path = (
            f"trades/year={y}/month={m:02d}/day={d:02d}"
            f"/asset_pair={pair_key}/{filename}"
        )
        abs_path = self.output_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        self._write_parquet_atomic(table, abs_path)

        sha256 = self._compute_file_hash(abs_path)
        size = abs_path.stat().st_size

        all_tokens = [r[1] for r in rows if r[1]]
        max_token = max(all_tokens) if all_tokens else ""

        return PartitionResult(
            partition_date=partition_date,
            asset_pair_raw=asset_pair,
            asset_pair_key=pair_key,
            relative_path=rel_path,
            records=len(rows),
            size_bytes=size,
            sha256=sha256,
            max_paging_token=max_token,
            exported_at=datetime.now(tz=timezone.utc),
        )

    def _fetch_rows(self, partition_date: date, asset_pair: str) -> list[tuple]:
        """Fetch raw SQLite rows for one partition.

        Returns rows whose column order matches :data:`PARQUET_SCHEMA_FIELDS`.
        All values come through parameterised queries — ``asset_pair`` is
        never string-interpolated into the SQL.
        """
        cur = self.db_conn.execute(
            """
            SELECT
                trade_id,
                paging_token,
                ledger_close_time,
                base_account,
                counter_account,
                base_amount,
                counter_amount,
                CASE WHEN base_asset_issuer IS NULL THEN 'native' ELSE 'credit' END,
                base_asset_code,
                COALESCE(base_asset_issuer, ''),
                CASE WHEN counter_asset_issuer IS NULL THEN 'native' ELSE 'credit' END,
                counter_asset_code,
                COALESCE(counter_asset_issuer, ''),
                price,
                base_is_seller,
                trade_type
            FROM trades
            WHERE DATE(ledger_close_time) = ?
              AND (base_asset_code || '/' || counter_asset_code) = ?
            ORDER BY ledger_close_time ASC, paging_token ASC
            """,
            (partition_date.isoformat(), asset_pair),
        )
        return cur.fetchall()

    def _rows_to_table(self, rows: list[tuple], schema) -> "pa.Table":
        """Convert SQLite rows to a PyArrow Table using the Parquet schema.

        Type coercions applied:
        - ``ledger_close_time``: ISO-8601 string → ``pa.timestamp("us", tz="UTC")``
        - ``base_amount``, ``counter_amount``, ``price``: float/str →
          ``Decimal`` quantised to 7 decimal places → ``pa.decimal128(22, 7)``
        - ``base_is_seller``: int (0/1) → ``bool``
        - Nullable string fields: ``None`` → ``""``
        """
        import pyarrow as pa  # noqa: PLC0415
        from decimal import Decimal  # noqa: PLC0415

        cols: dict[str, list] = {f: [] for f in PARQUET_SCHEMA_FIELDS}

        for row in rows:
            (
                trade_id, paging_token, ledger_close_time,
                base_account, counter_account,
                base_amount, counter_amount,
                base_asset_type, base_asset_code, base_asset_issuer,
                counter_asset_type, counter_asset_code, counter_asset_issuer,
                price, base_is_seller, trade_type,
            ) = row

            # Timestamp normalisation
            if isinstance(ledger_close_time, str):
                ts = datetime.fromisoformat(ledger_close_time.replace("Z", "+00:00"))
            else:
                ts = ledger_close_time
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            q = Decimal("0.0000001")
            cols["id"].append(str(trade_id) if trade_id else "")
            cols["paging_token"].append(str(paging_token) if paging_token else "")
            cols["ledger_close_time"].append(ts)
            cols["base_account"].append(str(base_account) if base_account else "")
            cols["counter_account"].append(str(counter_account) if counter_account else "")
            cols["base_amount"].append(Decimal(str(base_amount)).quantize(q))
            cols["counter_amount"].append(Decimal(str(counter_amount)).quantize(q))
            cols["base_asset_type"].append(str(base_asset_type) if base_asset_type else "")
            cols["base_asset_code"].append(str(base_asset_code) if base_asset_code else "")
            cols["base_asset_issuer"].append(str(base_asset_issuer) if base_asset_issuer else "")
            cols["counter_asset_type"].append(str(counter_asset_type) if counter_asset_type else "")
            cols["counter_asset_code"].append(str(counter_asset_code) if counter_asset_code else "")
            cols["counter_asset_issuer"].append(str(counter_asset_issuer) if counter_asset_issuer else "")
            cols["price"].append(Decimal(str(price)).quantize(q))
            cols["base_is_seller"].append(bool(base_is_seller))
            cols["trade_type"].append(str(trade_type) if trade_type else "orderbook")

        arrays = [pa.array(cols[f], type=schema.field(f).type) for f in PARQUET_SCHEMA_FIELDS]
        return pa.table(arrays, schema=schema)

    def _write_parquet_atomic(self, table, path: Path) -> None:
        """Write a PyArrow Table to ``path`` atomically with ``0o600`` permissions.

        Writes to a sibling temp file, restricts permissions to owner-only,
        then replaces the target via :func:`os.replace` so readers never see
        a partial file.
        """
        import pyarrow.parquet as pq  # noqa: PLC0415

        tmp_fd, tmp_str = tempfile.mkstemp(dir=path.parent, suffix=".parquet.tmp")
        tmp_path = Path(tmp_str)
        try:
            os.close(tmp_fd)
            os.chmod(tmp_path, 0o600)
            pq.write_table(
                table,
                tmp_path,
                compression=self.compression,
                row_group_size=self.row_group_size,
            )
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def _load_manifest(self) -> dict:
        """Load the existing manifest, returning ``{}`` on any failure."""
        if not self._manifest_path.exists():
            return {}
        try:
            raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read manifest.json (%s); starting fresh", exc)
            return {}

    def _write_manifest(
        self,
        results: list[PartitionResult],
        existing_manifest: dict | None = None,
    ) -> None:
        """Atomically write or update ``manifest.json``.

        Merges newly exported partitions into the existing manifest so that
        partitions skipped by delta detection retain their previous entries.
        The ``partition_index`` auxiliary dict is keyed by
        ``"{date}/{pair_key}"`` for fast O(1) delta lookups on subsequent
        runs.

        The file is written to a sibling temp path then :func:`os.replace`-d
        so concurrent readers never see a partial manifest.  Permissions are
        set to ``0o600`` before and after the rename.

        Args:
            results: Newly exported partitions from the current run.
            existing_manifest: Previously loaded manifest (may be ``None``).
        """
        existing = existing_manifest or {}
        # Key existing partition entries by relative path for O(1) merge
        existing_parts: dict[str, dict] = {
            p["path"]: p for p in existing.get("partitions", [])
        }
        existing_index: dict[str, dict] = dict(existing.get("partition_index", {}))

        for r in results:
            existing_parts[r.relative_path] = {
                "path": r.relative_path,
                "records": r.records,
                "size_bytes": r.size_bytes,
                "sha256": r.sha256,
                "max_paging_token": r.max_paging_token,
                "exported_at": r.exported_at.isoformat(),
            }
            key = f"{r.partition_date}/{r.asset_pair_key}"
            existing_index[key] = {
                "records": r.records,
                "max_paging_token": r.max_paging_token,
            }

        all_parts = list(existing_parts.values())
        manifest = {
            "schema_version": self._SCHEMA_VERSION,
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "total_records": sum(p["records"] for p in all_parts),
            "partitions": all_parts,
            "partition_index": existing_index,
        }

        tmp_fd, tmp_str = tempfile.mkstemp(dir=self.output_dir, suffix=".manifest.tmp")
        tmp_path = Path(tmp_str)
        try:
            os.close(tmp_fd)
            os.chmod(tmp_path, 0o600)
            tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp_path, self._manifest_path)
            os.chmod(self._manifest_path, 0o600)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_file_hash(self, path: Path) -> str:
        """Return the hex-encoded SHA-256 digest of ``path``.

        Reads in 64 KiB chunks to avoid loading large Parquet files into
        memory.  Always called *after* the file write is complete.

        Args:
            path: Absolute path to an existing file.

        Returns:
            Lowercase hex SHA-256 digest string.
        """
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _iter_partitions(
        self,
        since: date | None,
        until: date | None,
        asset_pair: str | None,
    ) -> Iterator[tuple[date, str]]:
        """Yield ``(date, asset_pair_raw)`` combinations present in SQLite.

        Applies ``since`` / ``until`` date bounds and the optional
        ``asset_pair`` filter via parameterised SQL.

        Args:
            since: Earliest date (inclusive) or ``None``.
            until: Latest date (inclusive) or ``None``.
            asset_pair: Optional raw asset pair filter.

        Yields:
            ``(partition_date, raw_asset_pair)`` tuples in ascending order.
        """
        conditions: list[str] = []
        params: list = []

        if since is not None:
            conditions.append("DATE(ledger_close_time) >= ?")
            params.append(since.isoformat())
        if until is not None:
            conditions.append("DATE(ledger_close_time) <= ?")
            params.append(until.isoformat())
        if asset_pair is not None:
            conditions.append("(base_asset_code || '/' || counter_asset_code) = ?")
            params.append(asset_pair)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT DISTINCT
                DATE(ledger_close_time) AS partition_date,
                base_asset_code || '/' || counter_asset_code AS pair
            FROM trades
            {where}
            ORDER BY partition_date ASC, pair ASC
        """
        cur = self.db_conn.execute(sql, params)
        for date_str, pair_raw in cur.fetchall():
            try:
                yield date.fromisoformat(date_str), pair_raw
            except (ValueError, TypeError):
                logger.warning("Skipping row with unparseable date: %r", date_str)

    @staticmethod
    def _validate_output_dir(output_dir: Path) -> Path:
        """Validate that ``output_dir`` is within the project working directory.

        Resolves the path and checks it is a descendant of ``Path.cwd()``.
        Relative paths that escape via ``../../..`` sequences are rejected.

        Args:
            output_dir: Candidate output directory.

        Returns:
            Resolved absolute :class:`Path`.

        Raises:
            ValueError: If the resolved path escapes the project root.
        """
        cwd = Path.cwd().resolve()
        candidate = Path(output_dir).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
        try:
            candidate.relative_to(cwd)
        except ValueError as exc:
            raise ValueError(
                f"output_dir {output_dir!r} resolves to {candidate}, which is outside "
                "the application working directory. Use a path within the project root."
            ) from exc
        return candidate
