"""Schema-evolution compatibility tests for ingestion.parquet_exporter."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ingestion import parquet_exporter as pe

FIXTURE = Path(__file__).parent / "fixtures" / "parquet" / "trades_v1_0_untagged.parquet"


def test_current_schema_carries_version_tag():
    metadata = pe._build_schema().metadata
    assert metadata[pe.SCHEMA_VERSION_METADATA_KEY] == pe.SCHEMA_VERSION.encode()
    assert pe.SCHEMA_VERSION in pe.SCHEMA_REGISTRY


def test_legacy_fixture_readable_with_current_tooling():
    assert pe.read_schema_version(FIXTURE) == pe.LEGACY_SCHEMA_VERSION
    table = pe.read_parquet_compatible(FIXTURE)
    assert table.schema.equals(pe._build_schema())
    assert table.num_rows == 1
    assert table.column("counter_asset_code")[0].as_py() == "USDC"


def test_missing_extra_and_renamed_columns_are_reconciled(tmp_path, monkeypatch):
    old = pa.table({"id": ["1"], "old_price": [1.5], "dropped_col": [7]})
    path = tmp_path / "old.parquet"
    pq.write_table(old, path)
    monkeypatch.setitem(pe.FIELD_RENAMES, "old_price", "price")

    table = pe.read_parquet_compatible(path)
    assert table.column_names == pe.PARQUET_SCHEMA_FIELDS
    assert table.column("price")[0].as_py() is not None
    assert table.column("trade_type")[0].as_py() is None
