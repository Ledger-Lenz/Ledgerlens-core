"""Real-time risk scoring: load trained models and score a feature vector."""

import os

import joblib
import numpy as np

from config.settings import settings
from detection.feature_engineering import FEATURE_NAMES

_MODEL_FILENAMES = {
    "random_forest": "random_forest.joblib",
    "xgboost": "xgboost.joblib",
    "lightgbm": "lightgbm.joblib",
}

# Ensemble weights; XGBoost is the primary classifier per the project design.
_ENSEMBLE_WEIGHTS = {"random_forest": 0.25, "xgboost": 0.5, "lightgbm": 0.25}


def load_models(model_dir: str | None = None) -> dict:
    """Load all trained models from `model_dir` (defaults to `settings.model_dir`)."""
    model_dir = model_dir or settings.model_dir
    models = {}
    for name, filename in _MODEL_FILENAMES.items():
        path = os.path.join(model_dir, filename)
        if os.path.exists(path):
            models[name] = joblib.load(path)
    if not models:
        raise FileNotFoundError(f"No trained models found in {model_dir}. Run model_training first.")
    return models


def score_feature_vector(models: dict, feature_vector: dict) -> tuple[float, float]:
    """Return `(probability, confidence)` for a single feature vector.

    `probability` is the weighted-average ensemble probability of a wash
    trade pattern. `confidence` is the agreement between models (1.0 =
    full agreement, lower = models disagree).
    """
    X = np.array([[feature_vector[name] for name in FEATURE_NAMES]])

    probabilities = {}
    for name, model in models.items():
        if hasattr(model, "feature_names_in_"):
            ordered = X[:, [FEATURE_NAMES.index(f) for f in model.feature_names_in_]]
        else:
            ordered = X
        probabilities[name] = model.predict_proba(ordered)[0, 1]

    total_weight = sum(_ENSEMBLE_WEIGHTS[n] for n in probabilities)
    weighted_prob = sum(probabilities[n] * _ENSEMBLE_WEIGHTS[n] for n in probabilities) / total_weight

    confidence = 1.0 - float(np.std(list(probabilities.values())))
    return float(weighted_prob), max(0.0, min(1.0, confidence))
