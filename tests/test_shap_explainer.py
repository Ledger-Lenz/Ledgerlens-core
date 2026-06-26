import time
from unittest.mock import patch

from sklearn.ensemble import RandomForestClassifier

from detection.shap_explainer import (
    FeatureContribution,
    ShapExplainer,
    ShapExplanation,
    _build_summary_sentence,
    explain_score,
    top_contributing_features,
)


def _trained_model():
    X = [[0, 0], [0, 1], [1, 0], [1, 1]] * 5
    y = [0, 0, 0, 1] * 5
    model = RandomForestClassifier(n_estimators=10, random_state=0)
    model.fit(X, y)
    return model


def test_explain_score_returns_value_per_feature():
    model = _trained_model()
    feature_vector = {"feature_a": 1.0, "feature_b": 0.0}

    explanation = explain_score(model, feature_vector)

    assert set(explanation.keys()) == {"feature_a", "feature_b"}
    assert all(isinstance(v, float) for v in explanation.values())


def test_top_contributing_features_orders_by_absolute_value():
    explanation = {"a": 0.1, "b": -0.9, "c": 0.5}

    top = top_contributing_features(explanation, n=2)

    assert top[0][0] == "b"
    assert top[1][0] == "c"
    assert len(top) == 2


# ---------------------------------------------------------------------------
# ShapExplainer waterfall tests
# ---------------------------------------------------------------------------


def test_shap_explainer_returns_waterfall_explanation():
    """ShapExplainer.explain() returns ShapExplanation with correct structure."""
    model = _trained_model()
    fv = {"feature_a": 1.0, "feature_b": 0.0}
    explainer = ShapExplainer()

    result = explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")

    assert isinstance(result, ShapExplanation)
    assert result.wallet == "GABCD"
    assert result.model_version == "v1"
    assert isinstance(result.base_value, float)
    assert len(result.contributions) >= 1
    assert result.contributions[0].rank == 1
    assert isinstance(result.summary_sentence, str)


def test_shap_explainer_contributions_sorted_descending():
    """Contributions are sorted by absolute SHAP value descending."""
    model = _trained_model()
    fv = {"feature_a": 1.0, "feature_b": 0.0}
    explainer = ShapExplainer()

    result = explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")

    abs_vals = [abs(c.shap_value) for c in result.contributions]
    assert abs_vals == sorted(abs_vals, reverse=True)


def test_shap_explainer_ranks_contiguous():
    """Rank field is 1-indexed and contiguous."""
    model = _trained_model()
    fv = {"feature_a": 1.0, "feature_b": 0.0}
    explainer = ShapExplainer()

    result = explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")

    ranks = [c.rank for c in result.contributions]
    assert ranks == list(range(1, len(result.contributions) + 1))


def test_build_summary_sentence_names_top3():
    """Summary sentence includes top-3 features with signed SHAP values."""
    top = [("wash_ring_membership", 0.42), ("round_trip_freq", 0.31), ("centrality", -0.18)]
    sentence = _build_summary_sentence(top, "Random Forest")
    assert "Random Forest" in sentence
    assert "wash_ring_membership" in sentence
    assert "+0.42" in sentence
    assert "round_trip_freq" in sentence
    assert "+0.31" in sentence
    assert "centrality" in sentence
    assert "-0.18" in sentence


def test_build_summary_sentence_handles_empty():
    sentence = _build_summary_sentence([], "XGBoost")
    assert "XGBoost" in sentence
    assert "no significant feature contributions" in sentence


def test_build_summary_sentence_direction_words():
    top = [("feat_a", 0.5), ("feat_b", -0.3)]
    sentence = _build_summary_sentence(top, "LightGBM")
    assert "increasing" in sentence
    assert "decreasing" in sentence


def test_shap_explainer_cache_hit_single_explainer_call():
    """Calling explain() twice with same inputs uses cache — single TreeExplainer call."""
    model = _trained_model()
    fv = {"feature_a": 1.0, "feature_b": 0.0}
    explainer = ShapExplainer()

    result1 = explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")
    result2 = explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")

    assert result1.base_value == result2.base_value
    assert result1.summary_sentence == result2.summary_sentence
    assert explainer._explainer_call_count == 1


def test_shap_explainer_cache_miss_after_ttl():
    """Cache expires after TTL — TreeExplainer is called again."""
    model = _trained_model()
    fv = {"feature_a": 1.0, "feature_b": 0.0}
    explainer = ShapExplainer(cache_ttl_seconds=0)

    explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")
    explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")

    assert explainer._explainer_call_count == 2


def test_shap_explainer_different_wallets_independent():
    """Different wallets produce distinct cache entries."""
    model = _trained_model()
    fv = {"feature_a": 1.0, "feature_b": 0.0}
    explainer = ShapExplainer()

    explainer.explain(model, fv, wallet="GABCD", model_version="v1", model_name="Test")
    explainer.explain(model, fv, wallet="GXYZ", model_version="v1", model_name="Test")

    assert explainer._explainer_call_count == 2


def test_feature_contribution_dataclass():
    """FeatureContribution dataclass is importable and assignable."""
    fc = FeatureContribution(feature="wash_ring_membership", shap_value=0.42, rank=1)
    assert fc.feature == "wash_ring_membership"
    assert fc.shap_value == 0.42
    assert fc.rank == 1
