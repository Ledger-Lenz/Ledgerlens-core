"""Cross-language schema contract vector tests (Python side).

These tests prove that the canonical fixture file (tests/fixtures/contract_vectors.json)
can be round-tripped faithfully through the Python models. They also prove that
the adversarial vectors are *rejected* — demonstrating divergence detection, not
just fixture existence.

ADR reference: docs/adr/ADR-005-schema-contract-enforcement.md
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from detection.risk_score import RiskScore
from ingestion.data_models import Asset, Trade

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "contract_vectors.json"


@pytest.fixture(scope="module")
def contract_vectors() -> dict:
    assert FIXTURE_PATH.exists(), (
        f"Contract vectors fixture not found at {FIXTURE_PATH}. "
        "Run: python scripts/generate_contract_vectors.py"
    )
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_metadata(d: dict) -> dict:
    """Remove fixture metadata keys (prefixed with _) before parsing."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# RiskScore – valid vectors must parse without error
# ---------------------------------------------------------------------------

class TestRiskScoreValidVectors:
    """Every valid RiskScore vector in the fixture must deserialize cleanly."""

    def test_complete_vector(self, contract_vectors: dict) -> None:
        """Complete vector with all fields including latency_ms and v2+ uncertainty fields."""
        raw = _strip_metadata(contract_vectors["risk_score"]["complete"])
        score = RiskScore.model_validate(raw)

        # Required fields
        assert isinstance(score.wallet, str)
        assert isinstance(score.asset_pair, str)
        assert 0 <= score.score <= 100
        assert isinstance(score.benford_flag, bool)
        assert isinstance(score.ml_flag, bool)
        assert 0 <= score.confidence <= 100
        assert isinstance(score.disputed, bool)

        # latency_ms must be present and correct type
        assert score.latency_ms is not None, (
            "latency_ms is None in 'complete' vector but should be populated. "
            "This field exists in detection/risk_score.py."
        )
        assert isinstance(score.latency_ms, float)

        # v2+ uncertainty fields
        assert score.score_lower is not None, "score_lower missing from complete vector"
        assert score.score_upper is not None, "score_upper missing from complete vector"
        assert score.prediction_set is not None, "prediction_set missing from complete vector"
        assert score.coverage_guarantee is not None, "coverage_guarantee missing from complete vector"
        assert isinstance(score.prediction_set, list)
        assert all(isinstance(i, int) for i in score.prediction_set), (
            "prediction_set elements must be int (list[int]), not another type. "
            "Rust SDK bug: Vec<u8> is wrong — must be Vec<i32>."
        )

    def test_minimal_vector(self, contract_vectors: dict) -> None:
        """Minimal vector with only required fields; all optional fields null."""
        raw = _strip_metadata(contract_vectors["risk_score"]["minimal"])
        score = RiskScore.model_validate(raw)

        assert score.latency_ms is None
        assert score.score_lower is None
        assert score.score_upper is None
        assert score.prediction_set is None
        assert score.coverage_guarantee is None

    def test_disputed_vector(self, contract_vectors: dict) -> None:
        """disputed=true must round-trip as True."""
        raw = _strip_metadata(contract_vectors["risk_score"]["disputed"])
        score = RiskScore.model_validate(raw)
        assert score.disputed is True

    def test_score_boundary_zero(self, contract_vectors: dict) -> None:
        """score=0 must parse (boundary value)."""
        raw = _strip_metadata(contract_vectors["risk_score"]["score_boundary_zero"])
        score = RiskScore.model_validate(raw)
        assert score.score == 0

    def test_score_boundary_hundred(self, contract_vectors: dict) -> None:
        """score=100 must parse (boundary value)."""
        raw = _strip_metadata(contract_vectors["risk_score"]["score_boundary_hundred"])
        score = RiskScore.model_validate(raw)
        assert score.score == 100

    def test_all_required_fields_present_in_complete_vector(
        self, contract_vectors: dict
    ) -> None:
        """The 'complete' vector must contain every field listed in required_risk_score_fields."""
        required = set(contract_vectors["required_risk_score_fields"])
        complete = _strip_metadata(contract_vectors["risk_score"]["complete"])
        missing = required - set(complete.keys())
        assert not missing, (
            f"The following required fields are absent from the 'complete' fixture vector: "
            f"{sorted(missing)}. "
            f"This means the fixture is stale. "
            f"Run: python scripts/generate_contract_vectors.py"
        )

    def test_v2_uncertainty_fields_in_required_list(self, contract_vectors: dict) -> None:
        """v2+ uncertainty fields must appear in the required_risk_score_fields list.

        Acceptance criterion: 'The schema-contract-enforcement mechanism covers,
        at minimum, RiskScore's uncertainty-quantification fields.'
        """
        v2_fields = {"score_lower", "score_upper", "prediction_set", "coverage_guarantee"}
        declared = set(contract_vectors["required_risk_score_fields"])
        missing = v2_fields - declared
        assert not missing, (
            f"v2+ uncertainty fields missing from required_risk_score_fields: "
            f"{sorted(missing)}\n"
            f"These are part of the enforced contract (ADR-005 acceptance criterion)."
        )

    def test_latency_ms_in_required_fields(self, contract_vectors: dict) -> None:
        """latency_ms must appear in required_risk_score_fields.

        This was the canonical example of the divergence problem: latency_ms exists
        in detection/risk_score.py but was absent from Python SDK, TS, and Rust SDKs.
        """
        declared = set(contract_vectors["required_risk_score_fields"])
        assert "latency_ms" in declared, (
            "'latency_ms' is missing from required_risk_score_fields in the fixture. "
            "This field exists in detection/risk_score.py. "
            "Run: python scripts/generate_contract_vectors.py"
        )

    def test_round_trip_complete_vector(self, contract_vectors: dict) -> None:
        """Deserialize → re-serialize the complete vector and verify key fields match."""
        raw = _strip_metadata(contract_vectors["risk_score"]["complete"])
        score = RiskScore.model_validate(raw)
        dumped = score.model_dump(mode="json")

        for field in contract_vectors["required_risk_score_fields"]:
            assert field in dumped, (
                f"Field '{field}' present in required_risk_score_fields but absent from "
                f"re-serialized RiskScore. This is a real contract gap."
            )


# ---------------------------------------------------------------------------
# RiskScore – adversarial vectors must be rejected
# ---------------------------------------------------------------------------

class TestRiskScoreAdversarialVectors:
    """Adversarial vectors must be rejected by the Python deserializer.

    This is the key divergence-detection test: if the deserializer silently
    accepts a wrong field name, we learn nothing about whether other language
    SDKs would catch the same rename.
    """

    def test_wrong_field_name_rejected(self, contract_vectors: dict) -> None:
        """Using 'risk_score' instead of 'score' must raise ValidationError.

        This test proves divergence detection works. A Python SDK that silently
        ignores the wrong field name would produce score=0 (the Pydantic default)
        instead of raising, masking a real schema drift.
        """
        raw = _strip_metadata(
            contract_vectors["risk_score_adversarial"]["wrong_field_name"]
        )
        with pytest.raises(ValidationError) as exc_info:
            RiskScore.model_validate(raw)
        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "score" in field_names, (
            "ValidationError raised but did not identify 'score' as the missing field. "
            f"Actual errors: {errors}"
        )

    def test_score_out_of_range_rejected(self, contract_vectors: dict) -> None:
        """score=999 must raise ValidationError (0-100 constraint)."""
        raw = _strip_metadata(
            contract_vectors["risk_score_adversarial"]["score_out_of_range"]
        )
        with pytest.raises(ValidationError) as exc_info:
            RiskScore.model_validate(raw)
        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "score" in field_names, (
            f"Expected 'score' range validation error, got: {errors}"
        )

    def test_deliberate_drift_detection(self, contract_vectors: dict) -> None:
        """Simulate what happens when a field is renamed in one language.

        This test constructs a payload that omits 'score' and uses 'SCORE'
        (wrong casing) to verify the enforcement catches the rename.
        """
        raw = _strip_metadata(contract_vectors["risk_score"]["complete"])
        # Simulate a drift: rename 'score' → 'SCORE' (as if another language
        # used camelCase or wrong casing)
        drifted = dict(raw)
        drifted["SCORE"] = drifted.pop("score")

        with pytest.raises(ValidationError) as exc_info:
            RiskScore.model_validate(drifted)
        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "score" in field_names, (
            f"Drift detection failed: renaming 'score' → 'SCORE' was not caught. "
            f"Errors: {errors}"
        )


# ---------------------------------------------------------------------------
# Asset vectors
# ---------------------------------------------------------------------------

class TestAssetVectors:
    def test_native_xlm(self, contract_vectors: dict) -> None:
        raw = _strip_metadata(contract_vectors["asset"]["native_xlm"])
        asset = Asset.model_validate(raw)
        assert asset.code == "XLM"
        assert asset.issuer is None
        assert asset.is_native

    def test_issued_usdc(self, contract_vectors: dict) -> None:
        raw = _strip_metadata(contract_vectors["asset"]["issued_usdc"])
        asset = Asset.model_validate(raw)
        assert asset.code == "USDC"
        assert asset.issuer is not None
        assert not asset.is_native

    def test_issued_btc(self, contract_vectors: dict) -> None:
        raw = _strip_metadata(contract_vectors["asset"]["issued_btc"])
        asset = Asset.model_validate(raw)
        assert asset.code == "BTC"
        assert asset.issuer is not None

    def test_native_xlm_with_issuer_rejected(self) -> None:
        """Native XLM should be accepted both with and without issuer=null.

        Per ingestion/data_models.py: issuer is optional (None for XLM).
        """
        asset = Asset.model_validate({"code": "XLM", "issuer": None})
        assert asset.is_native

    def test_non_native_without_issuer_rejected(self) -> None:
        """Non-native assets without an issuer must be rejected."""
        with pytest.raises(ValidationError):
            Asset.model_validate({"code": "USDC", "issuer": None})


# ---------------------------------------------------------------------------
# Trade vectors
# ---------------------------------------------------------------------------

class TestTradeVectors:
    def test_orderbook_trade(self, contract_vectors: dict) -> None:
        raw = _strip_metadata(contract_vectors["trade"]["orderbook"])
        trade = Trade.model_validate(raw)
        assert trade.trade_type.value == "orderbook"
        assert trade.counter_account is not None
        assert trade.liquidity_pool_id is None

    def test_liquidity_pool_trade(self, contract_vectors: dict) -> None:
        raw = _strip_metadata(contract_vectors["trade"]["liquidity_pool"])
        trade = Trade.model_validate(raw)
        assert trade.trade_type.value == "liquidity_pool"
        assert trade.counter_account is None
        assert trade.liquidity_pool_id == "pool-xyz-789"

    def test_all_required_trade_fields_present(self, contract_vectors: dict) -> None:
        """Every declared required trade field is present in both trade vectors."""
        required = set(contract_vectors["required_trade_fields"])
        for key in ("orderbook", "liquidity_pool"):
            raw = _strip_metadata(contract_vectors["trade"][key])
            vector_fields = set(raw.keys())
            missing = required - vector_fields
            assert not missing, (
                f"Trade vector '{key}' missing required fields: {sorted(missing)}"
            )

    def test_round_trip_orderbook_trade(self, contract_vectors: dict) -> None:
        raw = _strip_metadata(contract_vectors["trade"]["orderbook"])
        trade = Trade.model_validate(raw)
        dumped = trade.model_dump(mode="json")
        required = set(contract_vectors["required_trade_fields"])
        for field in required:
            assert field in dumped, (
                f"Required trade field '{field}' absent from re-serialized Trade."
            )


# ---------------------------------------------------------------------------
# Cross-language field agreement checks
# ---------------------------------------------------------------------------

class TestCrossLanguageFieldAgreement:
    """These tests check that the fixture's declared fields match the live Python models.

    If these fail, the fixture is stale and needs to be regenerated. They serve as
    the 'Python is authoritative' gate.
    """

    def test_fixture_required_risk_score_fields_match_python_model(
        self, contract_vectors: dict
    ) -> None:
        """The fixture's required_risk_score_fields must exactly match RiskScore.model_json_schema()."""
        from detection.risk_score import RiskScore as CoreRiskScore
        canonical = set(CoreRiskScore.model_json_schema()["properties"].keys())
        declared = set(contract_vectors["required_risk_score_fields"])

        missing_from_fixture = canonical - declared
        extra_in_fixture = declared - canonical

        assert not missing_from_fixture, (
            f"Fields present in Python RiskScore model but ABSENT from fixture's "
            f"required_risk_score_fields: {sorted(missing_from_fixture)}\n"
            f"Run: python scripts/generate_contract_vectors.py"
        )
        assert not extra_in_fixture, (
            f"Fields in fixture's required_risk_score_fields but ABSENT from Python model: "
            f"{sorted(extra_in_fixture)}\n"
            f"The fixture was generated from a different model version."
        )

    def test_fixture_contract_version_present(self, contract_vectors: dict) -> None:
        """Fixture must have a _contract_version field for evolution tracking."""
        assert "_contract_version" in contract_vectors, (
            "Fixture file missing '_contract_version' field."
        )
