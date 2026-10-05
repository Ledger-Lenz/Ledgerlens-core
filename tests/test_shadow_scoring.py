"""Tests for shadow model scoring mode."""

import os
import sqlite3
from unittest.mock import patch

import pytest

from detection.shadow_scoring import (
    _nearest_rank_percentile,
    compute_divergence_stats,
    divergence_out_of_range,
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


def test_nearest_rank_percentile_uses_expected_rank():
    values = [0.01 * i for i in range(1, 21)]

    assert _nearest_rank_percentile(values, 0.95) == pytest.approx(0.19)


class TestDivergenceMetric:
    PRODUCTION = (0.10, 0.50, 0.90, 0.30, 0.70)
    SHADOW = (0.15, 0.40, 0.90, 0.60, 0.65)

    def test_stats_match_fixed_score_sets(self):
        stats = compute_divergence_stats(self.PRODUCTION, self.SHADOW)
        assert stats["count"] == 5
        assert stats["mean_delta"] == pytest.approx(0.04)
        assert stats["mean_divergence"] == pytest.approx(0.10)
        assert stats["p95_divergence"] == pytest.approx(0.30)
        assert stats["max_divergence"] == pytest.approx(0.30)

    def test_identical_scores_have_zero_divergence(self):
        stats = compute_divergence_stats(self.PRODUCTION, self.PRODUCTION)
        assert stats["mean_divergence"] == 0.0
        assert stats["mean_delta"] == 0.0

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError):
            compute_divergence_stats([0.1], [0.1, 0.2])

    def test_store_emits_signed_delta_histogram(self, shadow_db):
        from detection.shadow_scoring import _get_delta_histogram

        histogram = _get_delta_histogram()
        if histogram is None:
            pytest.skip("prometheus_client not installed")
        before = histogram._sum.get()
        store_shadow_score(shadow_db, "GABC", "XLM/USDC", 0.40, 0.65, "v2.0")
        assert histogram._sum.get() - before == pytest.approx(0.25)


class TestDivergenceAlerting:
    def test_fires_when_divergence_trends_above_range(self):
        simulated = [0.03, 0.04, 0.12, 0.15, 0.18]
        assert divergence_out_of_range(simulated, normal_max=0.10, sustained_windows=3)

    def test_single_spike_does_not_fire(self):
        simulated = [0.03, 0.04, 0.25, 0.05, 0.04]
        assert not divergence_out_of_range(simulated, normal_max=0.10, sustained_windows=3)

    def test_within_range_does_not_fire(self):
        assert not divergence_out_of_range([0.02] * 10, normal_max=0.10)

    def test_insufficient_history_does_not_fire(self):
        assert not divergence_out_of_range([0.5, 0.5], normal_max=0.10, sustained_windows=3)
