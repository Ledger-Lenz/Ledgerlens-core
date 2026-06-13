import joblib
import pytest
from sklearn.ensemble import RandomForestClassifier

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
