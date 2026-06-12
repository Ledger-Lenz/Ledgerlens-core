"""SHAP-based interpretability for individual risk scores.

Given a trained model and a feature vector, returns the per-feature SHAP
values so the API/dashboard can show *why* a wallet received its score.
"""

import shap


def explain_score(model, feature_vector: dict) -> dict:
    """Return a `{feature_name: shap_value}` mapping for `feature_vector`.

    `model` should be a tree-based model from `detection.model_inference`
    (Random Forest, XGBoost, or LightGBM all support `shap.TreeExplainer`).
    """
    feature_names = sorted(feature_vector.keys())
    X = [[feature_vector[name] for name in feature_names]]

    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X)

    # Binary classifiers may return a list of per-class arrays.
    values = shap_values[1][0] if isinstance(shap_values, list) else shap_values[0]

    return dict(zip(feature_names, (float(v) for v in values)))


def top_contributing_features(explanation: dict, n: int = 5) -> list[tuple[str, float]]:
    """Return the `n` features with the largest absolute SHAP contribution."""
    return sorted(explanation.items(), key=lambda kv: abs(kv[1]), reverse=True)[:n]
