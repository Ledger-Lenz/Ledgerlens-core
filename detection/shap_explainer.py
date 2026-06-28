"""SHAP-based interpretability for individual risk scores.

Given a trained model and a feature vector, returns the per-feature SHAP
values so the API/dashboard can show *why* a wallet received its score.

Also provides waterfall-style explanations via :class:`ShapExplainer` with
TTL-based caching keyed on (wallet, model_version) in the feature store.
"""

import json
import logging
import time
from dataclasses import dataclass

import numpy as np
import shap

logger = logging.getLogger("ledgerlens.shap_explainer")

# ---------------------------------------------------------------------------
# Default cache TTL in seconds (1 hour)
# ---------------------------------------------------------------------------
DEFAULT_CACHE_TTL_SECONDS = 3600

# ---------------------------------------------------------------------------
# Valid model names accepted by the explainer
# ---------------------------------------------------------------------------
VALID_MODEL_NAMES = frozenset({"random_forest", "xgboost", "lightgbm"})


@dataclass
class FeatureContribution:
    """A single feature's SHAP contribution in a waterfall explanation."""

    feature: str
    shap_value: float
    rank: int


@dataclass
class ShapExplanation:
    """Waterfall-style SHAP explanation for a single wallet risk score.

    Includes the SHAP base value (expected model output), per-feature
    contributions sorted by absolute magnitude descending, and a
    human-readable summary sentence naming the top-3 features.
    """

    wallet: str
    model_version: str
    base_value: float
    contributions: list[FeatureContribution]
    summary_sentence: str
    model_name: str


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


# ---------------------------------------------------------------------------
# ShapExplainer — waterfall-style explanations with TTL-based caching
# ---------------------------------------------------------------------------


def _build_summary_sentence(
    top_features: list[tuple[str, float]], model_name: str
) -> str:
    """Build a human-readable summary sentence naming the top-3 features.

    Each feature's SHAP value direction is described as either "increasing"
    (positive SHAP pushes score higher, i.e. more risky) or "decreasing"
    (negative SHAP pushes score lower, i.e. less risky) the risk score.

    Parameters
    ----------
    top_features:
        List of (feature_name, shap_value) tuples sorted by absolute
        magnitude descending, with at most 3 entries used.
    model_name:
        Display name of the model used (e.g. "Random Forest").

    Returns
    -------
    A grammatically correct English sentence.
    """
    top = top_features[:3]
    parts: list[str] = []
    for feature, value in top:
        direction = "increasing" if value >= 0 else "decreasing"
        parts.append(f"{feature} ({'+' if value >= 0 else ''}{value:.2f}, {direction})")

    if not parts:
        return f"{model_name} model found no significant feature contributions."

    joined = ", ".join(parts)
    return f"{model_name} risk score is driven primarily by {joined}."


class ShapExplainer:
    """Produce waterfall-style SHAP explanations with TTL-based caching.

    The cache is an in-memory dict keyed on ``(wallet, model_version)``.
    Each entry stores the serialised ``ShapExplanation`` plus a timestamp
    so that entries older than ``cache_ttl_seconds`` are recomputed.

    Only tree-based models (Random Forest, XGBoost, LightGBM) are
    supported because ``shap.TreeExplainer`` requires tree structure.
    """

    def __init__(self, cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        """Initialise the explainer with a configurable cache TTL."""
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._explainer_call_count = 0

    def _cache_key(self, wallet: str, model_version: str) -> tuple[str, str]:
        """Return the stable cache key for (wallet, model_version)."""
        return (wallet, model_version)

    def _read_cache(self, wallet: str, model_version: str) -> ShapExplanation | None:
        """Return a cached explanation if present and not expired, else ``None``."""
        key = self._cache_key(wallet, model_version)
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, serialised = entry
        if time.monotonic() - stored_at > self._cache_ttl:
            del self._cache[key]
            return None
        return ShapExplanation(**json.loads(serialised))

    def _write_cache(self, explanation: ShapExplanation) -> None:
        """Store an explanation in the cache."""
        key = self._cache_key(explanation.wallet, explanation.model_version)
        serialised = json.dumps({
            "wallet": explanation.wallet,
            "model_version": explanation.model_version,
            "base_value": explanation.base_value,
            "contributions": [
                {"feature": c.feature, "shap_value": c.shap_value, "rank": c.rank}
                for c in explanation.contributions
            ],
            "summary_sentence": explanation.summary_sentence,
            "model_name": explanation.model_name,
        })
        self._cache[key] = (time.monotonic(), serialised)

    def explain(
        self,
        model,
        feature_vector: dict,
        *,
        wallet: str,
        model_version: str,
        model_name: str,
    ) -> ShapExplanation:
        """Compute a waterfall-style SHAP explanation for *wallet*.

        Parameters
        ----------
        model:
            A trained tree-based model (RandomForest, XGBoost, or
            LightGBM) compatible with ``shap.TreeExplainer``.
        feature_vector:
            Dictionary mapping feature names to float values for the
            wallet being explained.
        wallet:
            Stellar wallet address (used as part of the cache key).
        model_version:
            Model version string (used as part of the cache key).
        model_name:
            Human-readable model name for the summary sentence
            (e.g. ``"Random Forest"``).

        Returns
        -------
        ShapExplanation
            Waterfall explanation with base value, ranked contributions,
            and a summary sentence.

        Notes
        -----
        The cache is checked first.  On a cache hit the stored
        explanation is returned immediately without invoking SHAP.
        """
        cached = self._read_cache(wallet, model_version)
        if cached is not None:
            return cached

        feature_names = sorted(feature_vector.keys())
        X = np.array([[feature_vector[name] for name in feature_names]])

        explainer = shap.TreeExplainer(model)
        self._explainer_call_count += 1

        if isinstance(explainer.expected_value, list):
            base_value = float(explainer.expected_value[1])
        elif isinstance(explainer.expected_value, np.ndarray):
            if explainer.expected_value.size == 2:
                base_value = float(explainer.expected_value[1])
            else:
                base_value = float(explainer.expected_value[0])
        else:
            base_value = float(explainer.expected_value)

        shap_values_raw = explainer.shap_values(X)

        if isinstance(shap_values_raw, list):
            values = shap_values_raw[1][0]
        elif shap_values_raw.ndim == 3:
            values = shap_values_raw[0, :, 1]
        else:
            values = shap_values_raw[0]

        raw_pairs = list(zip(feature_names, (float(v) for v in values)))
        sorted_pairs = sorted(raw_pairs, key=lambda kv: abs(kv[1]), reverse=True)

        contributions = [
            FeatureContribution(feature=name, shap_value=val, rank=i + 1)
            for i, (name, val) in enumerate(sorted_pairs)
        ]

        summary = _build_summary_sentence(
            [(c.feature, c.shap_value) for c in contributions[:3]], model_name
        )

        explanation = ShapExplanation(
            wallet=wallet,
            model_version=model_version,
            base_value=base_value,
            contributions=contributions,
            summary_sentence=summary,
            model_name=model_name,
        )

        self._write_cache(explanation)

        return explanation
