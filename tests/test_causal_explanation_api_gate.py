"""API-level tests for the redesigned causal-explanation refutation gate.

See tests/test_causal_refutation_gate.py for the engine-level tests and full
background on the two defects being fixed:

1. ``api/main.py``'s refutation gate compared ``all_failing`` (capped at 3,
   since only 3 refutation tests ever ran against a single hardcoded
   treatment) against a threshold of exactly 3 with strict ``>`` — making
   rejection mathematically impossible regardless of how badly the causal
   model failed refutation.
2. ``CausalEngine.estimate_ate()`` silently substituted a correlational OLS
   coefficient for a failed/non-identifiable DoWhy estimate, indistinguishable
   from a genuine causal estimate in the API response.

These tests exercise the actual FastAPI endpoint via ``TestClient``, with the
causal engine faked out (``_FakeEngine``) so they don't depend on a real
DoWhy install or a populated database — only on the endpoint's own gating
and response-construction logic in ``api/main.py``.

Split into its own file (rather than combined with the engine-level tests)
because importing ``api.main`` pulls in the full app dependency graph
(tracing, admin router, ZK commitment/py_ecc, etc.) — unrelated optional
dependencies that may not be present in every environment. If this module
fails to collect due to a missing unrelated dependency, that is an
environment/installation issue, not a defect in the gate logic itself; see
tests/test_causal_refutation_gate.py for coverage that has no such
dependency.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from detection.causal_engine import (
    ESTIMATION_CAUSAL,
    ESTIMATION_FALLBACK,
    ATEEstimate,
    OBSERVABLE_FEATURE_NODES,
)
from detection.risk_score import RiskScore

import api.main as main
from api.main import app, CausalExplanationResponse

WALLET = "G" + "A" * 55


class _FakeEngine:
    """Stand-in for CausalEngine at the API layer, controlled per-test."""

    def __init__(self, detailed_ate: dict[str, ATEEstimate], refutation_by_feature: dict[str, dict[str, float]]):
        self._detailed_ate = detailed_ate
        self._refutation_by_feature = refutation_by_feature
        self.refutation_calls: list[list[str]] = []

    def is_fitted(self) -> bool:
        return True

    def feature_ate_table(self, use_cache=True):
        return {feat: est.value for feat, est in self._detailed_ate.items()}

    def feature_ate_table_detailed(self, use_cache=True):
        return dict(self._detailed_ate)

    def all_feature_refutation_tests(self, features=None):
        target = features if features is not None else list(self._detailed_ate.keys())
        self.refutation_calls.append(list(target))
        return {f: self._refutation_by_feature[f] for f in target if f in self._refutation_by_feature}

    def counterfactual_score(self, wallet_features, overrides):
        return 42.0


def _causal_only(value: float = 12.0) -> ATEEstimate:
    return ATEEstimate(value=value, method=ESTIMATION_CAUSAL, identified=True, reason=None)


def _fallback(value: float = 0.5, reason: str = "dowhy_not_installed") -> ATEEstimate:
    return ATEEstimate(value=value, method=ESTIMATION_FALLBACK, identified=False, reason=reason)


@pytest.fixture(autouse=True)
def _reset_rate_limit(monkeypatch):
    monkeypatch.setattr(main, "_causal_rate_buckets", defaultdict(list))


@pytest.fixture
def _fake_score(monkeypatch):
    score = RiskScore(
        wallet=WALLET,
        asset_pair="XLM/USDC",
        score=75,
        benford_flag=True,
        ml_flag=True,
        confidence=90,
        timestamp=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(main, "get_latest_scores", lambda wallet=None, **kw: [score])
    monkeypatch.setattr(main, "get_feature_vector", lambda wallet, pair: None)
    return score


def _client(monkeypatch, engine, _fake_score):
    monkeypatch.setattr(main, "_get_or_fit_causal_engine", lambda: engine)
    return TestClient(app)


def test_causal_explanation_includes_provenance_fields(monkeypatch, _fake_score):
    """Happy path: response must include the new estimation_method /
    estimation_notes / refutation_coverage fields, all consistent with the
    per-feature ATE provenance."""
    detailed = {
        "wash_ring_membership": _causal_only(30.0),
        "account_age_days": _fallback(0.2, reason="dowhy_not_installed"),
    }
    refutations = {"wash_ring_membership": {"random_common_cause": 0.8, "placebo_treatment_refuter": 0.7, "data_subset_refuter": 0.9}}
    engine = _FakeEngine(detailed, refutations)
    client = _client(monkeypatch, engine, _fake_score)

    resp = client.get(f"/scores/{WALLET}/causal-explanation")

    assert resp.status_code == 200
    data = resp.json()
    assert data["estimation_method"] == {
        "wash_ring_membership": "causal",
        "account_age_days": "correlational_fallback",
    }
    assert data["estimation_notes"] == {"account_age_days": "dowhy_not_installed"}
    assert data["refutation_coverage"] == ["wash_ring_membership"]
    assert engine.refutation_calls == [["wash_ring_membership"]]


def test_refutation_gate_rejects_when_all_tests_fail(monkeypatch, _fake_score):
    """THE core regression test: under the old code, no combination of
    refutation results could ever trigger the 503 gate (all_failing was
    capped at 3, the exact value of the threshold, so `>` could never be
    True). Here every refutation test for every causally-identified feature
    fails (p < 0.05); the redesigned fraction-based gate must reject with 503.
    """
    detailed = {feat: _causal_only(10.0) for feat in OBSERVABLE_FEATURE_NODES}
    failing_tests = {"random_common_cause": 0.01, "placebo_treatment_refuter": 0.0, "data_subset_refuter": 0.02}
    refutations = {feat: dict(failing_tests) for feat in OBSERVABLE_FEATURE_NODES}
    engine = _FakeEngine(detailed, refutations)
    client = _client(monkeypatch, engine, _fake_score)

    resp = client.get(f"/scores/{WALLET}/causal-explanation")

    assert resp.status_code == 503
    assert "misspecified" in resp.json()["detail"].lower()


def test_refutation_gate_passes_when_tests_mostly_succeed(monkeypatch, _fake_score):
    """Sanity check: the redesigned gate must not be overly aggressive --
    a model with a small minority of noisy refutation failures (well under
    the 1/3 threshold) must still be served."""
    detailed = {feat: _causal_only(10.0) for feat in OBSERVABLE_FEATURE_NODES}
    refutations = {}
    for i, feat in enumerate(OBSERVABLE_FEATURE_NODES):
        # Only the very first feature's random_common_cause test fails;
        # 1/24 total tests failing is far below the 1/3 threshold.
        if i == 0:
            refutations[feat] = {"random_common_cause": 0.01, "placebo_treatment_refuter": 0.8, "data_subset_refuter": 0.9}
        else:
            refutations[feat] = {"random_common_cause": 0.9, "placebo_treatment_refuter": 0.8, "data_subset_refuter": 0.95}
    engine = _FakeEngine(detailed, refutations)
    client = _client(monkeypatch, engine, _fake_score)

    resp = client.get(f"/scores/{WALLET}/causal-explanation")

    assert resp.status_code == 200


def test_refutation_gate_skipped_when_no_causal_features_identified(monkeypatch, _fake_score):
    """If every feature fell back to correlational estimation (e.g. DoWhy
    entirely unavailable), there is no genuine causal claim being made, so
    there is nothing to refute -- the endpoint must still serve the
    (honestly-labeled) fallback table rather than raising a nonsensical 503
    over zero tests."""
    detailed = {feat: _fallback(1.0) for feat in OBSERVABLE_FEATURE_NODES}
    engine = _FakeEngine(detailed, refutation_by_feature={})
    client = _client(monkeypatch, engine, _fake_score)

    resp = client.get(f"/scores/{WALLET}/causal-explanation")

    assert resp.status_code == 200
    data = resp.json()
    assert all(v == "correlational_fallback" for v in data["estimation_method"].values())
    assert data["refutation_coverage"] == []


def test_refutation_coverage_matches_feature_ate_table(monkeypatch, _fake_score):
    """Refutation testing must cover every feature reported in
    feature_ate_table that has a genuine causal estimate -- not a single
    hardcoded treatment."""
    detailed = {feat: _causal_only(float(i)) for i, feat in enumerate(OBSERVABLE_FEATURE_NODES)}
    refutations = {feat: {"random_common_cause": 0.9, "placebo_treatment_refuter": 0.8, "data_subset_refuter": 0.9} for feat in OBSERVABLE_FEATURE_NODES}
    engine = _FakeEngine(detailed, refutations)
    client = _client(monkeypatch, engine, _fake_score)

    resp = client.get(f"/scores/{WALLET}/causal-explanation")

    assert resp.status_code == 200
    data = resp.json()
    assert set(data["refutation_coverage"]) == set(data["feature_ate_table"].keys())
    assert set(engine.refutation_calls[0]) == set(OBSERVABLE_FEATURE_NODES)


def test_non_identifiability_surfaced_not_masked(monkeypatch, _fake_score):
    """A non-identifiable feature must appear in the response as an explicit,
    labeled fallback with its reason -- never silently substituted under the
    same schema as a genuine causal estimate."""
    detailed = {
        "wash_ring_membership": _causal_only(30.0),
        "network_centrality": ATEEstimate(
            value=0.3,
            method=ESTIMATION_FALLBACK,
            identified=False,
            reason="non_identifiable: Causal effect for 'network_centrality' is not identifiable via backdoor/frontdoor/IV criteria.",
        ),
    }
    refutations = {"wash_ring_membership": {"random_common_cause": 0.9, "placebo_treatment_refuter": 0.8, "data_subset_refuter": 0.9}}
    engine = _FakeEngine(detailed, refutations)
    client = _client(monkeypatch, engine, _fake_score)

    resp = client.get(f"/scores/{WALLET}/causal-explanation")

    assert resp.status_code == 200
    data = resp.json()
    assert data["estimation_method"]["network_centrality"] == "correlational_fallback"
    assert "non_identifiable" in data["estimation_notes"]["network_centrality"]
    assert "network_centrality" not in data["refutation_coverage"]


def test_causal_explanation_response_schema_accepts_new_fields():
    """CausalExplanationResponse must accept the new fields while keeping
    them optional (default empty) for backward compatibility with existing
    constructions that omit them entirely."""
    resp = CausalExplanationResponse(
        wallet=WALLET,
        current_score=80,
        feature_ate_table={"wash_ring_membership": 10.0},
        top_causal_features=[("wash_ring_membership", 10.0)],
        counterfactual_score=None,
        coverage_note="note",
        estimation_method={"wash_ring_membership": "causal"},
        estimation_notes={},
        refutation_coverage=["wash_ring_membership"],
    )
    assert resp.estimation_method == {"wash_ring_membership": "causal"}

    # Omitting the new fields entirely (as pre-fix callers/tests do) must
    # still work, defaulting to empty.
    resp2 = CausalExplanationResponse(
        wallet=WALLET,
        current_score=80,
        feature_ate_table={},
        top_causal_features=[],
        counterfactual_score=None,
        coverage_note="note",
    )
    assert resp2.estimation_method == {}
    assert resp2.estimation_notes == {}
    assert resp2.refutation_coverage == []
