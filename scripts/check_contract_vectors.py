#!/usr/bin/env python3
"""CI gate: verify tests/fixtures/contract_vectors.json is consistent with
the canonical Python models (detection/risk_score.py, ingestion/data_models.py).

Exit codes:
  0  All checks pass.
  1  Drift detected: the fixture does not match the live Python models.

Usage (called by CI):
    python scripts/check_contract_vectors.py

This script intentionally has no dependencies beyond the stdlib and the
project's own models -- it must run in the same environment as the project
tests without additional pip installs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

FIXTURE_PATH = repo_root / "tests" / "fixtures" / "contract_vectors.json"
EXIT_OK = 0
EXIT_DRIFT = 1


def load_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_canonical_risk_score_fields() -> set[str]:
    """Extract the complete field set from the live Python RiskScore model."""
    from detection.risk_score import RiskScore
    schema = RiskScore.model_json_schema()
    return set(schema.get("properties", {}).keys())


def get_canonical_asset_fields() -> set[str]:
    from ingestion.data_models import Asset
    schema = Asset.model_json_schema()
    return set(schema.get("properties", {}).keys())


def get_canonical_trade_fields() -> set[str]:
    """Fields that must always be present: truly required fields, plus fields
    with a real (non-None) default like trade_type/source. Excludes fields
    typed as `X | None = None`, which are legitimately absent depending on
    trade_type (see Trade's docstring)."""
    from ingestion.data_models import Trade
    return {
        name for name, field in Trade.model_fields.items()
        if field.default is not None
    }


def check_fixture_fields(
    fixture: dict,
    canonical_fields: set[str],
    schema_name: str,
    fixture_required_key: str,
) -> list[str]:
    """Compare the fixture's declared required fields against the canonical model.

    Returns a list of drift messages (empty = no drift).
    """
    errors: list[str] = []
    declared = set(fixture.get(fixture_required_key, []))

    missing_from_fixture = canonical_fields - declared
    extra_in_fixture = declared - canonical_fields

    if missing_from_fixture:
        errors.append(
            f"[{schema_name}] Fields present in Python model but ABSENT from fixture "
            f"'{fixture_required_key}': {sorted(missing_from_fixture)}\n"
            f"  → Run 'python scripts/generate_contract_vectors.py' to regenerate the fixture."
        )
    if extra_in_fixture:
        errors.append(
            f"[{schema_name}] Fields declared in fixture '{fixture_required_key}' but ABSENT "
            f"from Python model: {sorted(extra_in_fixture)}\n"
            f"  → The fixture was generated from a newer/different model. "
            f"Update the Python model or regenerate the fixture."
        )
    return errors


def check_vector_fields(
    vector: dict,
    canonical_fields: set[str],
    schema_name: str,
    vector_id: str,
    check_extra: bool = True,
) -> list[str]:
    """Check that a specific JSON vector contains all canonical fields."""
    errors: list[str] = []
    # Strip metadata keys (prefixed with _)
    vector_fields = {k for k in vector if not k.startswith("_")}

    missing = canonical_fields - vector_fields
    if missing:
        errors.append(
            f"[{schema_name}] Vector '{vector_id}' is missing fields: {sorted(missing)}"
        )
    if check_extra:
        extra = vector_fields - canonical_fields
        if extra:
            errors.append(
                f"[{schema_name}] Vector '{vector_id}' has extra fields not in canonical model: "
                f"{sorted(extra)}"
            )
    return errors


def check_risk_score_vectors(fixture: dict, canonical_fields: set[str]) -> list[str]:
    """Check all RiskScore vectors in the fixture."""
    errors: list[str] = []
    rs = fixture.get("risk_score", {})
    for key, vector in rs.items():
        if key.startswith("_") or not isinstance(vector, dict):
            continue
        vid = vector.get("_id", key)
        errors.extend(
            check_vector_fields(vector, canonical_fields, "RiskScore", vid, check_extra=False)
        )
    return errors


def check_asset_vectors(fixture: dict, canonical_fields: set[str]) -> list[str]:
    errors: list[str] = []
    assets = fixture.get("asset", {})
    for key, vector in assets.items():
        if key.startswith("_") or not isinstance(vector, dict):
            continue
        vid = vector.get("_id", key)
        # Asset only requires 'code'; issuer is optional
        required = {"code"}
        vector_fields = {k for k in vector if not k.startswith("_")}
        missing = required - vector_fields
        if missing:
            errors.append(f"[Asset] Vector '{vid}' missing required fields: {sorted(missing)}")
    return errors


def check_trade_vectors(fixture: dict, canonical_fields: set[str]) -> list[str]:
    errors: list[str] = []
    required_trade = set(fixture.get("required_trade_fields", []))
    trades = fixture.get("trade", {})
    for key, vector in trades.items():
        if key.startswith("_") or not isinstance(vector, dict):
            continue
        vid = vector.get("_id", key)
        vector_fields = {k for k in vector if not k.startswith("_")}
        missing = required_trade - vector_fields
        if missing:
            errors.append(f"[Trade] Vector '{vid}' missing required fields: {sorted(missing)}")
    return errors


def check_v2_uncertainty_fields(fixture: dict) -> list[str]:
    """Explicitly verify that v2+ uncertainty fields are present in the fixture.

    Per ADR-005 acceptance criteria: 'The schema-contract-enforcement mechanism
    covers, at minimum, RiskScore's uncertainty-quantification fields.'
    """
    errors: list[str] = []
    v2_fields = {"score_lower", "score_upper", "prediction_set", "coverage_guarantee"}
    declared = set(fixture.get("required_risk_score_fields", []))
    missing = v2_fields - declared
    if missing:
        errors.append(
            f"[RiskScore v2+] Uncertainty fields missing from fixture required fields: "
            f"{sorted(missing)}\n"
            f"  → These fields are part of the enforced contract per ADR-005."
        )
    # Also check the 'complete' vector explicitly
    complete = fixture.get("risk_score", {}).get("complete", {})
    for field in v2_fields:
        if field not in complete:
            errors.append(
                f"[RiskScore v2+] Field '{field}' absent from 'complete' vector."
            )
    return errors


def check_latency_ms_present(fixture: dict) -> list[str]:
    """Explicitly verify that latency_ms is declared in the fixture required fields."""
    errors: list[str] = []
    declared = set(fixture.get("required_risk_score_fields", []))
    if "latency_ms" not in declared:
        errors.append(
            "[RiskScore] 'latency_ms' is missing from required_risk_score_fields in the fixture.\n"
            "  → This field exists in detection/risk_score.py but is absent from the fixture.\n"
            "  → Run 'python scripts/generate_contract_vectors.py' to regenerate."
        )
    return errors


def main() -> int:
    print(f"Checking contract vectors: {FIXTURE_PATH}")

    try:
        fixture = load_fixture()
    except FileNotFoundError:
        print(f"❌ Fixture file not found: {FIXTURE_PATH}")
        print("  → Run 'python scripts/generate_contract_vectors.py' to create it.")
        return EXIT_DRIFT
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in fixture: {e}")
        return EXIT_DRIFT

    # Load canonical fields from live Python models
    try:
        canonical_rs_fields = get_canonical_risk_score_fields()
        canonical_asset_fields = get_canonical_asset_fields()
        canonical_trade_fields = get_canonical_trade_fields()
    except ImportError as e:
        print(f"❌ Failed to import Python models: {e}")
        return EXIT_DRIFT

    all_errors: list[str] = []

    # 1. Verify the fixture's declared required fields match the live models
    all_errors.extend(
        check_fixture_fields(
            fixture, canonical_rs_fields, "RiskScore", "required_risk_score_fields"
        )
    )
    all_errors.extend(
        check_fixture_fields(
            fixture, canonical_trade_fields, "Trade", "required_trade_fields"
        )
    )

    # 2. Check each RiskScore vector contains the canonical fields
    all_errors.extend(check_risk_score_vectors(fixture, canonical_rs_fields))

    # 3. Check asset and trade vectors
    all_errors.extend(check_asset_vectors(fixture, canonical_asset_fields))
    all_errors.extend(check_trade_vectors(fixture, canonical_trade_fields))

    # 4. Explicitly verify v2+ uncertainty fields (acceptance criterion)
    all_errors.extend(check_v2_uncertainty_fields(fixture))

    # 5. Explicitly verify latency_ms (known divergence from audit)
    all_errors.extend(check_latency_ms_present(fixture))

    if all_errors:
        print(f"\n❌ CONTRACT VECTOR DRIFT DETECTED ({len(all_errors)} issue(s)):\n")
        for err in all_errors:
            print(f"  • {err}")
        print(
            "\nTo identify which language implementations are now out of sync:\n"
            "  1. Run: python scripts/generate_contract_vectors.py\n"
            "  2. Review the git diff of tests/fixtures/contract_vectors.json\n"
            "  3. Update the matching fields in:\n"
            "     - sdk/src/schemas.ts (TypeScript/Zod)\n"
            "     - crates/ledgerlens-sdk/src/models.rs (Rust)\n"
            "     - packages/ledgerlens-sdk/src/ledgerlens/models.py (Python SDK)\n"
            "     - proto/ledgerlens/v1/scoring.proto (Proto)\n"
            "  4. Run per-language tests to confirm:\n"
            "     pytest tests/test_contract_vectors.py\n"
            "     cargo test -p ledgerlens-sdk contract_vectors\n"
            "     npx vitest run sdk/tests/contract_vectors.test.ts\n"
        )
        return EXIT_DRIFT

    print(
        f"✅ Contract vectors are consistent with Python models.\n"
        f"   RiskScore fields ({len(canonical_rs_fields)}): "
        f"{sorted(canonical_rs_fields)}\n"
        f"   All required v2+ uncertainty fields present: "
        f"score_lower, score_upper, prediction_set, coverage_guarantee\n"
        f"   latency_ms: present"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
