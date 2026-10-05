"""Tests for coordinate-wise trimmed-mean aggregation (Issue #1037)."""

from __future__ import annotations

import numpy as np
import pytest

from detection.federated.robust_aggregation import trimmed_mean
from detection.federated.server import FederatedAggregationServer


def _honest(n: int, dim: int = 50, seed: int = 0) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    return [np.clip(0.3 + rng.normal(0.0, 0.02, dim), 0.0, 1.0) for _ in range(n)]


def test_zero_trim_equals_mean():
    updates = _honest(5)
    np.testing.assert_allclose(trimmed_mean(updates, 0.0), np.mean(updates, axis=0))


@pytest.mark.parametrize("n_honest,n_byzantine,trim", [(8, 2, 0.2), (7, 3, 0.3), (16, 4, 0.25)])
def test_poisoning_within_tolerance_is_resisted(n_honest, n_byzantine, trim):
    honest = _honest(n_honest)
    poisoned = [np.ones(50) * 1.0 for _ in range(n_byzantine)]
    updates = honest + poisoned
    honest_mean = np.mean(honest, axis=0)

    robust = trimmed_mean(updates, trim)
    naive = np.mean(updates, axis=0)

    # Robust aggregate stays within the honest spread; naive is dragged away.
    assert np.max(np.abs(robust - honest_mean)) < 0.05
    assert np.max(np.abs(naive - honest_mean)) > 0.1
    assert np.all(robust <= np.max(honest, axis=0) + 1e-12)


def test_poisoning_beyond_tolerance_is_not_guaranteed():
    honest = _honest(6)
    poisoned = [np.ones(50) for _ in range(4)]
    robust = trimmed_mean(honest + poisoned, 0.2)  # trims 2, attacker has 4
    assert np.max(np.abs(robust - np.mean(honest, axis=0))) > 0.1


@pytest.mark.parametrize("trim", [-0.1, 0.5, 0.9])
def test_invalid_trim_fraction_raises(trim):
    with pytest.raises(ValueError):
        trimmed_mean(_honest(4), trim)


def test_empty_updates_raise():
    with pytest.raises(ValueError):
        trimmed_mean([], 0.1)


def test_server_rejects_unknown_strategy():
    with pytest.raises(ValueError):
        FederatedAggregationServer(aggregation_strategy="median")


def test_server_accepts_trimmed_mean_strategy():
    server = FederatedAggregationServer(aggregation_strategy="trimmed_mean", trim_fraction=0.25)
    assert server.aggregation_strategy == "trimmed_mean"
    assert server.trim_fraction == 0.25
