from types import SimpleNamespace

import joblib
import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier

import detection.model_inference as model_inference
from detection.feature_engineering import FEATURE_NAMES
from detection.model_inference import load_models, score_feature_vector


def _trained_classifier(weight: float):
    """A classifier that always predicts probability `weight` for class 1."""
    X = [[0] * len(FEATURE_NAMES), [1] * len(FEATURE_NAMES)]
    y = [0, 1] if weight > 0.5 else [1, 0]
    return RandomForestClassifier(n_estimators=5, random_state=0).fit(X, y)


@pytest.fixture
def model_dir(tmp_path):
    joblib.dump(_trained_classifier(0.9), tmp_path / "random_forest.joblib")
    joblib.dump(_trained_classifier(0.9), tmp_path / "xgboost.joblib")
    joblib.dump(_trained_classifier(0.9), tmp_path / "lightgbm.joblib")
    return str(tmp_path)


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
    joblib.dump(_trained_classifier(0.9), tmp_path / "random_forest.joblib")
    models = load_models(str(tmp_path))
    assert set(models.keys()) == {"random_forest"}


class FixedProbabilityModel:
    def __init__(self, probability: float):
        self.probability = probability

    def predict_proba(self, _X):
        return np.array([[1.0 - self.probability, self.probability]])


def test_score_feature_vector_uses_runtime_settings_weights(monkeypatch):
    monkeypatch.setattr(
        model_inference,
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
        model_inference,
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
