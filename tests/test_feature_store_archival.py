"""Tests for Feature Store cold-tier archival and dual-tier retrieval.

Covers:
- FeatureStoreArchiver.archive_old_features(): correct row movement, atomicity
- ParquetFeatureColdTier.query(): partition-pruned reads
- DualTierFeatureStore.query(): merged results, deduplication
- Archive directory permissions (0o700)
- load_production_features() integration
- GET /admin/feature-store/stats endpoint
- cli.py archive-features command
"""

import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import pyarrow.parquet as pq

from detection.feature_store import (
    DualTierFeatureStore,
    FeatureStore,
    FeatureStoreArchiver,
    ParquetFeatureColdTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_snapshot_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feature_distribution_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                asset_pair TEXT NOT NULL,
                feature_name TEXT NOT NULL,
                feature_value REAL NOT NULL,
                recorded_at TIMESTAMP NOT NULL
            )
            """
        )


def _insert_row(
    db_path: str,
    wallet: str,
    feature_name: str,
    feature_value: float,
    recorded_at: datetime,
    asset_pair: str = "XLM/USDC",
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO feature_distribution_snapshots "
            "(wallet, asset_pair, feature_name, feature_value, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (wallet, asset_pair, feature_name, feature_value, recorded_at.isoformat()),
        )


def _count_rows(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM feature_distribution_snapshots"
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# FeatureStoreArchiver
# ---------------------------------------------------------------------------


class TestFeatureStoreArchiver:
    def test_moves_old_rows_to_parquet(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        old_ts = datetime.utcnow() - timedelta(days=40)
        recent_ts = datetime.utcnow() - timedelta(days=10)

        _insert_row(db_path, "GA_OLD", "benford_mad_1h", 0.01, old_ts)
        _insert_row(db_path, "GA_NEW", "benford_mad_1h", 0.02, recent_ts)

        archiver = FeatureStoreArchiver(db_path=db_path, archive_dir=archive_dir)
        n = archiver.archive_old_features(cutoff_days=30)

        assert n == 1
        # Old row removed from SQLite
        assert _count_rows(db_path) == 1
        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute(
                "SELECT wallet FROM feature_distribution_snapshots"
            ).fetchall()
        assert remaining[0][0] == "GA_NEW"

        # Parquet file created somewhere under archive_dir
        parquet_files = list(archive_dir.rglob("*.parquet"))
        assert len(parquet_files) >= 1

    def test_returns_zero_when_nothing_to_archive(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        recent_ts = datetime.utcnow() - timedelta(days=5)
        _insert_row(db_path, "GA1", "f1", 0.5, recent_ts)

        archiver = FeatureStoreArchiver(db_path=db_path, archive_dir=archive_dir)
        assert archiver.archive_old_features(cutoff_days=30) == 0
        assert _count_rows(db_path) == 1

    def test_parquet_partitioned_by_date(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        old_ts = datetime(2025, 3, 15, 12, 0, 0)  # specific date for path check
        _insert_row(db_path, "GA1", "f1", 1.0, old_ts)

        archiver = FeatureStoreArchiver(db_path=db_path, archive_dir=archive_dir)
        archiver.archive_old_features(cutoff_days=1)

        # Partition path should contain year=2025/month=3/day=15
        partition_dirs = [str(p) for p in archive_dir.rglob("*") if p.is_dir()]
        assert any("year=2025" in d for d in partition_dirs)
        assert any("month=3" in d for d in partition_dirs)
        assert any("day=15" in d for d in partition_dirs)

    def test_atomicity_no_delete_on_parquet_failure(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        old_ts = datetime.utcnow() - timedelta(days=40)
        _insert_row(db_path, "GA1", "f1", 1.0, old_ts)

        archiver = FeatureStoreArchiver(db_path=db_path, archive_dir=archive_dir)

        with patch("pyarrow.parquet.write_to_dataset", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                archiver.archive_old_features(cutoff_days=30)

        # Row must still be in SQLite
        assert _count_rows(db_path) == 1

    def test_archive_dir_created_with_0o700(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        FeatureStoreArchiver(db_path=db_path, archive_dir=archive_dir)

        assert archive_dir.exists()
        mode = stat.S_IMODE(archive_dir.stat().st_mode)
        assert mode == 0o700

    def test_multiple_rows_archived(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        old_ts = datetime.utcnow() - timedelta(days=45)
        for i in range(5):
            _insert_row(db_path, f"GA{i}", "f1", float(i), old_ts)
        # One recent row
        _insert_row(db_path, "GA_RECENT", "f1", 99.0, datetime.utcnow() - timedelta(days=5))

        archiver = FeatureStoreArchiver(db_path=db_path, archive_dir=archive_dir)
        n = archiver.archive_old_features(cutoff_days=30)

        assert n == 5
        assert _count_rows(db_path) == 1


# ---------------------------------------------------------------------------
# ParquetFeatureColdTier
# ---------------------------------------------------------------------------


class TestParquetFeatureColdTier:
    def _write_parquet(self, archive_dir: Path, rows: list[dict]) -> None:
        df = pd.DataFrame(rows)
        df["recorded_at"] = pd.to_datetime(df["recorded_at"], utc=True)
        df["year"] = df["recorded_at"].dt.year.astype("int32")
        df["month"] = df["recorded_at"].dt.month.astype("int32")
        df["day"] = df["recorded_at"].dt.day.astype("int32")
        import pyarrow as pa
        table = pa.Table.from_pandas(df)
        pq.write_to_dataset(
            table,
            root_path=str(archive_dir),
            partition_cols=["year", "month", "day"],
            existing_data_behavior="overwrite_or_ignore",
        )

    def test_returns_empty_when_no_archive(self, tmp_path: Path) -> None:
        cold = ParquetFeatureColdTier(tmp_path / "nonexistent")
        assert cold.query().empty

    def test_reads_all_rows_without_filters(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        rows = [
            {"wallet": "GA1", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 0.5, "recorded_at": "2025-01-10T00:00:00Z"},
            {"wallet": "GA2", "asset_pair": "XLM/USDC", "feature_name": "f2",
             "feature_value": 1.5, "recorded_at": "2025-02-20T00:00:00Z"},
        ]
        self._write_parquet(archive_dir, rows)

        cold = ParquetFeatureColdTier(archive_dir)
        df = cold.query()
        assert len(df) == 2

    def test_filter_by_wallet(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        rows = [
            {"wallet": "GA1", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 0.5, "recorded_at": "2025-01-10T00:00:00Z"},
            {"wallet": "GA2", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 1.5, "recorded_at": "2025-01-10T00:00:00Z"},
        ]
        self._write_parquet(archive_dir, rows)

        cold = ParquetFeatureColdTier(archive_dir)
        df = cold.query(wallet="GA1")
        assert len(df) == 1
        assert df.iloc[0]["wallet"] == "GA1"

    def test_filter_by_since(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        rows = [
            {"wallet": "GA1", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 0.5, "recorded_at": "2025-01-10T00:00:00Z"},
            {"wallet": "GA1", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 1.5, "recorded_at": "2025-06-01T00:00:00Z"},
        ]
        self._write_parquet(archive_dir, rows)

        cold = ParquetFeatureColdTier(archive_dir)
        since = datetime(2025, 3, 1, tzinfo=timezone.utc)
        df = cold.query(since=since)
        assert len(df) == 1
        assert df.iloc[0]["feature_value"] == 1.5

    def test_returns_empty_on_read_error(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()  # exists but is empty
        cold = ParquetFeatureColdTier(archive_dir)
        # Should not raise; empty dir returns empty DF
        df = cold.query()
        assert isinstance(df, pd.DataFrame)

    def test_parquet_filters_called_with_args(self, tmp_path: Path) -> None:
        """Verify _build_parquet_filters produces correct filter tuples."""
        cold = ParquetFeatureColdTier(tmp_path / "archive")
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        until = datetime(2025, 12, 31, tzinfo=timezone.utc)

        filters = cold._build_parquet_filters(since, until, "GA1", "f1")

        assert filters is not None
        filter_cols = {f[0] for f in filters}
        assert "recorded_at" in filter_cols
        assert "wallet" in filter_cols
        assert "feature_name" in filter_cols

    def test_no_filters_returns_none(self, tmp_path: Path) -> None:
        cold = ParquetFeatureColdTier(tmp_path / "archive")
        assert cold._build_parquet_filters(None, None, None, None) is None

    def test_row_count(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        rows = [
            {"wallet": "GA1", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 0.5, "recorded_at": "2025-01-10T00:00:00Z"},
            {"wallet": "GA2", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 1.5, "recorded_at": "2025-01-11T00:00:00Z"},
        ]
        self._write_parquet(archive_dir, rows)
        cold = ParquetFeatureColdTier(archive_dir)
        assert cold.row_count() == 2

    def test_oldest_record(self, tmp_path: Path) -> None:
        archive_dir = tmp_path / "archive"
        rows = [
            {"wallet": "GA1", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 0.5, "recorded_at": "2025-01-10T00:00:00Z"},
            {"wallet": "GA2", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 1.5, "recorded_at": "2025-06-01T00:00:00Z"},
        ]
        self._write_parquet(archive_dir, rows)
        cold = ParquetFeatureColdTier(archive_dir)
        oldest = cold.oldest_record()
        assert oldest is not None
        assert oldest.year == 2025
        assert oldest.month == 1
        assert oldest.day == 10


# ---------------------------------------------------------------------------
# FeatureStore.query() (hot-tier)
# ---------------------------------------------------------------------------


class TestFeatureStoreQuery:
    def test_query_returns_all_rows(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        _init_snapshot_table(db_path)
        ts = datetime.utcnow() - timedelta(days=5)
        _insert_row(db_path, "GA1", "f1", 0.1, ts)
        _insert_row(db_path, "GA2", "f2", 0.2, ts)

        fs = FeatureStore(redis_url=None)
        df = fs.query(db_path=db_path)
        assert len(df) == 2

    def test_query_filter_wallet(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        _init_snapshot_table(db_path)
        ts = datetime.utcnow() - timedelta(days=5)
        _insert_row(db_path, "GA1", "f1", 0.1, ts)
        _insert_row(db_path, "GA2", "f1", 0.2, ts)

        fs = FeatureStore(redis_url=None)
        df = fs.query(wallet="GA1", db_path=db_path)
        assert len(df) == 1
        assert df.iloc[0]["wallet"] == "GA1"

    def test_query_filter_since(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        _init_snapshot_table(db_path)
        old_ts = datetime.utcnow() - timedelta(days=40)
        recent_ts = datetime.utcnow() - timedelta(days=5)
        _insert_row(db_path, "GA1", "f1", 0.1, old_ts)
        _insert_row(db_path, "GA2", "f1", 0.2, recent_ts)

        fs = FeatureStore(redis_url=None)
        since = datetime.utcnow() - timedelta(days=10)
        df = fs.query(since=since, db_path=db_path)
        assert len(df) == 1
        assert df.iloc[0]["wallet"] == "GA2"

    def test_query_returns_empty_on_missing_table(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "empty.db")
        fs = FeatureStore(redis_url=None)
        df = fs.query(db_path=db_path)
        assert isinstance(df, pd.DataFrame)
        assert df.empty


# ---------------------------------------------------------------------------
# DualTierFeatureStore
# ---------------------------------------------------------------------------


class TestDualTierFeatureStore:
    def _make_hot_df(self, rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["recorded_at"] = pd.to_datetime(df["recorded_at"], utc=True)
        return df

    def test_deduplicates_same_row_in_both_tiers(self) -> None:
        ts = pd.Timestamp("2025-01-15T12:00:00", tz="UTC")
        row = {
            "wallet": "GA1", "asset_pair": "XLM/USDC",
            "feature_name": "f1", "feature_value": 0.5, "recorded_at": ts,
        }
        hot = MagicMock(spec=FeatureStore)
        hot.query.return_value = pd.DataFrame([row])

        cold = MagicMock(spec=ParquetFeatureColdTier)
        cold.query.return_value = pd.DataFrame([row])

        store = DualTierFeatureStore(hot, cold)
        result = store.query()

        assert len(result) == 1

    def test_merges_distinct_rows_from_both_tiers(self) -> None:
        ts_hot = pd.Timestamp("2026-01-10T00:00:00", tz="UTC")
        ts_cold = pd.Timestamp("2025-11-01T00:00:00", tz="UTC")

        hot_row = {
            "wallet": "GA1", "asset_pair": "XLM/USDC",
            "feature_name": "f1", "feature_value": 0.5, "recorded_at": ts_hot,
        }
        cold_row = {
            "wallet": "GA1", "asset_pair": "XLM/USDC",
            "feature_name": "f1", "feature_value": 0.3, "recorded_at": ts_cold,
        }

        hot = MagicMock(spec=FeatureStore)
        hot.query.return_value = pd.DataFrame([hot_row])
        cold = MagicMock(spec=ParquetFeatureColdTier)
        cold.query.return_value = pd.DataFrame([cold_row])

        store = DualTierFeatureStore(hot, cold)
        result = store.query()
        assert len(result) == 2

    def test_spans_archival_boundary(self, tmp_path: Path) -> None:
        """Data from 45 days ago (cold) and 10 days ago (hot) both returned."""
        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        # Insert 10-day-old row into hot SQLite
        recent_ts = datetime.utcnow() - timedelta(days=10)
        _insert_row(db_path, "GA1", "f1", 0.5, recent_ts)

        # Write 45-day-old row to Parquet cold tier
        old_ts = datetime.utcnow() - timedelta(days=45)
        cold_rows = [
            {"wallet": "GA2", "asset_pair": "XLM/USDC", "feature_name": "f1",
             "feature_value": 0.3, "recorded_at": old_ts.strftime("%Y-%m-%dT%H:%M:%SZ")},
        ]
        df_cold = pd.DataFrame(cold_rows)
        df_cold["recorded_at"] = pd.to_datetime(df_cold["recorded_at"], utc=True)
        df_cold["year"] = df_cold["recorded_at"].dt.year.astype("int32")
        df_cold["month"] = df_cold["recorded_at"].dt.month.astype("int32")
        df_cold["day"] = df_cold["recorded_at"].dt.day.astype("int32")
        import pyarrow as pa
        table = pa.Table.from_pandas(df_cold)
        pq.write_to_dataset(table, root_path=str(archive_dir),
                            partition_cols=["year", "month", "day"],
                            existing_data_behavior="overwrite_or_ignore")

        fs = FeatureStore(redis_url=None)
        cold = ParquetFeatureColdTier(archive_dir)
        store = DualTierFeatureStore(fs, cold)
        result = store.query(db_path=db_path)

        assert len(result) == 2
        wallets = set(result["wallet"])
        assert "GA1" in wallets
        assert "GA2" in wallets

    def test_returns_empty_when_both_tiers_empty(self) -> None:
        hot = MagicMock(spec=FeatureStore)
        hot.query.return_value = pd.DataFrame()
        cold = MagicMock(spec=ParquetFeatureColdTier)
        cold.query.return_value = pd.DataFrame()

        store = DualTierFeatureStore(hot, cold)
        result = store.query()
        assert result.empty

    def test_logs_warning_on_duplicates(self, caplog: pytest.LogCaptureFixture) -> None:
        ts = pd.Timestamp("2025-01-15T12:00:00", tz="UTC")
        row = {
            "wallet": "GA1", "asset_pair": "XLM/USDC",
            "feature_name": "f1", "feature_value": 0.5, "recorded_at": ts,
        }
        hot = MagicMock(spec=FeatureStore)
        hot.query.return_value = pd.DataFrame([row])
        cold = MagicMock(spec=ParquetFeatureColdTier)
        cold.query.return_value = pd.DataFrame([row])

        store = DualTierFeatureStore(hot, cold)
        import logging
        with caplog.at_level(logging.WARNING, logger="detection.feature_store"):
            store.query()

        assert any("duplicate" in m.lower() for m in caplog.messages)


# ---------------------------------------------------------------------------
# load_production_features
# ---------------------------------------------------------------------------


class TestLoadProductionFeatures:
    def test_calls_store_query_with_since(self) -> None:
        from detection.drift_monitor import load_production_features

        store = MagicMock()
        store.query.return_value = pd.DataFrame(
            columns=["wallet", "asset_pair", "feature_name", "feature_value", "recorded_at"]
        )
        load_production_features(store, since_days=30)

        assert store.query.called
        call_kwargs = store.query.call_args
        since_arg = call_kwargs.kwargs.get("since") or call_kwargs.args[0]
        assert isinstance(since_arg, datetime)

    def test_returns_dataframe(self) -> None:
        from detection.drift_monitor import load_production_features

        expected_df = pd.DataFrame(
            [{"wallet": "GA1", "asset_pair": "XLM/USDC", "feature_name": "f1",
              "feature_value": 0.5, "recorded_at": datetime.utcnow()}]
        )
        store = MagicMock()
        store.query.return_value = expected_df

        result = load_production_features(store, since_days=60)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Admin API endpoint
# ---------------------------------------------------------------------------


class TestFeatureStoreStatsEndpoint:
    def test_stats_endpoint_returns_expected_fields(self, tmp_path: Path) -> None:
        try:
            from fastapi.testclient import TestClient
            import api.admin_router  # noqa: F401 — skip if dependencies missing
        except (ImportError, ModuleNotFoundError):
            pytest.skip("api.admin_router dependencies (slowapi etc.) not installed")

        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        recent_ts = datetime.utcnow() - timedelta(days=5)
        _insert_row(db_path, "GA1", "f1", 0.5, recent_ts)

        mock_cold = MagicMock(spec=ParquetFeatureColdTier)
        mock_cold.row_count.return_value = 0
        mock_cold.oldest_record.return_value = None

        with (
            patch("api.admin_router.settings.db_path", db_path),
            patch("api.admin_router.settings.feature_archive_dir", str(archive_dir)),
            patch("api.admin_router.ParquetFeatureColdTier", return_value=mock_cold),
        ):
            from api.admin_router import router
            from fastapi import FastAPI

            test_app = FastAPI()

            def _override_admin() -> None:
                pass

            from api.auth import require_admin_key
            test_app.include_router(router)
            test_app.dependency_overrides[require_admin_key] = _override_admin

            client = TestClient(test_app)
            resp = client.get("/admin/feature-store/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert "hot_tier_rows" in data
        assert "cold_tier_rows" in data
        assert "archive_dir_size_mb" in data
        assert data["hot_tier_rows"] == 1
        assert data["cold_tier_rows"] == 0
        assert data["archive_dir_size_mb"] == 0.0


# ---------------------------------------------------------------------------
# CLI archive-features command
# ---------------------------------------------------------------------------


class TestArchiveFeaturesCLI:
    def test_archive_features_command_archives_and_removes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from cli import app

        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)

        old_ts = datetime.utcnow() - timedelta(days=40)
        _insert_row(db_path, "GA_OLD", "f1", 0.5, old_ts)
        _insert_row(db_path, "GA_NEW", "f1", 0.6, datetime.utcnow() - timedelta(days=5))

        from config import settings as settings_module
        monkeypatch.setattr(settings_module.settings, "ledgerlens_db_path", db_path)
        monkeypatch.setattr(settings_module.settings, "feature_archive_dir", str(archive_dir))
        monkeypatch.setattr(settings_module.settings, "feature_archive_cutoff_days", 30)

        runner = CliRunner()
        result = runner.invoke(app, ["archive-features", "--cutoff-days", "30"])

        assert result.exit_code == 0
        assert _count_rows(db_path) == 1
        parquet_files = list(archive_dir.rglob("*.parquet"))
        assert len(parquet_files) >= 1

    def test_archive_features_no_op_when_nothing_old(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from cli import app

        db_path = str(tmp_path / "test.db")
        archive_dir = tmp_path / "archive"
        _init_snapshot_table(db_path)
        _insert_row(db_path, "GA1", "f1", 0.5, datetime.utcnow() - timedelta(days=5))

        from config import settings as settings_module
        monkeypatch.setattr(settings_module.settings, "ledgerlens_db_path", db_path)
        monkeypatch.setattr(settings_module.settings, "feature_archive_dir", str(archive_dir))
        monkeypatch.setattr(settings_module.settings, "feature_archive_cutoff_days", 30)

        runner = CliRunner()
        result = runner.invoke(app, ["archive-features", "--cutoff-days", "30"])

        assert result.exit_code == 0
        assert "nothing to archive" in result.output.lower()
        assert _count_rows(db_path) == 1
