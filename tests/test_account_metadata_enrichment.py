"""Tests for account metadata enrichment pipeline (Issue #91)."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from ingestion.account_loader import AccountMetadata, AccountMetadataCache


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db_path = f.name
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TABLE account_metadata_cache (
                    account_id TEXT PRIMARY KEY,
                    funding_source TEXT,
                    created_at TEXT,
                    home_domain TEXT,
                    num_signers INTEGER NOT NULL DEFAULT 1,
                    low_threshold INTEGER NOT NULL DEFAULT 0,
                    med_threshold INTEGER NOT NULL DEFAULT 0,
                    high_threshold INTEGER NOT NULL DEFAULT 0,
                    signer_keys_json TEXT NOT NULL DEFAULT '[]',
                    fetched_at TEXT NOT NULL
                )"""
            )
        yield db_path


def _make_metadata(account_id: str = "GABC", fetched_at: datetime | None = None) -> AccountMetadata:
    return AccountMetadata(
        account_id=account_id,
        funding_source="GFUNDER",
        created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
        home_domain="example.com",
        num_signers=2,
        low_threshold=1,
        med_threshold=2,
        high_threshold=3,
        signer_keys=["GABC", "GXYZ"],
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )


def test_accountmetadata_dataclass():
    m = _make_metadata()
    assert m.account_id == "GABC"
    assert m.home_domain == "example.com"
    assert m.num_signers == 2
    assert m.signer_keys == ["GABC", "GXYZ"]


def test_cache_set_and_get(tmp_db):
    cache = AccountMetadataCache(ttl_seconds=3600, db_path=tmp_db)
    meta = _make_metadata()
    cache.set(meta)
    loaded = cache.get("GABC")
    assert loaded is not None
    assert loaded.account_id == "GABC"
    assert loaded.home_domain == "example.com"
    assert loaded.signer_keys == ["GABC", "GXYZ"]


def test_cache_miss_returns_none(tmp_db):
    cache = AccountMetadataCache(ttl_seconds=3600, db_path=tmp_db)
    assert cache.get("GNOBODY") is None


def test_cache_ttl_expiry(tmp_db):
    cache = AccountMetadataCache(ttl_seconds=60, db_path=tmp_db)
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    meta = _make_metadata(fetched_at=stale_time)
    cache.set(meta)
    # TTL expired — should return None
    assert cache.get("GABC") is None


def test_cache_upsert(tmp_db):
    cache = AccountMetadataCache(ttl_seconds=3600, db_path=tmp_db)
    meta1 = _make_metadata()
    cache.set(meta1)
    meta2 = _make_metadata()
    meta2.home_domain = "updated.com"
    cache.set(meta2)
    loaded = cache.get("GABC")
    assert loaded.home_domain == "updated.com"


def test_load_all_enriched_uses_cache(tmp_db):
    cache = AccountMetadataCache(ttl_seconds=3600, db_path=tmp_db)
    meta = _make_metadata("GABC")
    cache.set(meta)

    with patch("ingestion.account_loader.get_account_metadata_enriched") as mock_fetch:
        result = cache.load_all_enriched(["GABC"], concurrency=2)

    # cache hit — should not call the network
    mock_fetch.assert_not_called()
    assert result["GABC"].account_id == "GABC"


def test_load_all_enriched_fetches_missing(tmp_db):
    cache = AccountMetadataCache(ttl_seconds=3600, db_path=tmp_db)
    fetched_meta = _make_metadata("GNEW")

    with patch("ingestion.account_loader.get_account_metadata_enriched", return_value=fetched_meta):
        result = cache.load_all_enriched(["GNEW"], concurrency=1)

    assert "GNEW" in result
    # Should now be in cache
    assert cache.get("GNEW") is not None
