"""Golden-file regression test for the versioned risk-score aggregation contract.

Issue #932 — Formalize risk-score aggregation as a versioned, backward-compatible contract.

This test:
  1. Verifies that RiskScore.score_version equals SCORE_VERSION.
  2. Re-runs every case in tests/score_version_golden.json through RiskScore.combine()
     and asserts the output matches the recorded expected_score.
  3. FAILS if the aggregation formula is changed without bumping SCORE_VERSION and
     regenerating the golden file.

To update the golden file after an intentional formula change (with a version bump):
  pytest tests/test_score_version_contract.py --regen-golden
"""

from __future__ import annotations

import json
import os

import pytest

from detection.risk_score import SCORE_VERSION, RiskScore

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "score_version_golden.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_golden() -> dict:
    with open(GOLDEN_PATH, "r") as f:
        return json.load(f)


def _regen_golden(cases: list[dict]) -> None:
    """Recompute expected_score for every case and overwrite the golden file."""
    data = _load_golden()
    for case, golden_case in zip(cases, data["cases"]):
        inputs = golden_case["inputs"]
        result = RiskScore.combine(
            wallet="REGEN",
            asset_pair="XLM/USDC",
            **inputs,
        )
        golden_case["expected_score"] = result.score
    data["score_version"] = SCORE_VERSION
    with open(GOLDEN_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Fixtures / parametrize
# ---------------------------------------------------------------------------

def _golden_cases() -> list[tuple[str, dict, int]]:
    data = _load_golden()
    return [
        (c["label"], c["inputs"], c["expected_score"])
        for c in data["cases"]
    ]


@pytest.fixture(autouse=True)
def regen_golden_if_requested(request):
    """When --regen-golden is passed, regenerate golden file before tests run."""
    if request.config.getoption("--regen-golden", default=False):
        data = _load_golden()
        _regen_golden(data["cases"])
        pytest.skip("Golden file regenerated — re-run without --regen-golden to validate.")


def pytest_addoption(parser):
    parser.addoption(
        "--regen-golden",
        action="store_true",
        default=False,
        help="Regenerate tests/score_version_golden.json from current formula.",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScoreVersionContract:
    def test_score_version_constant_matches_golden(self):
        """SCORE_VERSION must match what is recorded in the golden file."""
        data = _load_golden()
        assert SCORE_VERSION == data["score_version"], (
            f"SCORE_VERSION ({SCORE_VERSION!r}) does not match golden file "
            f"({data['score_version']!r}). Bump SCORE_VERSION and regenerate "
            "the golden file with: pytest tests/test_score_version_contract.py --regen-golden"
        )

    def test_risk_score_carries_score_version(self):
        """Every RiskScore produced by combine() must carry the current SCORE_VERSION."""
        rs = RiskScore.combine(
            wallet="GTEST",
            asset_pair="XLM/USDC",
            benford_mad=0.01,
            benford_mad_threshold=0.015,
            ml_probability=0.6,
            ml_confidence=0.8,
        )
        assert rs.score_version == SCORE_VERSION

    def test_risk_score_default_version_field(self):
        """RiskScore constructed directly (not via combine) defaults to SCORE_VERSION."""
        from datetime import datetime, timezone
        rs = RiskScore(
            wallet="GTEST",
            asset_pair="XLM/USDC",
            score=50,
            benford_flag=False,
            ml_flag=True,
            confidence=80,
            timestamp=datetime.now(timezone.utc),
        )
        assert rs.score_version == SCORE_VERSION

    @pytest.mark.parametrize("label,inputs,expected_score", _golden_cases())
    def test_golden_case(self, label: str, inputs: dict, expected_score: int):
        """Aggregation output must match the golden file for every recorded case.

        A failure here means the formula changed without a version bump.
        Bump SCORE_VERSION and run with --regen-golden to update the contract.
        """
        result = RiskScore.combine(
            wallet="GOLDEN",
            asset_pair="XLM/USDC",
            **inputs,
        )
        assert result.score == expected_score, (
            f"[{label}] score mismatch: got {result.score}, expected {expected_score}. "
            "If this is an intentional formula change, bump SCORE_VERSION and regenerate: "
            "pytest tests/test_score_version_contract.py --regen-golden"
        )
        # score_version must also be consistent
        assert result.score_version == SCORE_VERSION, (
            f"[{label}] score_version mismatch: got {result.score_version!r}, "
            f"expected {SCORE_VERSION!r}"
        )
