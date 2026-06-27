# Feature Store Cold-Tier Archival

LedgerLens persists scored feature vectors to the `feature_distribution_snapshots` SQLite table for PSI-based drift detection.  A two-tier storage architecture prevents unbounded growth while preserving full historical data for 60–90 day drift analysis and compliance auditing.

## Tiered Storage Architecture

```
Scoring pipeline
      │
      ▼
┌─────────────────────────────────────┐
│  Hot Tier: SQLite                   │
│  feature_distribution_snapshots     │
│  Recent data (< CUTOFF days)        │
│  Fast writes + ad-hoc queries       │
└───────────────┬─────────────────────┘
                │  archive_old_features()
                ▼
┌─────────────────────────────────────┐
│  Cold Tier: Parquet                 │
│  ./feature_archive/YYYY/MM/DD/      │
│  Historical data (≥ CUTOFF days)    │
│  Columnar, partition-pruned reads   │
└─────────────────────────────────────┘
                │
                └──────────────────────────┐
                                           ▼
                              DualTierFeatureStore.query()
                              (transparent, deduplicated)
```

Both tiers are exposed through `DualTierFeatureStore.query()`, which is the single interface used by `drift_monitor.py`.  Callers never need to know whether data resides in SQLite or Parquet.

## Parquet Partition Layout

Parquet files are partitioned by `year`, `month`, and `day`:

```
feature_archive/
├── year=2025/
│   ├── month=10/
│   │   └── day=1/
│   │       └── features.parquet
│   └── month=11/
│       └── day=15/
│           └── features.parquet
└── year=2026/
    └── month=1/
        └── day=10/
            └── features.parquet
```

Date-based partitioning allows PyArrow to skip irrelevant directories entirely when a `since`/`until` filter is applied, reducing I/O for time-range queries.

### Parquet Schema

| Column | Type | Notes |
|--------|------|-------|
| `wallet` | string | Stellar account ID |
| `asset_pair` | string | e.g. `XLM/USDC` |
| `feature_name` | string | Feature identifier |
| `feature_value` | float64 | Computed feature value |
| `recorded_at` | timestamp (us) | UTC timestamp of the snapshot |
| `year` | int32 | Partition column |
| `month` | int32 | Partition column |
| `day` | int32 | Partition column |

## Configuration

Set the following environment variables (or add them to `.env`):

```bash
# Directory for archived Parquet files (must be within the working directory)
FEATURE_ARCHIVE_DIR=./feature_archive

# Rows older than this many days are eligible for archival
FEATURE_ARCHIVE_CUTOFF_DAYS=30
```

The archive directory is created automatically on first use with mode **0o700** (owner-read-only) to protect sensitive feature vectors from other OS users.

## Archival Schedule

### Manual archival

```bash
python cli.py archive-features
# or with an explicit cutoff:
python cli.py archive-features --cutoff-days 45
```

### Automatic archival (recommended)

Archival runs automatically at the start of each `retrain-check`:

```bash
python cli.py retrain-check
```

### Cron example

```cron
# Archive daily at 03:00 UTC
0 3 * * * cd /opt/ledgerlens && python cli.py archive-features >> logs/archive.log 2>&1
```

## Key Classes

### `FeatureStoreArchiver`

Moves rows from `feature_distribution_snapshots` to Parquet.

```python
from pathlib import Path
from detection.feature_store import FeatureStoreArchiver

archiver = FeatureStoreArchiver(
    db_path="./ledgerlens.db",
    archive_dir=Path("./feature_archive"),
)
n_archived = archiver.archive_old_features(cutoff_days=30)
print(f"Archived {n_archived} rows")
```

### `ParquetFeatureColdTier`

Reads from the Parquet archive with optional filter pushdown.

```python
from pathlib import Path
from datetime import datetime, timezone, timedelta
from detection.feature_store import ParquetFeatureColdTier

cold = ParquetFeatureColdTier(archive_dir=Path("./feature_archive"))
df = cold.query(since=datetime.now(timezone.utc) - timedelta(days=90))
```

### `DualTierFeatureStore`

Unified interface combining both tiers.

```python
from pathlib import Path
from datetime import datetime, timezone, timedelta
from detection.feature_store import FeatureStore, ParquetFeatureColdTier, DualTierFeatureStore

hot  = FeatureStore()
cold = ParquetFeatureColdTier(Path("./feature_archive"))
store = DualTierFeatureStore(hot, cold)

df = store.query(since=datetime.now(timezone.utc) - timedelta(days=60))
```

### `drift_monitor.load_production_features`

Convenience wrapper for drift analysis:

```python
from detection.drift_monitor import load_production_features

df = load_production_features(store, since_days=60)
```

## Observability

The `GET /admin/feature-store/stats` endpoint (admin-key gated) returns current storage metrics:

```json
{
  "hot_tier_rows": 48320,
  "cold_tier_rows": 312000,
  "oldest_hot_record": "2026-05-28T00:00:00+00:00",
  "oldest_cold_record": "2025-10-01T00:00:00+00:00",
  "archive_dir_size_mb": 14.2
}
```

## Recovery Procedure for Failed Archives

A failed archive leaves data in **both** SQLite and Parquet.  `DualTierFeatureStore.query()` deduplicates by `(wallet, feature_name, recorded_at)` and logs a WARNING:

```
WARNING DualTierFeatureStore: removed N duplicate rows across hot/cold boundary
(indicates a previously failed archive run)
```

To repair:

1. Verify the Parquet files are intact: `python -c "import pyarrow.parquet as pq; print(pq.read_table('./feature_archive').schema)"`
2. Re-run archival: `python cli.py archive-features`
3. If the Parquet files are corrupt, delete the affected partitions and re-archive from SQLite (the rows are still present in SQLite until the delete step succeeds).

## Security

- The archive directory is created with mode **0o700** — readable only by the process owner.
- `FEATURE_ARCHIVE_DIR` is validated at startup: path traversal outside the working directory raises a `ValueError` and aborts startup.
- Feature vectors reveal LedgerLens's internal ML features per wallet.  Treat `feature_archive/` as sensitive data and apply the same access controls as the SQLite database.
