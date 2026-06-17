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


def _get_ensemble_weights() -> dict[str, float]:
    return {
        "random_forest": settings.ensemble_weight_rf,
        "xgboost": settings.ensemble_weight_xgb,
        "lightgbm": settings.ensemble_weight_lgbm,
    }


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

    weights = _get_ensemble_weights()
    total_weight = sum(weights[n] for n in probabilities)
    if total_weight <= 0:
        raise ValueError("At least one loaded model must have a positive ensemble weight.")
    weighted_prob = sum(probabilities[n] * weights[n] for n in probabilities) / total_weight

    confidence = 1.0 - float(np.std(list(probabilities.values())))
    return float(weighted_prob), max(0.0, min(1.0, confidence))


def score_feature_matrix(
    models: dict,
    feature_vectors: list[dict],
) -> list[tuple[float, float]]:
    """Score a batch of feature vectors with a single `predict_proba` call per model.

    For N accounts this makes len(models) predict_proba calls on an N-row
    matrix instead of N × len(models) calls, reducing Python overhead and
    enabling scikit-learn's internal parallelism.

    Returns a list of (probability, confidence) tuples, one per input vector,
    in the same order as `feature_vectors`. Results are numerically identical
    to calling `score_feature_vector` for each vector individually.
    """
    if not feature_vectors:
        return []

    X = np.array([[fv[name] for name in FEATURE_NAMES] for fv in feature_vectors])
    weights = _get_ensemble_weights()

    model_probs: dict[str, np.ndarray] = {}
    for name, model in models.items():
        if hasattr(model, "feature_names_in_"):
            col_idx = [FEATURE_NAMES.index(f) for f in model.feature_names_in_]
            ordered = X[:, col_idx]
        else:
            ordered = X
        model_probs[name] = model.predict_proba(ordered)[:, 1]

    total_weight = sum(weights[n] for n in model_probs)
    if total_weight <= 0:
        raise ValueError("At least one loaded model must have a positive ensemble weight.")

    weighted_probs = sum(model_probs[n] * weights[n] for n in model_probs) / total_weight

    all_probs = np.stack(list(model_probs.values()), axis=0)  # (M, N)
    confidences = np.clip(1.0 - np.std(all_probs, axis=0), 0.0, 1.0)  # (N,)

    return [(float(weighted_probs[i]), float(confidences[i])) for i in range(len(feature_vectors))]
