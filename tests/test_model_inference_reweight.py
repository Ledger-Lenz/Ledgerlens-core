"""Tests for load_runtime_weights integration in model_inference.py."""

import json
import os

import pytest

import detection.model_inference as mi
import config.settings as settings_module


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear module-level weight cache between tests."""
    mi._weights_mtime = None
    mi._runtime_weights = None
    yield
    mi._weights_mtime = None
    mi._runtime_weights = None


def _write_weights(model_dir, rf=0.2, xgb=0.5, lgbm=0.3):
    path = os.path.join(model_dir, "ensemble_weights.json")
    with open(path, "w") as fh:
        json.dump({"random_forest": rf, "xgboost": xgb, "lightgbm": lgbm}, fh)
    return path


def test_uses_runtime_weights_when_file_present(tmp_path):
    _write_weights(tmp_path, rf=0.1, xgb=0.6, lgbm=0.3)
    weights = mi.load_runtime_weights(str(tmp_path))
    assert weights is not None
    assert abs(weights["xgboost"] - 0.6) < 1e-9


def test_falls_back_when_file_absent(tmp_path):
    weights = mi.load_runtime_weights(str(tmp_path))
    assert weights is None


def test_falls_back_and_logs_warning_for_bad_sum(tmp_path, caplog):
    import logging
    _write_weights(tmp_path, rf=0.5, xgb=0.5, lgbm=0.5)  # sum = 1.5
    with caplog.at_level(logging.WARNING, logger="ledgerlens.model_inference"):
        weights = mi.load_runtime_weights(str(tmp_path))
    assert weights is None
    assert any("falling back" in r.message.lower() for r in caplog.records)


def test_score_feature_matrix_uses_runtime_weights(tmp_path, monkeypatch):
    """score_feature_matrix picks up runtime weights, not settings weights."""
    from detection.feature_engineering import FEATURE_NAMES

    _write_weights(tmp_path, rf=0.0, xgb=1.0, lgbm=0.0)
    object.__setattr__(settings_module.settings, "model_dir", str(tmp_path))

    # Minimal stub models that return fixed probabilities
    class _StubModel:
        def __init__(self, prob):
            self.prob = prob
        def predict_proba(self, X):
            import numpy as np
            return np.array([[1 - self.prob, self.prob]] * len(X))

    models = {
        "random_forest": _StubModel(0.2),
        "xgboost": _StubModel(0.9),
        "lightgbm": _StubModel(0.4),
    }
    fv = {name: 0.0 for name in FEATURE_NAMES}
    results = mi.score_feature_matrix(models, [fv])
    prob, _ = results[0]
    # With xgb weight=1.0, result should equal xgboost prob (0.9)
    assert abs(prob - 0.9) < 1e-6


def test_score_feature_matrix_falls_back_when_absent(tmp_path, monkeypatch):
    from detection.feature_engineering import FEATURE_NAMES

    object.__setattr__(settings_module.settings, "model_dir", str(tmp_path))

    class _StubModel:
        def predict_proba(self, X):
            import numpy as np
            return np.array([[0.5, 0.5]] * len(X))

    models = {k: _StubModel() for k in ("random_forest", "xgboost", "lightgbm")}
    fv = {name: 0.0 for name in FEATURE_NAMES}
    # Should not raise — falls back to settings weights
    results = mi.score_feature_matrix(models, [fv])
    assert len(results) == 1
