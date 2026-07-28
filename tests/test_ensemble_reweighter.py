"""Tests for detection/ensemble_reweighter.py."""

from datetime import datetime, timezone


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


def test_get_current_weights_fallback_to_uniform(tmp_path):
    """When adaptive reweighter is not available and no weights file exists,
    get_current_weights returns uniform weights."""
    from detection.ensemble_reweighter import get_current_weights
    model_dir = str(tmp_path / "nonexistent_models")
    weights = get_current_weights(model_dir=model_dir)
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    for model in _MODELS:
        assert abs(weights[model] - 1 / 3) < 1e-6


def test_get_current_weights_reads_from_file(tmp_path):
    """When a valid weights file exists, get_current_weights reads it."""
    import json
    from detection.ensemble_reweighter import get_current_weights

    model_dir = str(tmp_path)
    expected = {"random_forest": 0.5, "xgboost": 0.3, "lightgbm": 0.2}
    with open(tmp_path / "ensemble_weights.json", "w") as f:
        json.dump(expected, f)

    weights = get_current_weights(model_dir=model_dir)
    assert weights == expected


def test_get_current_weights_handles_partial_file(tmp_path):
    """When file has only some models, falls back to uniform."""
    import json
    from detection.ensemble_reweighter import get_current_weights

    model_dir = str(tmp_path)
    with open(tmp_path / "ensemble_weights.json", "w") as f:
        json.dump({"random_forest": 1.0}, f)

    weights = get_current_weights(model_dir=model_dir)
    for model in _MODELS:
        assert abs(weights[model] - 1 / 3) < 1e-6


def test_apply_weights_writes_and_is_readable(tmp_path):
    """apply_weights writes a file that get_current_weights can read back."""
    import json
    from detection.ensemble_reweighter import apply_weights, get_current_weights

    model_dir = str(tmp_path)
    weights = {"random_forest": 0.6, "xgboost": 0.3, "lightgbm": 0.1}
    apply_weights(weights, model_dir)

    # File should exist and be valid JSON
    weights_path = tmp_path / "ensemble_weights.json"
    assert weights_path.exists()
    data = json.loads(weights_path.read_text())
    assert "updated_at" in data
    assert data["random_forest"] == 0.6
    assert data["xgboost"] == 0.3
    assert data["lightgbm"] == 0.1

    # Round-trip: get_current_weights should return our weights
    read_back = get_current_weights(model_dir=model_dir)
    assert read_back == {"random_forest": 0.6, "xgboost": 0.3, "lightgbm": 0.1}
