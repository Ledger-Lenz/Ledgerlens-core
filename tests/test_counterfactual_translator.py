"""Tests for detection/counterfactual_translator.py."""

import pytest

from detection.counterfactual_translator import (
    _build_translations,
    translate_counterfactual,
)


def test_build_translations_returns_dict():
    translations = _build_translations()
    assert isinstance(translations, dict)
    assert len(translations) > 50  # we have many features


def test_build_translations_keys_are_strings():
    translations = _build_translations()
    assert all(isinstance(k, str) for k in translations)


def test_build_translations_values_are_non_empty():
    translations = _build_translations()
    assert all(isinstance(v, str) and len(v) > 10 for v in translations.values())


def test_translate_single_feature():
    result = translate_counterfactual({"self_matching_rate": -0.5})
    assert len(result) == 1
    assert "Stop trading against your own accounts" in result[0]


def test_translate_multiple_features():
    deltas = {
        "self_matching_rate": -0.5,
        "order_cancellation_rate": -0.3,
    }
    result = translate_counterfactual(deltas)
    assert len(result) == 2
    assert any("Stop trading against your own accounts" in r for r in result)
    assert any("cancel orders" in r for r in result)


def test_translate_unknown_feature_raises_keyerror():
    with pytest.raises(KeyError):
        translate_counterfactual({"nonexistent_feature": 1.0})


def test_translate_empty_deltas():
    result = translate_counterfactual({})
    assert result == []


def test_translate_benford_features():
    deltas = {"benford_chi_square_1h": -10.0}
    result = translate_counterfactual(deltas)
    assert len(result) == 1
    assert "1-hour" in result[0]
    assert "Benford" in result[0]


def test_translate_benford_features_all_windows():
    """All five Benford windows have translations."""
    for window in ("1h", "4h", "24h", "7d", "30d"):
        result = translate_counterfactual({f"benford_chi_square_{window}": -1.0})
        assert len(result) == 1
        assert len(result[0]) > 10, f"Missing translation for benford_chi_square_{window}"


def test_translate_gnn_features():
    for name in (
        "gnn_wash_ring_probability",
        "gnn_neighbor_avg_score",
        "gnn_asset_mediated_ring_score",
        "gnn_order_cancel_coordination_score",
        "gnn_funding_proximity_score",
    ):
        result = translate_counterfactual({name: -0.5})
        assert len(result) == 1
        assert len(result[0]) > 10


def test_translate_amm_features():
    result = translate_counterfactual({"amm_tenure_ratio": 0.3})
    assert len(result) == 1
    assert "liquidity" in result[0].lower() or "pool" in result[0].lower()


def test_translate_cross_chain_features():
    result = translate_counterfactual({"cross_chain_round_trip_score": -0.5})
    assert len(result) == 1
    assert len(result[0]) > 10


def test_translate_adversarial_features():
    for name in ("evasion_composite_score", "adversarial_feature_score"):
        result = translate_counterfactual({name: -0.5})
        assert len(result) == 1


def test_translate_idempotent():
    """Calling translate_counterfactual twice is safe (lazy init)."""
    r1 = translate_counterfactual({"self_matching_rate": -0.5})
    r2 = translate_counterfactual({"self_matching_rate": -0.5})
    assert r1 == r2
