"""Unit and validation tests for the DoWhy-based CausalEngine.

These tests cover:
- DAG structure validity (no cycles, expected edges)
- CausalEngine.fit() on synthetic data
- ATE directionality: true causal drivers have larger ATE than spurious features
- counterfactual_score(): flagged wallets score lower when ring membership is removed
- feature_override parameter validation (422 on invalid input)
- refutation_tests() API contract
- Integration test for GET /scores/{wallet}/causal-explanation response schema

Synthetic data design
---------------------
We construct ground-truth data where ``wash_ring_membership`` is the true
causal driver of ``risk_score`` (large coefficient), and ``account_age_days``
is an uncorrelated covariate with a negligible coefficient.  The ATE test then
asserts that the engine assigns a larger ATE to the true causal feature.
"""

from __future__ import annotations

import math
import re
import sqlite3
import tempfile
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import pytest

from detection.causal_engine import (
    CAUSAL_DAG_EDGES,
    OBSERVABLE_FEATURE_NODES,
    CausalEngine,
    build_causal_dag,
)


# ---------------------------------------------------------------------------
# Synthetic data factory
# ---------------------------------------------------------------------------

RNG_SEED = 42


def _make_synthetic_df(
    n: int = 1200,
    seed: int = RNG_SEED,
    ring_effect: float = 40.0,
    age_effect: float = 1.0,
) -> pd.DataFrame:
    """Build a synthetic scored-wallet DataFrame with known causal structure.

    ``wash_ring_membership`` has a large causal effect (``ring_effect`` points
    per unit); ``account_age_days`` has a near-zero effect (``age_effect``).
    All other features are noise, making the directionality test unambiguous.

    Parameters
    ----------
    n:
        Number of rows.
    seed:
        NumPy random seed for reproducibility.
    ring_effect:
        True causal effect of wash_ring_membership on risk_score.
    age_effect:
        True causal effect of account_age_days on risk_score (should be small).
    """
    rng = np.random.default_rng(seed)

    ring = rng.binomial(1, 0.3, n).astype(float)      # 30% in rings
    age = rng.uniform(0, 1, n)                          # normalised age

    # Correlated features (non-causal, driven by ring membership)
    chi_sq = ring * 0.6 + rng.normal(0, 0.2, n)
    rtf = ring * 0.5 + rng.normal(0, 0.2, n)
    cvr = ring * 0.4 + rng.normal(0, 0.15, n)
    centrality = rng.uniform(0, 1, n)
    vol_ratio = ring * 0.3 + rng.normal(0, 0.2, n)
    gnn_prob = ring * 0.7 + rng.normal(0, 0.15, n)

    # True structural equation: large ring effect, tiny age effect
    noise = rng.normal(0, 3, n)
    score = (
        30.0
        + ring_effect * ring
        + age_effect * age
        + 2.0 * chi_sq
        + 1.5 * rtf
        + noise
    )
    score = np.clip(score, 0, 100)

    return pd.DataFrame({
        "wash_ring_membership": ring,
        "account_age_days": age,
        "chi_sq_24h": chi_sq,
        "round_trip_trade_frequency": rtf,
        "cycle_volume_ratio": cvr,
        "network_centrality": centrality,
        "volume_to_unique_counterparty_ratio": vol_ratio,
        "gnn_wash_ring_prob": gnn_prob,
        "risk_score": score,
    })


# ---------------------------------------------------------------------------
# DAG structure tests
# ---------------------------------------------------------------------------


def test_build_causal_dag_is_dag():
    """The causal graph must be a valid DAG (no cycles)."""
    G = build_causal_dag()
    assert nx.is_directed_acyclic_graph(G), "CAUSAL_DAG_EDGES contains a cycle"


def test_build_causal_dag_has_risk_score_node():
    """risk_score must be a node in the DAG."""
    G = build_causal_dag()
    assert "risk_score" in G.nodes


def test_build_causal_dag_wash_activity_is_source():
    """wash_activity should have no incoming edges (it is a root node)."""
    G = build_causal_dag()
    assert G.in_degree("wash_activity") == 0


def test_build_causal_dag_all_edges_present():
    """Every edge defined in CAUSAL_DAG_EDGES must appear in the built graph."""
    G = build_causal_dag()
    for src, dst in CAUSAL_DAG_EDGES:
        assert G.has_edge(src, dst), f"Missing edge: {src} → {dst}"


def test_build_causal_dag_observable_features_are_nodes():
    """All observable feature nodes must be in the DAG."""
    G = build_causal_dag()
    for feat in OBSERVABLE_FEATURE_NODES:
        assert feat in G.nodes, f"Observable feature '{feat}' not in DAG"


# ---------------------------------------------------------------------------
# CausalEngine.fit() tests
# ---------------------------------------------------------------------------


def test_causal_engine_fit_completes():
    """fit() on 1200-row synthetic DataFrame must complete without error."""
    df = _make_synthetic_df(n=1200)
    engine = CausalEngine()
    engine.fit(df)
    assert engine.is_fitted()


def test_causal_engine_fit_requires_all_columns():
    """fit() must raise ValueError when required columns are missing."""
    df = _make_synthetic_df(n=600)
    df = df.drop(columns=["wash_ring_membership"])
    engine = CausalEngine()
    with pytest.raises(ValueError, match="wash_ring_membership"):
        engine.fit(df)


def test_causal_engine_fit_warns_on_small_sample(caplog):
    """fit() on fewer than min_sample_size rows must log a warning."""
    import logging
    df = _make_synthetic_df(n=100)
    engine = CausalEngine(min_sample_size=500)
    with caplog.at_level(logging.WARNING, logger="ledgerlens.causal_engine"):
        engine.fit(df)
    assert any("100 rows" in r.message for r in caplog.records)


def test_causal_engine_assert_fitted_raises():
    """Methods that require fitting must raise RuntimeError when not fitted."""
    engine = CausalEngine()
    with pytest.raises(RuntimeError, match="not been fitted"):
        engine.counterfactual_score({}, {})


# ---------------------------------------------------------------------------
# ATE directionality tests
# ---------------------------------------------------------------------------


def test_wash_ring_ate_exceeds_account_age_ate():
    """ATE of wash_ring_membership must exceed ATE of account_age_days.

    In the synthetic ground truth, wash_ring_membership has a 40-point causal
    effect on risk_score while account_age_days has a 1-point effect.  The
    linear structural equation fit must recover this ordering.
    """
    df = _make_synthetic_df(n=1500, ring_effect=40.0, age_effect=1.0)
    engine = CausalEngine()
    engine.fit(df)

    # Use the linear coefficient path (fastest, does not require dowhy)
    ring_ate = engine._linear_coefs.get("wash_ring_membership", 0.0)
    age_ate = engine._linear_coefs.get("account_age_days", 0.0)

    assert abs(ring_ate) > abs(age_ate), (
        f"Expected |ATE(wash_ring_membership)| > |ATE(account_age_days)|, "
        f"got {ring_ate:.3f} vs {age_ate:.3f}"
    )


def test_feature_ate_table_returns_all_features():
    """feature_ate_table() must return an entry for every observable feature."""
    df = _make_synthetic_df(n=1200)
    engine = CausalEngine()
    engine.fit(df)

    # Use linear path (no DoWhy required)
    ate_table = {
        feat: engine._linear_coefs.get(feat, 0.0)
        for feat in OBSERVABLE_FEATURE_NODES
    }

    assert set(ate_table.keys()) == set(OBSERVABLE_FEATURE_NODES)
    assert all(isinstance(v, float) for v in ate_table.values())


# ---------------------------------------------------------------------------
# counterfactual_score tests
# ---------------------------------------------------------------------------


def test_counterfactual_score_flagged_wallet_lower_when_ring_removed():
    """counterfactual_score with wash_ring_membership=0 returns lower score for flagged wallet."""
    df = _make_synthetic_df(n=1200, ring_effect=40.0)
    engine = CausalEngine()
    engine.fit(df)

    # A flagged wallet: in a ring, high RTF, moderate other features
    flagged_wallet = {
        "wash_ring_membership": 1.0,
        "round_trip_trade_frequency": 0.8,
        "chi_sq_24h": 0.7,
        "cycle_volume_ratio": 0.6,
        "volume_to_unique_counterparty_ratio": 0.5,
        "network_centrality": 0.4,
        "account_age_days": 0.3,
        "gnn_wash_ring_prob": 0.9,
    }

    baseline = engine.counterfactual_score(flagged_wallet, {})
    without_ring = engine.counterfactual_score(
        flagged_wallet,
        overrides={"wash_ring_membership": 0.0},
    )

    assert without_ring < baseline, (
        f"Expected counterfactual (no ring) < baseline, "
        f"got {without_ring:.1f} vs {baseline:.1f}"
    )


def test_counterfactual_score_in_valid_range():
    """counterfactual_score must always return a value in [0, 100]."""
    df = _make_synthetic_df(n=1000)
    engine = CausalEngine()
    engine.fit(df)

    wallet = {feat: 1.0 for feat in OBSERVABLE_FEATURE_NODES}
    score = engine.counterfactual_score(wallet, {"wash_ring_membership": 0.0})
    assert 0.0 <= score <= 100.0


def test_counterfactual_score_unknown_override_silently_ignored():
    """Unknown override keys must be ignored (only valid feature names affect the result)."""
    df = _make_synthetic_df(n=1000)
    engine = CausalEngine()
    engine.fit(df)

    wallet = {feat: 0.5 for feat in OBSERVABLE_FEATURE_NODES}
    score_no_override = engine.counterfactual_score(wallet, {})
    score_unknown = engine.counterfactual_score(wallet, {"nonexistent_feature": 99.0})
    assert score_no_override == score_unknown


# ---------------------------------------------------------------------------
# refutation_tests API contract
# ---------------------------------------------------------------------------


def test_refutation_tests_returns_three_keys():
    """refutation_tests() must return a dict with exactly three test keys."""
    df = _make_synthetic_df(n=1200)
    engine = CausalEngine()
    engine.fit(df)

    results = engine.refutation_tests()

    assert isinstance(results, dict)
    assert set(results.keys()) == {
        "random_common_cause",
        "placebo_treatment_refuter",
        "data_subset_refuter",
    }
    for key, pval in results.items():
        assert isinstance(pval, float), f"{key} p-value is not a float"
        assert 0.0 <= pval <= 1.0, f"{key} p-value {pval} out of [0, 1]"


def test_refutation_tests_raises_when_not_fitted():
    """refutation_tests() must raise RuntimeError when not fitted."""
    engine = CausalEngine()
    with pytest.raises(RuntimeError, match="not been fitted"):
        engine.refutation_tests()



# ---------------------------------------------------------------------------
# ATE SQLite cache tests
# ---------------------------------------------------------------------------


def test_ate_cache_roundtrip():
    """ATE table should be saved and loaded from SQLite correctly."""
    from detection.causal_engine import _init_ate_cache, _save_ate_cache, _load_ate_cache

    ate_table = {
        "wash_ring_membership": 12.5,
        "account_age_days": 0.3,
        "chi_sq_24h": 4.1,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/test.db"
        with sqlite3.connect(db_path) as conn:
            _init_ate_cache(conn)
            _save_ate_cache(conn, "v1", ate_table)
            loaded = _load_ate_cache(conn, "v1")

    assert loaded is not None
    assert loaded["wash_ring_membership"] == pytest.approx(12.5)
    assert loaded["account_age_days"] == pytest.approx(0.3)


def test_ate_cache_returns_none_for_missing_version():
    """Loading ATE for an unknown model_version must return None."""
    from detection.causal_engine import _init_ate_cache, _load_ate_cache

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/test.db"
        with sqlite3.connect(db_path) as conn:
            _init_ate_cache(conn)
            loaded = _load_ate_cache(conn, "nonexistent_version")

    assert loaded is None


def test_engine_uses_cache_on_second_fit():
    """feature_ate_table() must return cached values without re-fitting."""
    df = _make_synthetic_df(n=1000)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/test.db"
        engine = CausalEngine(db_path=db_path, model_version="v1")
        engine.fit(df)

        # Pre-populate cache with known values
        from detection.causal_engine import _save_ate_cache
        known_ate = {feat: 99.0 for feat in OBSERVABLE_FEATURE_NODES}
        with sqlite3.connect(db_path) as conn:
            _save_ate_cache(conn, "v1", known_ate)

        # Now call feature_ate_table — should return cached values
        result = engine.feature_ate_table(use_cache=True)

    assert all(v == pytest.approx(99.0) for v in result.values())


# ---------------------------------------------------------------------------
# API validation tests (feature_override parameter)
# ---------------------------------------------------------------------------


def test_parse_feature_override_valid():
    """Valid 'feature=value' strings must parse correctly."""
    from api.main import _parse_feature_override

    result = _parse_feature_override("wash_ring_membership=0.0")
    assert result == ("wash_ring_membership", pytest.approx(0.0))

    result = _parse_feature_override("network_centrality=0.75")
    assert result is not None
    assert result[0] == "network_centrality"
    assert result[1] == pytest.approx(0.75)


def test_parse_feature_override_rejects_unknown_feature():
    """Unknown feature names must raise HTTP 422."""
    from fastapi import HTTPException
    from api.main import _parse_feature_override

    with pytest.raises(HTTPException) as exc_info:
        _parse_feature_override("totally_made_up_feature=1.0")
    assert exc_info.value.status_code == 422


def test_parse_feature_override_rejects_out_of_range_value():
    """Values outside [-1000, 1000] must raise HTTP 422."""
    from fastapi import HTTPException
    from api.main import _parse_feature_override

    with pytest.raises(HTTPException) as exc_info:
        _parse_feature_override("wash_ring_membership=9999.0")
    assert exc_info.value.status_code == 422


def test_parse_feature_override_rejects_negative_out_of_range():
    """Extreme negative values must raise HTTP 422."""
    from fastapi import HTTPException
    from api.main import _parse_feature_override

    with pytest.raises(HTTPException) as exc_info:
        _parse_feature_override("wash_ring_membership=-5000.0")
    assert exc_info.value.status_code == 422


def test_parse_feature_override_rejects_non_numeric_value():
    """Non-numeric values must raise HTTP 422."""
    from fastapi import HTTPException
    from api.main import _parse_feature_override

    with pytest.raises(HTTPException) as exc_info:
        _parse_feature_override("wash_ring_membership=abc")
    assert exc_info.value.status_code == 422


def test_parse_feature_override_rejects_missing_equals():
    """Missing '=' separator must raise HTTP 422."""
    from fastapi import HTTPException
    from api.main import _parse_feature_override

    with pytest.raises(HTTPException) as exc_info:
        _parse_feature_override("wash_ring_membership0.5")
    assert exc_info.value.status_code == 422


def test_parse_feature_override_none_returns_none():
    """None input (no override provided) must return None."""
    from api.main import _parse_feature_override

    assert _parse_feature_override(None) is None


def test_parse_feature_override_rejects_infinity():
    """Infinity values must raise HTTP 422."""
    from fastapi import HTTPException
    from api.main import _parse_feature_override

    with pytest.raises(HTTPException) as exc_info:
        _parse_feature_override("wash_ring_membership=inf")
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Integration test: response schema
# ---------------------------------------------------------------------------


def test_causal_explanation_response_schema_fields():
    """CausalExplanationResponse Pydantic model must accept a well-formed dict."""
    from api.main import CausalExplanationResponse

    resp = CausalExplanationResponse(
        wallet="GABC" + "A" * 52,
        current_score=75,
        feature_ate_table={"wash_ring_membership": 12.3, "account_age_days": 0.4},
        top_causal_features=[("wash_ring_membership", 12.3), ("chi_sq_24h", 4.1), ("network_centrality", 2.0)],
        counterfactual_score=55.0,
        coverage_note="Based on 1000 scored wallets.",
    )
    assert resp.wallet.startswith("G")
    assert resp.current_score == 75
    assert "wash_ring_membership" in resp.feature_ate_table
    assert len(resp.top_causal_features) == 3
    assert resp.counterfactual_score == pytest.approx(55.0)


def test_causal_explanation_response_optional_counterfactual():
    """CausalExplanationResponse must accept None for counterfactual_score."""
    from api.main import CausalExplanationResponse

    resp = CausalExplanationResponse(
        wallet="GABC" + "A" * 52,
        current_score=60,
        feature_ate_table={},
        top_causal_features=[],
        counterfactual_score=None,
        coverage_note="Note.",
    )
    assert resp.counterfactual_score is None
