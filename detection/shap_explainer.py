"""SHAP-based interpretability for individual risk scores.

Given a trained model and a feature vector, returns the per-feature SHAP
values so the API/dashboard can show *why* a wallet received its score.
"""

import numpy as np
import shap


def explain_score(model, feature_vector: dict) -> dict:
    """Return a `{feature_name: shap_value}` mapping for `feature_vector`.

    `model` should be a tree-based model from `detection.model_inference`
    (Random Forest, XGBoost, or LightGBM all support `shap.TreeExplainer`).
    """
    feature_names = sorted(feature_vector.keys())
    X = np.array([[feature_vector[name] for name in feature_names]])

    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X)

    if isinstance(shap_values, list):
        # Older SHAP versions: list of per-class arrays, each (n_samples, n_features).
        values = shap_values[1][0]
    elif shap_values.ndim == 3:
        # Newer SHAP versions: (n_samples, n_features, n_classes).
        values = shap_values[0, :, 1]
    else:
        values = shap_values[0]

    return dict(zip(feature_names, (float(v) for v in values)))


def top_contributing_features(explanation: dict, n: int = 5) -> list[tuple[str, float]]:
    """Return the `n` features with the largest absolute SHAP contribution."""
    return sorted(explanation.items(), key=lambda kv: abs(kv[1]), reverse=True)[:n]


# Causal (price-discovery-contribution) features, mapped to the canonical name
# used in human-readable explanations.
PDC_FEATURES = ("pdc_5m", "pdc_1h", "price_discovery_contribution")


def pdc_annotation(feature: str, value: float) -> dict:
    """Build a human-readable causal annotation for a single PDC feature.

    SHAP attributes *correlation*; PDC attributes *causal* responsibility for
    price discovery. A positive PDC reduces risk (market-making behaviour), a
    negative PDC increases it (price suppression consistent with wash trading).
    """
    if value > 0.0:
        direction = "reduces_risk"
        interpretation = "wallet consistently improves mid-price — consistent with market making"
    elif value < 0.0:
        direction = "increases_risk"
        interpretation = "wallet suppresses price discovery — consistent with wash trading"
    else:
        direction = "neutral"
        interpretation = "wallet has no measurable causal effect on price discovery"

    return {
        "feature": feature,
        "value": float(value),
        "direction": direction,
        "interpretation": interpretation,
    }


def annotate_causal_features(feature_vector: dict) -> list[dict]:
    """Return causal PDC annotations for whichever PDC features are present.

    Lets the API/dashboard show *why* a high-frequency wallet was (or was not)
    discounted, alongside the correlational SHAP values from `explain_score`.
    """
    return [
        pdc_annotation(name, feature_vector[name])
        for name in PDC_FEATURES
        if name in feature_vector
    ]


def explain_score_with_causal(model, feature_vector: dict) -> dict:
    """SHAP explanation plus causal PDC annotations.

    Returns ``{"shap": {feature: shap_value}, "causal": [annotation, ...]}`` so
    consumers get both the correlational attribution and the causal
    interpretation in one call. `explain_score` is left unchanged for callers
    that only need raw SHAP values.
    """
    return {
        "shap": explain_score(model, feature_vector),
        "causal": annotate_causal_features(feature_vector),
    }
