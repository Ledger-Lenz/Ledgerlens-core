import json
import os
from types import SimpleNamespace

import joblib
import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier

import config.settings as settings_module
from detection.feature_engineering import FEATURE_NAMES
from detection.model_inference import (
    _get_ensemble_weights,
    load_calibration,
    load_models,
    load_runtime_weights,
    score_feature_matrix,
    score_feature_vector,
    score_with_uncertainty,
)
from detection.model_signing import ModelIntegrityError, sign_model_file

_TEST_KEY = b"test-signing-key-for-unit-tests-only"


def _trained_classifier(weight: float):
    """A classifier that always predicts probability `weight` for class 1."""
    X = [[0] * len(FEATURE_NAMES), [1] * len(FEATURE_NAMES)]
    y = [0, 1] if weight > 0.5 else [1, 0]
    return RandomForestClassifier(n_estimators=5, random_state=0).fit(X, y)


def _dump_and_sign(path, model):
    joblib.dump(model, path)
    sign_model_file(str(path), _TEST_KEY)


@pytest.fixture
def model_dir(tmp_path):
    _dump_and_sign(tmp_path / "random_forest.joblib", _trained_classifier(0.9))
    _dump_and_sign(tmp_path / "xgboost.joblib", _trained_classifier(0.9))
    _dump_and_sign(tmp_path / "lightgbm.joblib", _trained_classifier(0.9))
    return str(tmp_path)


@pytest.fixture(autouse=True)
def reset_runtime_weights():
    import detection.model_inference as mi
    mi._weights_mtime = None
    mi._runtime_weights = None
    yield
    mi._weights_mtime = None
    mi._runtime_weights = None


def test_load_models_raises_when_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_models(str(tmp_path))


def test_load_models_returns_all_models(model_dir):
    models = load_models(model_dir)
    assert set(models.keys()) == {"random_forest", "xgboost", "lightgbm"}


def test_score_feature_vector_returns_probability_and_confidence(model_dir):
    models = load_models(model_dir)
    feature_vector = dict.fromkeys(FEATURE_NAMES, 1.0)

    probability, confidence = score_feature_vector(models, feature_vector)

    assert 0.0 <= probability <= 1.0
    assert 0.0 <= confidence <= 1.0


def test_load_models_with_partial_directory(tmp_path):
    _dump_and_sign(tmp_path / "random_forest.joblib", _trained_classifier(0.9))
    models = load_models(str(tmp_path))
    assert set(models.keys()) == {"random_forest"}


def test_load_models_raises_model_integrity_error_on_tampered_file(tmp_path):
    path = tmp_path / "random_forest.joblib"
    _dump_and_sign(path, _trained_classifier(0.9))
    with open(str(path), "r+b") as f:
        f.seek(0)
        b = f.read(1)
        f.seek(0)
        f.write(bytes([b[0] ^ 0xFF]))
    with pytest.raises(ModelIntegrityError):
        load_models(str(tmp_path))


def test_load_models_raises_model_integrity_error_on_missing_signature(tmp_path):
    path = tmp_path / "random_forest.joblib"
    joblib.dump(_trained_classifier(0.9), path)
    with pytest.raises(ModelIntegrityError):
        load_models(str(tmp_path))


def test_load_models_raises_on_missing_signing_key(tmp_path):
    path = tmp_path / "random_forest.joblib"
    _dump_and_sign(path, _trained_classifier(0.9))
    import config.settings as settings_module
    object.__setattr__(settings_module.settings, "model_signing_key", "")
    try:
        with pytest.raises(ModelIntegrityError, match="LEDGERLENS_MODEL_SIGNING_KEY"):
            load_models(str(tmp_path))
    finally:
        object.__setattr__(settings_module.settings, "model_signing_key", "test-signing-key-for-unit-tests-only")


class FixedProbabilityModel:
    def __init__(self, probability: float):
        self.probability = probability

    def predict_proba(self, _X):
        return np.array([[1.0 - self.probability, self.probability]])


def test_score_feature_vector_uses_runtime_settings_weights(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(ensemble_weight_rf=0.0, ensemble_weight_xgb=1.0, ensemble_weight_lgbm=0.0),
    )
    feature_vector = dict.fromkeys(FEATURE_NAMES, 1.0)

    probability, _confidence = score_feature_vector(
        {
            "random_forest": FixedProbabilityModel(0.1),
            "xgboost": FixedProbabilityModel(0.9),
            "lightgbm": FixedProbabilityModel(0.2),
        },
        feature_vector,
    )

    assert probability == pytest.approx(0.9)


def test_score_feature_vector_normalizes_non_unit_weights(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(ensemble_weight_rf=2.0, ensemble_weight_xgb=1.0, ensemble_weight_lgbm=0.0),
    )
    feature_vector = dict.fromkeys(FEATURE_NAMES, 1.0)

    probability, _confidence = score_feature_vector(
        {
            "random_forest": FixedProbabilityModel(0.2),
            "xgboost": FixedProbabilityModel(0.8),
            "lightgbm": FixedProbabilityModel(0.5),
        },
        feature_vector,
    )

    assert probability == pytest.approx(0.4)
    assert 0.0 <= probability <= 1.0


def test_score_feature_vector_raises_on_zero_total_weight(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(ensemble_weight_rf=0.0, ensemble_weight_xgb=0.0, ensemble_weight_lgbm=0.0),
    )
    feature_vector = dict.fromkeys(FEATURE_NAMES, 1.0)
    models = {"random_forest": FixedProbabilityModel(0.5)}
    with pytest.raises(ValueError, match="positive ensemble weight"):
        score_feature_vector(models, feature_vector)


# ---------------------------------------------------------------------------
# score_feature_matrix tests
# ---------------------------------------------------------------------------


class FixedProbabilityMatrixModel:
    """Returns the same probability for every row in a batch."""

    def __init__(self, probability: float):
        self.probability = probability

    def predict_proba(self, X):
        n = X.shape[0]
        return np.column_stack([
            np.full(n, 1.0 - self.probability),
            np.full(n, self.probability),
        ])


def test_score_feature_matrix_empty_input():
    assert score_feature_matrix({}, []) == []


def test_score_feature_matrix_returns_one_result_per_vector():
    models = {
        "random_forest": FixedProbabilityMatrixModel(0.7),
        "xgboost": FixedProbabilityMatrixModel(0.7),
        "lightgbm": FixedProbabilityMatrixModel(0.7),
    }
    vectors = [dict.fromkeys(FEATURE_NAMES, float(i)) for i in range(10)]
    results = score_feature_matrix(models, vectors)
    assert len(results) == 10
    for prob, conf in results:
        assert 0.0 <= prob <= 1.0
        assert 0.0 <= conf <= 1.0


def test_score_feature_matrix_consistent_with_per_vector(monkeypatch):
    """Batch scores must match per-vector scores exactly."""
    weights = SimpleNamespace(ensemble_weight_rf=0.25, ensemble_weight_xgb=0.50, ensemble_weight_lgbm=0.25)
    monkeypatch.setattr(settings_module, "settings", weights)

    models = {
        "random_forest": FixedProbabilityMatrixModel(0.3),
        "xgboost": FixedProbabilityMatrixModel(0.6),
        "lightgbm": FixedProbabilityMatrixModel(0.9),
    }
    vectors = [dict.fromkeys(FEATURE_NAMES, float(v)) for v in [0.0, 0.5, 1.0]]

    batch = score_feature_matrix(models, vectors)
    per_vector = [score_feature_vector(models, fv) for fv in vectors]

    for (b_prob, b_conf), (p_prob, p_conf) in zip(batch, per_vector):
        assert b_prob == pytest.approx(p_prob, abs=1e-9)
        assert b_conf == pytest.approx(p_conf, abs=1e-9)


def test_score_feature_matrix_full_agreement_gives_max_confidence(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(ensemble_weight_rf=1.0, ensemble_weight_xgb=1.0, ensemble_weight_lgbm=1.0),
    )
    models = {
        "random_forest": FixedProbabilityMatrixModel(0.8),
        "xgboost": FixedProbabilityMatrixModel(0.8),
        "lightgbm": FixedProbabilityMatrixModel(0.8),
    }
    results = score_feature_matrix(models, [dict.fromkeys(FEATURE_NAMES, 1.0)])
    _prob, conf = results[0]
    assert conf == pytest.approx(1.0)


def test_score_feature_matrix_raises_when_all_weights_zero(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(ensemble_weight_rf=0.0, ensemble_weight_xgb=0.0, ensemble_weight_lgbm=0.0),
    )
    models = {"random_forest": FixedProbabilityMatrixModel(0.5)}
    with pytest.raises(ValueError, match="positive ensemble weight"):
        score_feature_matrix(models, [dict.fromkeys(FEATURE_NAMES, 1.0)])


# ---------------------------------------------------------------------------
# load_runtime_weights tests
# ---------------------------------------------------------------------------


def test_load_runtime_weights_returns_none_when_file_absent(tmp_path):
    assert load_runtime_weights(str(tmp_path)) is None


def test_load_runtime_weights_loads_valid_file(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": 0.2, "xgboost": 0.5, "lightgbm": 0.3}, f)
    weights = load_runtime_weights(str(tmp_path))
    assert weights is not None
    assert abs(weights["random_forest"] - 0.2) < 1e-9
    assert abs(weights["xgboost"] - 0.5) < 1e-9
    assert abs(weights["lightgbm"] - 0.3) < 1e-9


def test_load_runtime_weights_returns_none_on_bad_json(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        f.write("not json")
    assert load_runtime_weights(str(tmp_path)) is None


def test_load_runtime_weights_returns_none_on_missing_keys(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": 0.2, "xgboost": 0.5}, f)
    assert load_runtime_weights(str(tmp_path)) is None


def test_load_runtime_weights_returns_none_on_extra_keys(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": 0.2, "xgboost": 0.5, "lightgbm": 0.3, "unknown": 0.0}, f)
    assert load_runtime_weights(str(tmp_path)) is None


def test_load_runtime_weights_returns_none_on_negative_weight(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": -0.1, "xgboost": 0.6, "lightgbm": 0.5}, f)
    assert load_runtime_weights(str(tmp_path)) is None


def test_load_runtime_weights_returns_none_on_non_numeric_weight(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": "abc", "xgboost": 0.5, "lightgbm": 0.3}, f)
    assert load_runtime_weights(str(tmp_path)) is None


def test_load_runtime_weights_returns_none_on_bad_sum(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": 0.5, "xgboost": 0.5, "lightgbm": 0.5}, f)
    assert load_runtime_weights(str(tmp_path)) is None


def test_load_runtime_weights_caches_valid_result(tmp_path):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": 0.2, "xgboost": 0.5, "lightgbm": 0.3}, f)
    w1 = load_runtime_weights(str(tmp_path))
    w2 = load_runtime_weights(str(tmp_path))
    assert w1 is w2


# ---------------------------------------------------------------------------
# load_calibration tests
# ---------------------------------------------------------------------------


def test_load_calibration_returns_empty_when_no_files(tmp_path):
    cal = load_calibration(str(tmp_path))
    assert cal == {}


# ---------------------------------------------------------------------------
# score_with_uncertainty tests
# ---------------------------------------------------------------------------


def test_score_with_uncertainty_no_calibrators(model_dir):
    models = load_models(model_dir)
    fv = dict.fromkeys(FEATURE_NAMES, 1.0)
    result = score_with_uncertainty(models, fv, calibrators=None)
    assert "score" in result
    assert result["score_lower"] == 0.0
    assert result["score_upper"] == 100.0
    assert result["prediction_set"] == []
    assert result["coverage_guarantee"] == 1.0
    assert 0.0 <= result["score"] <= 100.0


# ---------------------------------------------------------------------------
# _get_ensemble_weights tests
# ---------------------------------------------------------------------------


def test_get_ensemble_weights_falls_back_to_settings_when_no_model_dir(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(
            ensemble_weight_rf=0.3, ensemble_weight_xgb=0.3, ensemble_weight_lgbm=0.4
        ),
    )
    weights = _get_ensemble_weights()
    assert abs(weights["random_forest"] - 0.3) < 1e-9


def test_get_ensemble_weights_uses_runtime_when_model_dir_provided(tmp_path, monkeypatch):
    path = os.path.join(tmp_path, "ensemble_weights.json")
    with open(path, "w") as f:
        json.dump({"random_forest": 0.1, "xgboost": 0.8, "lightgbm": 0.1}, f)
    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(
            model_dir=str(tmp_path),
            ensemble_weight_rf=0.3,
            ensemble_weight_xgb=0.3,
            ensemble_weight_lgbm=0.4,
        ),
    )
    weights = _get_ensemble_weights()
    assert abs(weights["xgboost"] - 0.8) < 1e-9
