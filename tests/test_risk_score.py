from detection.risk_score import RiskScore


def test_combine_high_risk_flags_both_signals():
    score = RiskScore.combine(
        wallet="GABC",
        asset_pair="XLM/USDC",
        benford_mad=0.05,
        benford_mad_threshold=0.015,
        ml_probability=0.9,
        ml_confidence=0.95,
    )

    assert score.benford_flag is True
    assert score.ml_flag is True
    assert score.score > 70
    assert score.confidence == 95


def test_combine_low_risk_flags_neither_signal():
    score = RiskScore.combine(
        wallet="GABC",
        asset_pair="XLM/USDC",
        benford_mad=0.001,
        benford_mad_threshold=0.015,
        ml_probability=0.05,
        ml_confidence=0.8,
    )

    assert score.benford_flag is False
    assert score.ml_flag is False
    assert score.score < 30


def test_combine_score_is_clamped_to_0_100():
    score = RiskScore.combine(
        wallet="GABC",
        asset_pair="XLM/USDC",
        benford_mad=10.0,
        benford_mad_threshold=0.015,
        ml_probability=1.0,
        ml_confidence=1.0,
    )

    assert 0 <= score.score <= 100
    assert score.score == 100


def test_combine_zero_threshold_skips_benford_component():
    score = RiskScore.combine(
        wallet="GABC",
        asset_pair="XLM/USDC",
        benford_mad=0.05,
        benford_mad_threshold=0.0,
        ml_probability=0.5,
        ml_confidence=0.5,
    )

    # With a zero threshold, the score is driven entirely by the ML probability.
    assert score.score == round(0.7 * 50)


def test_combine_clamps_score_to_0_100_on_extreme_inputs():
    """Even with out-of-range Benford/ML inputs, the final score stays in [0, 100]."""
    score = RiskScore.combine(
        wallet="GABC",
        asset_pair="XLM/USDC",
        benford_mad=-999.0,
        benford_mad_threshold=0.015,
        ml_probability=-0.5,
        ml_confidence=0.5,
    )
    assert 0 <= score.score <= 100


def test_combine_all_extra_signals_default_to_no_effect():
    """All optional signals default to zero weight — score must equal legacy blend."""
    without_extra = RiskScore.combine(
        wallet="GABC",
        asset_pair="XLM/USDC",
        benford_mad=0.05,
        benford_mad_threshold=0.015,
        ml_probability=0.8,
        ml_confidence=0.9,
    )
    with_extra = RiskScore.combine(
        wallet="GABC",
        asset_pair="XLM/USDC",
        benford_mad=0.05,
        benford_mad_threshold=0.015,
        ml_probability=0.8,
        ml_confidence=0.9,
        # All extra signals explicitly zeroed
        sandwich_signal=0.0,
        sandwich_weight=0.0,
        pdc_score=0.0,
        pdc_discount_weight=0.0,
        benford_copula_pval=1.0,
        benford_copula_weight=0.0,
    )
    assert without_extra.score == with_extra.score


def test_module_uses_future_annotations():
    """Lock in the `from __future__ import annotations` cleanup."""
    import ast
    import detection.risk_score as m
    src = ast.parse(m.__loader__.get_source(m.__name__))
    future_imports = [
        node.names[0].name
        for node in ast.iter_child_nodes(src)
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
        for alias in node.names
    ]
    assert "annotations" in future_imports, \
        "risk_score.py must have `from __future__ import annotations`"
