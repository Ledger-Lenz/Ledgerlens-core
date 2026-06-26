"""Tests for Byzantine-fault-tolerant Krum / Multi-Krum aggregation (Issue #146)."""

from __future__ import annotations

import time
from unittest.mock import patch

import numpy as np
import pytest

from detection.federated.krum import KrumAggregator, KrumStrategy


# ---------------------------------------------------------------------------
# KrumAggregator unit tests
# ---------------------------------------------------------------------------

class TestKrumScores:
    def test_outlier_has_highest_score(self):
        """One gradient scaled 100x should have the highest Krum score."""
        rng = np.random.default_rng(0)
        normal = [rng.standard_normal(50) for _ in range(4)]
        outlier = rng.standard_normal(50) * 100
        gradients = normal + [outlier]
        agg = KrumAggregator(f=1)
        scores = agg.krum_scores(gradients)
        assert np.argmax(scores) == 4

    def test_identical_gradients_equal_scores(self):
        """All identical gradient vectors should yield equal Krum scores."""
        g = np.ones(20)
        gradients = [g.copy() for _ in range(5)]
        agg = KrumAggregator(f=1)
        scores = agg.krum_scores(gradients)
        assert np.allclose(scores, scores[0])

    def test_invalid_f_raises(self):
        """2f+2 >= n should raise ValueError."""
        agg = KrumAggregator(f=2)
        gradients = [np.ones(10) for _ in range(5)]  # 2*2+2=6 >= 5
        with pytest.raises(ValueError, match="2f\\+2 < n"):
            agg.krum_scores(gradients)

    def test_negative_f_raises(self):
        with pytest.raises(ValueError):
            KrumAggregator(f=-1)


class TestKrumSelect:
    def test_select_m1_returns_minimum_score_index(self):
        """m=1 should return the index with the lowest Krum score."""
        rng = np.random.default_rng(42)
        gradients = [rng.standard_normal(30) for _ in range(6)]
        agg = KrumAggregator(f=1)
        scores = agg.krum_scores(gradients)
        selected, excluded, returned_scores = agg.select(gradients, m=1)
        assert len(selected) == 1
        assert selected[0] == int(np.argmin(scores))
        assert set(excluded) == set(range(6)) - set(selected)

    def test_select_multi_krum_m3(self):
        """m=3 should return 3 selected and 3 excluded indices."""
        rng = np.random.default_rng(7)
        gradients = [rng.standard_normal(20) for _ in range(6)]
        agg = KrumAggregator(f=1)
        selected, excluded, _ = agg.select(gradients, m=3)
        assert len(selected) == 3
        assert len(excluded) == 3
        assert set(selected) | set(excluded) == set(range(6))

    def test_m_exceeds_n_minus_f_raises(self):
        """m > n - f must raise ValueError."""
        agg = KrumAggregator(f=1)
        gradients = [np.ones(10) for _ in range(5)]
        with pytest.raises(ValueError, match="m="):
            agg.select(gradients, m=5)  # 5 > 5-1 = 4


# ---------------------------------------------------------------------------
# KrumStrategy unit tests
# ---------------------------------------------------------------------------

class TestKrumStrategyConstructor:
    def test_invalid_tolerance_raises(self):
        """2f+2 >= min_clients must raise ValueError at construction."""
        with pytest.raises(ValueError, match="Byzantine tolerance"):
            KrumStrategy(f=2, min_clients=5)  # 2*2+2=6 >= 5

    def test_default_f_floor_n_over_3(self):
        """Default f should be floor(min_clients / 3)."""
        strat = KrumStrategy(min_clients=9)
        assert strat.f == 3  # floor(9/3) = 3; 2*3+2=8 < 9

    def test_valid_construction(self):
        strat = KrumStrategy(f=1, min_clients=5)
        assert strat.f == 1
        assert strat.m == 1


class TestKrumStrategyAggregate:
    def test_aggregate_uses_only_selected_gradients(self):
        """Aggregate result should equal the mean of selected gradients only."""
        rng = np.random.default_rng(0)
        gradients = [rng.standard_normal(20) for _ in range(5)]
        strat = KrumStrategy(f=1, min_clients=5)
        result = strat.aggregate(gradients)
        # Verify independently: get selected indices and compute mean
        agg = KrumAggregator(f=1)
        selected, _, _ = agg.select(gradients, m=1)
        expected = np.mean([gradients[i] for i in selected], axis=0)
        np.testing.assert_array_almost_equal(result, expected)

    def test_multi_krum_aggregate(self):
        """Multi-Krum (m=3) result must equal mean of top-3 selected gradients."""
        rng = np.random.default_rng(1)
        gradients = [rng.standard_normal(15) for _ in range(7)]
        strat = KrumStrategy(f=1, min_clients=7, multi_krum_m=3)
        result = strat.aggregate(gradients)
        agg = KrumAggregator(f=1)
        selected, _, _ = agg.select(gradients, m=3)
        expected = np.mean([gradients[i] for i in selected], axis=0)
        np.testing.assert_array_almost_equal(result, expected)

    def test_round_number_increments(self):
        strat = KrumStrategy(f=1, min_clients=5)
        gradients = [np.ones(10) * i for i in range(5)]
        strat.aggregate(gradients)
        strat.aggregate(gradients)
        assert strat._round_number == 2

    def test_persistent_exclusion_warning(self, caplog):
        """A client excluded >50% of rounds should trigger a WARNING."""
        import logging
        rng = np.random.default_rng(99)
        # Build gradients where index 4 is always a large outlier.
        base = [rng.standard_normal(20) for _ in range(4)]
        strat = KrumStrategy(f=1, min_clients=5)
        with caplog.at_level(logging.WARNING, logger="ledgerlens.federated.krum"):
            for _ in range(4):
                outlier = rng.standard_normal(20) * 1000
                strat.aggregate(base + [outlier])
        assert any("persistent Byzantine" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------

class TestKrumPerformance:
    def test_krum_scores_50_clients_50k_dim_under_5s(self):
        """krum_scores for n=50, D=50_000 must finish in under 5 seconds."""
        rng = np.random.default_rng(0)
        gradients = [rng.standard_normal(50_000) for _ in range(50)]
        agg = KrumAggregator(f=15)  # 2*15+2=32 < 50
        start = time.perf_counter()
        agg.krum_scores(gradients)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"krum_scores took {elapsed:.2f}s, expected < 5s"
