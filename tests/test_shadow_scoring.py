"""Tests for shadow model scoring mode."""

import os
import sqlite3
from unittest.mock import patch

import pytest

from detection.shadow_scoring import (
    get_shadow_model_version,
    get_shadow_report,
    store_shadow_score,
    _init_shadow_table,
)


@pytest.fixture
def shadow_db(tmp_path):
    db_path = str(tmp_path / "shadow_test.db")
    _init_shadow_table(db_path)
    return db_path


class TestShadowModelVersion:
    def test_returns_none_when_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SHADOW_MODEL_VERSION", None)
            assert get_shadow_model_version() is None

    def test_returns_version_when_set(self):
        with patch.dict(os.environ, {"SHADOW_MODEL_VERSION": "v2.1"}):
            assert get_shadow_model_version() == "v2.1"


class TestStoreShadowScore:
    def test_stores_and_returns_divergence(self, shadow_db):
        div = store_shadow_score(
            shadow_db, "GABC123", "XLM/USDC", 0.85, 0.70, "v2.0"
        )
        assert div == pytest.approx(0.15)

        with sqlite3.connect(shadow_db) as conn:
            rows = conn.execute("SELECT * FROM shadow_scores").fetchall()
            assert len(rows) == 1

    def test_stores_multiple_scores(self, shadow_db):
        store_shadow_score(shadow_db, "GABC1", "XLM/USDC", 0.9, 0.8, "v2.0")
        store_shadow_score(shadow_db, "GABC2", "XLM/USDC", 0.5, 0.5, "v2.0")
        store_shadow_score(shadow_db, "GABC3", "XLM/USDC", 0.3, 0.7, "v2.0")

        with sqlite3.connect(shadow_db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM shadow_scores").fetchone()[0]
            assert count == 3


class TestShadowReport:
    def test_empty_report(self, shadow_db):
        report = get_shadow_report(shadow_db)
        assert report["total_comparisons"] == 0
        assert report["mean_divergence"] == 0.0
        assert report["p95_divergence"] == 0.0
        assert report["high_divergence_wallets"] == []

    def test_report_with_data(self, shadow_db):
        store_shadow_score(shadow_db, "GABC1", "XLM/USDC", 0.9, 0.6, "v2.0")
        store_shadow_score(shadow_db, "GABC2", "XLM/USDC", 0.5, 0.5, "v2.0")
        store_shadow_score(shadow_db, "GABC3", "XLM/USDC", 0.8, 0.3, "v2.0")

        report = get_shadow_report(shadow_db)
        assert report["total_comparisons"] == 3
        assert report["mean_divergence"] > 0
        assert report["p95_divergence"] > 0

    def test_high_divergence_wallets(self, shadow_db):
        store_shadow_score(shadow_db, "GABC1", "XLM/USDC", 0.9, 0.5, "v2.0")
        store_shadow_score(shadow_db, "GABC2", "XLM/USDC", 0.5, 0.49, "v2.0")

        report = get_shadow_report(shadow_db, divergence_threshold=0.20)
        high = report["high_divergence_wallets"]
        assert len(high) == 1
        assert high[0]["wallet"] == "GABC1"
        assert high[0]["divergence"] == pytest.approx(0.4)
