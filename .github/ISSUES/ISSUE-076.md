---
title: "Implement Feature Store Cold-Tier Archival to Parquet with Transparent Dual-Tier Retrieval"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/feature_store.py` to archive feature vectors older than 30 days from the SQLite hot tier to Parquet files on disk, partitioned by date (`YYYY/MM/DD`). Implement transparent dual-tier retrieval so `drift_monitor.py` can query historical features across both tiers using a unified interface — without needing to know whether the data resides in SQLite or Parquet. This resolves the storage growth problem identified in README (`feature_distribution_snapshots` hard cap: 500,000 rows) while preserving full historical data for drift analysis.

## Background & Context

LedgerLens persists scored feature vectors to the `feature_distribution_snapshots` SQLite table for drift monitoring. At 1,000 wallets/run × 4 runs/day × 30 days × 26 features × ~8 bytes ≈ 25MB, and with a hard cap of 500,000 rows (~50MB), the hot-tier SQLite store retains approximately 30 days of data before oldest rows are pruned.

This pruning discards potentially useful historical data for:
1. **Drift trend analysis**: detecting gradual drift over 60–90 days requires more than 30 days of feature history.
2. **Model provenance**: understanding which feature distribution a specific model version was trained on requires the feature snapshot from the training date, which may be older than 30 days.
3. **Compliance audit**: storing feature snapshots for >90 days may be required for regulatory compliance in some jurisdictions.

The solution is a two-tier storage architecture:
- **Hot tier (SQLite)**: recent data (<30 days), optimised for fast writes from the scoring pipeline and ad-hoc queries.
- **Cold tier (Parquet)**: archived data (≥30 days), stored as columnar Parquet files partitioned by date, optimised for batch analytical reads.

The cold tier uses `pyarrow` for Parquet write/read and `pandas` for query-level merge. The hot-tier SQLite cap is raised or removed once the archival job is running.

## Objectives

- [ ] Implement `FeatureStoreArchiver` class in `detection/feature_store.py` with method `archive_old_features(cutoff_days=30)` that reads rows older than `cutoff_days` from `feature_distribution_snapshots`, writes them to Parquet, and deletes them from SQLite.
- [ ] Partition Parquet files by date: path format `{ARCHIVE_DIR}/{YYYY}/{MM}/{DD}/features.parquet`.
- [ ] Use `pyarrow.parquet.write_to_dataset` with `partition_cols=["year", "month", "day"]` for efficient partition pruning.
- [ ] Implement `FeatureStore.query(wallet=None, feature_name=None, since=None, until=None) -> pd.DataFrame` that merges results from both SQLite (hot) and Parquet (cold) tiers transparently.
- [ ] `query()` must never return duplicate rows when data spans the archival boundary.
- [ ] Implement `DualTierFeatureStore` class that wraps both `FeatureStore` (hot) and `ParquetFeatureColdTier` (cold) and exposes the unified `query()` interface.
- [ ] Add `cli.py archive-features` command that runs `FeatureStoreArchiver.archive_old_features()`.
- [ ] Schedule archival in `cli.py retrain-check` — run archival at the start of each retrain check.
- [ ] Expose `GET /admin/feature-store/stats` returning hot-tier row count, cold-tier row count, oldest record timestamp, and archive directory size.
- [ ] All new code covered by tests; ≥90% branch coverage.

## Technical Requirements

### `FeatureStoreArchiver` class

```python
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

class FeatureStoreArchiver:
    PARQUET_SCHEMA = pa.schema([
        pa.field("wallet", pa.string()),
        pa.field("asset_pair", pa.string()),
        pa.field("feature_name", pa.string()),
        pa.field("feature_value", pa.float64()),
        pa.field("recorded_at", pa.timestamp("us")),
        pa.field("year", pa.int32()),
        pa.field("month", pa.int32()),
        pa.field("day", pa.int32()),
    ])

    def __init__(self, db_path: str, archive_dir: Path):
        self.db_path = db_path
        self.archive_dir = archive_dir
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def archive_old_features(self, cutoff_days: int = 30) -> int:
        """
        Archive rows older than cutoff_days to Parquet cold tier.
        Returns number of rows archived.
        Uses SQLite transaction: write Parquet first; delete from SQLite only on success.
        """
        cutoff = datetime.utcnow() - timedelta(days=cutoff_days)
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM feature_distribution_snapshots WHERE recorded_at < ?",
                conn, params=[cutoff.isoformat()]
            )
        if df.empty:
            return 0
        # Add partition columns
        df["year"] = df["recorded_at"].dt.year
        df["month"] = df["recorded_at"].dt.month
        df["day"] = df["recorded_at"].dt.day
        # Write to Parquet (append to existing partition if present)
        table = pa.Table.from_pandas(df, schema=self.PARQUET_SCHEMA)
        pq.write_to_dataset(
            table,
            root_path=str(self.archive_dir),
            partition_cols=["year", "month", "day"],
            existing_data_behavior="overwrite_or_ignore",
        )
        # Delete from SQLite only after successful Parquet write
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM feature_distribution_snapshots WHERE recorded_at < ?",
                [cutoff.isoformat()]
            )
        return len(df)
```

### `ParquetFeatureColdTier` class

```python
class ParquetFeatureColdTier:
    def __init__(self, archive_dir: Path):
        self.archive_dir = archive_dir

    def query(
        self,
        wallet: Optional[str] = None,
        feature_name: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """
        Read from Parquet cold tier with partition pruning.
        Applies date-range partition pruning before column-level filtering.
        Returns empty DataFrame if archive_dir does not exist.
        """
        if not self.archive_dir.exists():
            return pd.DataFrame()
        filters = self._build_parquet_filters(since, until, wallet, feature_name)
        try:
            dataset = pq.read_table(
                str(self.archive_dir),
                filters=filters,
                columns=["wallet", "asset_pair", "feature_name", "feature_value", "recorded_at"],
            )
            return dataset.to_pandas()
        except Exception:
            return pd.DataFrame()

    def _build_parquet_filters(self, since, until, wallet, feature_name):
        """Build PyArrow partition filter expressions for efficient pruning."""
        filters = []
        if since:
            filters.append(("recorded_at", ">=", since))
        if until:
            filters.append(("recorded_at", "<=", until))
        if wallet:
            filters.append(("wallet", "=", wallet))
        if feature_name:
            filters.append(("feature_name", "=", feature_name))
        return filters if filters else None
```

### `DualTierFeatureStore` class

```python
class DualTierFeatureStore:
    def __init__(self, hot: "FeatureStore", cold: ParquetFeatureColdTier):
        self._hot = hot
        self._cold = cold

    def query(
        self,
        wallet: Optional[str] = None,
        feature_name: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """
        Merge hot (SQLite) and cold (Parquet) results.
        Deduplicates by (wallet, feature_name, recorded_at) in case data straddles
        the archival boundary during a concurrent archive run.
        """
        hot_df = self._hot.query(wallet=wallet, feature_name=feature_name, since=since, until=until)
        cold_df = self._cold.query(wallet=wallet, feature_name=feature_name, since=since, until=until)
        combined = pd.concat([hot_df, cold_df], ignore_index=True)
        return combined.drop_duplicates(subset=["wallet", "feature_name", "recorded_at"])
```

### Integration with `drift_monitor.py`

```python
# drift_monitor.py: replace direct FeatureStore with DualTierFeatureStore
def load_production_features(
    store: DualTierFeatureStore,
    since_days: int = 30,
) -> pd.DataFrame:
    since = datetime.utcnow() - timedelta(days=since_days)
    return store.query(since=since)
```

### Configuration

```
FEATURE_ARCHIVE_DIR=./feature_archive
FEATURE_ARCHIVE_CUTOFF_DAYS=30
```

### API endpoint

```python
@router.get("/admin/feature-store/stats", response_model=FeatureStoreStats)
async def feature_store_stats(...):
    ...

class FeatureStoreStats(BaseModel):
    hot_tier_rows: int
    cold_tier_rows: int
    oldest_hot_record: Optional[datetime]
    oldest_cold_record: Optional[datetime]
    archive_dir_size_mb: float
```

## Security Considerations

- Parquet files contain feature vectors which reveal LedgerLens's internal ML features for each wallet. Treat the archive directory as sensitive data: set directory permissions to `700` (owner-read-only) on creation.
- The `archive_old_features` method must not partially archive: if the Parquet write succeeds but the SQLite delete fails (e.g., due to a disk-full error), the data will exist in both tiers. The `DualTierFeatureStore.query()` deduplication handles this gracefully — but log a WARNING when deduplication removes >0 rows (indicates a previously failed archive).
- Archive directory path (`FEATURE_ARCHIVE_DIR`) must be validated to be within the application's working directory or an explicitly configured path. Do not allow path traversal via the env variable.

## Testing Requirements

- **Unit — `archive_old_features` moves rows correctly**: insert rows with timestamps 40 days ago and 10 days ago; run archive with `cutoff_days=30`; assert old rows removed from SQLite; assert Parquet file created at correct path.
- **Unit — `archive_old_features` Parquet-before-delete atomicity**: mock Parquet write to fail; assert no rows deleted from SQLite.
- **Unit — `ParquetFeatureColdTier.query` partition pruning**: assert only partitions within date range are read (mock `pq.read_table` and verify filter arguments).
- **Unit — `DualTierFeatureStore.query` deduplication**: insert same row in both hot and cold; assert query returns only one row.
- **Unit — `DualTierFeatureStore.query` spans archival boundary**: data from 45 days ago (cold) + 10 days ago (hot); assert both returned without duplicates.
- **Unit — archive directory permissions**: assert `feature_archive/` is created with mode 0o700.
- **Integration — drift monitor compatibility**: run `load_production_features` with `DualTierFeatureStore`; assert returns DataFrame with expected columns.
- **Integration — `GET /admin/feature-store/stats`**: assert response contains `hot_tier_rows`, `cold_tier_rows`, `archive_dir_size_mb`.
- **Integration — `cli.py archive-features`**: run command; assert archived rows removed from SQLite; assert Parquet files created.

## Documentation Requirements

- Docstrings on `FeatureStoreArchiver`, `ParquetFeatureColdTier`, and `DualTierFeatureStore`.
- Update `README.md` Continuous Retraining section: replace mention of 500,000-row hard cap with description of archival pipeline.
- New file `docs/feature_store_archival.md` covering: tiered storage architecture, Parquet partition layout, archival schedule, and recovery procedure for failed archives.
- Document `FEATURE_ARCHIVE_DIR` and `FEATURE_ARCHIVE_CUTOFF_DAYS` in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `FeatureStoreArchiver.archive_old_features()` implemented with Parquet write and SQLite delete.
- [ ] `ParquetFeatureColdTier.query()` with partition pruning implemented.
- [ ] `DualTierFeatureStore.query()` merges both tiers with deduplication.
- [ ] `drift_monitor.py` uses `DualTierFeatureStore` instead of direct `FeatureStore`.
- [ ] `cli.py archive-features` command operational.
- [ ] `cli.py retrain-check` runs archival at start.
- [ ] Archive directory created with `0o700` permissions.
- [ ] `GET /admin/feature-store/stats` endpoint operational.
- [ ] All unit and integration tests pass; ≥90% branch coverage.
- [ ] `docs/feature_store_archival.md` written.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience designing tiered storage architectures — specifically hot/cold data lifecycle management, Parquet file layout optimisation (partition pruning), and transparent dual-source query abstractions. Familiarity with `pyarrow`, `pandas`, and SQLite is required. Experience with columnar storage formats, partitioned datasets, and partition-pruning query strategies is highly valued. Understanding of LedgerLens's drift monitoring requirements will inform good design decisions around partition granularity and query performance.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., data engineering, tiered storage, Parquet/columnar formats, Python backend).
2. **Relevant experience**: cold-tier archival pipelines, Parquet dataset management, or hot/cold tiered storage systems you have built.
3. **Approach / thoughts**: would you use daily or monthly partitioning for the Parquet cold tier given LedgerLens's ~4 runs/day write rate? What is the optimal retention policy for the hot tier given the 30-day drift monitoring window?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
