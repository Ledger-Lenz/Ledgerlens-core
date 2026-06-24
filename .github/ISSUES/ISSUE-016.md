---
title: "Implement Incremental Snapshot Export of Raw Trade Data to Parquet"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
`ingestion/historical_loader.py` ingests raw trade data into SQLite but provides no mechanism to export that data to the `ledgerlens-data` repository in a format suitable for long-term storage, versioned dataset management, and ML training. Implementing incremental Parquet snapshot exports — partitioned by date and asset pair, with delta detection and a checksum manifest — will enable `ledgerlens-core` to populate `ledgerlens-data` with auditable, reproducible training datasets that other tools can consume without a running SQLite instance.

## Background & Context
The README describes `ledgerlens-data` as the canonical storage for raw and processed trade data and labelled training datasets. `ledgerlens-core`'s `ingestion/historical_loader.py` reads from (or writes new snapshots to) this repo. Currently, the integration between the SQLite store in `core` and the dataset files in `ledgerlens-data` is listed as an "Open Integration Point (not yet implemented)".

Parquet is the standard columnar storage format for ML training data: it supports efficient predicate pushdown, column pruning, and compression, and is natively supported by Pandas, PyArrow, and HuggingFace Datasets. Partitioning by `(date, asset_pair)` allows incremental exports — only changed partitions need to be re-exported after each pipeline run.

The export must produce a checksum manifest (`manifest.json`) that records the SHA-256 hash of each Parquet file. This allows `ledgerlens-data` to detect corruption and allows `ledgerlens-core`'s training pipeline to verify dataset integrity before training.

The `detection/model_training.py` pipeline should be updated to optionally load training data from Parquet (exported by this feature) instead of re-generating synthetic data, enabling training on real historical data once `ledgerlens-data` is populated.

## Objectives
- [ ] Implement `ParquetExporter` in `ingestion/historical_loader.py` (or a new `ingestion/parquet_exporter.py` module) that exports `Trade` records from SQLite to Parquet files partitioned by `(year, month, day, asset_pair)`.
- [ ] Implement delta detection: compare the current partition's record count and max `paging_token` against the last exported manifest; only re-export partitions that have changed.
- [ ] Generate a `manifest.json` in the export root directory that records file paths, record counts, SHA-256 hashes, and export timestamps for all exported partitions.
- [ ] Add a `cli.py export-parquet` sub-command with `--output-dir`, `--since`, `--until`, `--asset-pair`, and `--force` flags.

## Technical Requirements

**Parquet partition directory structure:**
```
<output_dir>/
├── manifest.json
├── trades/
│   ├── year=2026/
│   │   ├── month=06/
│   │   │   ├── day=01/
│   │   │   │   ├── asset_pair=XLM_USDC/
│   │   │   │   │   └── trades_20260601_XLM_USDC.parquet
│   │   │   │   └── asset_pair=XLM_BTC/
│   │   │   │       └── trades_20260601_XLM_BTC.parquet
```

**Parquet schema** — the Parquet schema must match `Trade` model field names exactly to maintain the shared contract with `ledgerlens-data`:
```python
PARQUET_SCHEMA = pa.schema([
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
```

**`ParquetExporter` interface:**
```python
class ParquetExporter:
    def __init__(
        self,
        db_conn: sqlite3.Connection,
        output_dir: Path,
        compression: str = "snappy",       # "snappy" | "zstd" | "gzip" | "none"
        row_group_size: int = 100_000,
    ): ...

    def export(
        self,
        since: date | None = None,
        until: date | None = None,
        asset_pair: str | None = None,
        force: bool = False,
    ) -> ExportResult: ...

    def _export_partition(
        self,
        partition_date: date,
        asset_pair: str,
        force: bool,
    ) -> PartitionResult | None:
        """
        Returns None if partition is unchanged (delta check passed).
        """

    def _compute_file_hash(self, path: Path) -> str:
        """SHA-256 of file contents, hex-encoded."""

    def _write_manifest(self, results: list[PartitionResult]) -> None:
        """Atomically write/update manifest.json."""
```

**`manifest.json` format:**
```json
{
  "schema_version": "1.0",
  "exported_at": "2026-06-24T09:00:00Z",
  "total_records": 1500000,
  "partitions": [
    {
      "path": "trades/year=2026/month=06/day=01/asset_pair=XLM_USDC/trades_20260601_XLM_USDC.parquet",
      "records": 45000,
      "size_bytes": 2345678,
      "sha256": "abc123...",
      "max_paging_token": "50123456-200",
      "exported_at": "2026-06-24T09:00:00Z"
    }
  ]
}
```

**Delta detection algorithm:**
```python
def _is_partition_changed(
    self,
    partition_date: date,
    asset_pair: str,
    manifest: dict,
) -> bool:
    """
    Query SQLite: SELECT COUNT(*), MAX(paging_token) for this partition.
    Compare with manifest entry. Return True if changed or not in manifest.
    """
    cursor = self.db_conn.execute(
        "SELECT COUNT(*), MAX(paging_token) FROM trades "
        "WHERE DATE(ledger_close_time) = ? AND base_asset_code || '/' || counter_asset_code = ?",
        (partition_date.isoformat(), asset_pair),
    )
    count, max_token = cursor.fetchone()
    key = f"{partition_date}/{asset_pair}"
    if key not in manifest.get("partition_index", {}):
        return True
    prev = manifest["partition_index"][key]
    return prev["records"] != count or prev["max_paging_token"] != max_token
```

**`ExportResult` dataclass:**
```python
@dataclass
class ExportResult:
    total_partitions: int
    exported_partitions: int
    skipped_partitions: int         # unchanged (delta check)
    total_records_exported: int
    total_size_bytes: int
    duration_seconds: float
    manifest_path: Path
```

**Asset pair normalisation**: the partition key `asset_pair` must normalise asset codes to avoid filesystem issues: replace `/` with `_`, uppercase, and replace non-alphanumeric characters with `_`. E.g., `"XLM/USDC"` → `"XLM_USDC"`.

**PyArrow version requirement**: pin `pyarrow>=14.0` in `requirements.txt` for Decimal128 support with 7 decimal places (matching Stellar's amount precision).

**Performance target**: exporting 1 million `Trade` records to Parquet (Snappy compression) should complete in < 60 seconds.

## Security Considerations
- The `output_dir` must be validated to be an absolute path or a path within the project directory — relative paths that escape the project root (e.g., `../../etc`) must be rejected.
- Parquet files must not be world-readable on creation — use `umask(0o077)` or equivalent to create files with `0o600` permissions.
- The manifest `sha256` field is used for integrity verification — ensure the hash is computed over the final file contents after all writes are complete (not during write).
- SQL queries for partition export must use parameterised statements to prevent SQL injection from attacker-controlled `asset_pair` strings passed via CLI.
- Do not include raw wallet addresses or account IDs in the manifest or any export metadata — only aggregate statistics (record count, max paging token).

## Testing Requirements
- Unit tests covering `_compute_file_hash()`: deterministic hash for known content, different content → different hash
- Unit tests covering `_is_partition_changed()`: no manifest entry (True), manifest matches SQLite (False), count differs (True), max_paging_token differs (True)
- Unit tests covering `_export_partition()`: mock SQLite returning 100 trades; assert Parquet file created with correct schema and row count
- Unit tests covering `_write_manifest()`: atomic write, manifest contains correct partition entries
- Unit tests covering asset pair normalisation: `"XLM/USDC"` → `"XLM_USDC"`, edge cases with special characters
- Integration tests: insert 500 synthetic `Trade` records into SQLite; run `ParquetExporter.export()`; read back with PyArrow; assert field values match
- Integration tests: run export twice — second run skips unchanged partitions; assert `ExportResult.skipped_partitions > 0`
- Integration tests: run `cli.py export-parquet --output-dir /tmp/test_export`; assert directory structure and manifest created
- Edge cases: empty partition (0 records), all records in one partition, `force=True` overrides delta check
- Performance benchmark: 1M records exported in < 60 seconds with Snappy compression

## Documentation Requirements
- Update `README.md` CLI Reference with `python cli.py export-parquet` flags
- Update the LedgerLens Organization > Data Flow section to describe how `export-parquet` populates `ledgerlens-data`
- Add docstrings to `ParquetExporter`, `_export_partition`, `_is_partition_changed`, and `_write_manifest`
- Create or update `docs/data-export.md` documenting the Parquet schema, partition structure, manifest format, and how to verify dataset integrity using the SHA-256 hashes

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: PyArrow, Parquet, Hive-style partitioning, incremental data exports, SQLite to Parquet pipelines, Pandas
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Data engineer with hands-on experience building Parquet export pipelines from relational stores. Familiarity with PyArrow's schema and batch writer APIs, Hive-style directory partitioning conventions, and Decimal128 precision requirements for financial data is essential.
