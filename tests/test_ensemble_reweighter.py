"""Tests for detection/ensemble_reweighter.py."""

from datetime import datetime, timezone

import pytest

from detection.ensemble_reweighter import compute_updated_weights
from detection.feedback_store import ScoringFeedback

_MODELS = ("random_forest", "xgboost", "lightgbm")


def _fb(model_name, predicted_probability, ground_truth):
    return ScoringFeedback(
        wallet="GABC",
        asset_pair="XLM/USDC",
        model_name=model_name,
        predicted_probability=predicted_probability,
        ground_truth=ground_truth,
        scored_at=datetime.now(timezone.utc),
        confirmed_at=datetime.now(timezone.utc),
    )


def test_weights_sum_to_one():
    feedback = [_fb("random_forest", 0.9, 1), _fb("xgboost", 0.7, 1), _fb("lightgbm", 0.5, 0)]
    w = compute_updated_weights(feedback)
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_better_model_gets_higher_weight():
    # random_forest always correct (high confidence on wash), xgboost at 50%
    feedback = (
        [_fb("random_forest", 0.99, 1)] * 10
        + [_fb("xgboost", 0.5, 1)] * 10
        + [_fb("lightgbm", 0.5, 1)] * 10
    )
    w = compute_updated_weights(feedback)
    assert w["random_forest"] > w["xgboost"]


def test_zero_feedback_returns_uniform():
    w = compute_updated_weights([])
    for model in _MODELS:
        assert abs(w[model] - 1 / 3) < 1e-6


def test_weights_in_open_unit_interval():
    import random
    rng = random.Random(0)
    feedback = [
        _fb(m, rng.random(), rng.randint(0, 1))
        for m in _MODELS
        for _ in range(20)
    ]
    w = compute_updated_weights(feedback)
    for model in _MODELS:
        assert 0 < w[model] < 1
