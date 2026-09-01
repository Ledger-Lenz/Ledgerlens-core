#!/usr/bin/env python3
"""Generate tests/fixtures/contract_vectors.json from the canonical Python models.

Usage:
    python scripts/generate_contract_vectors.py

This script is the authoritative source of truth for what the contract fixture
contains. Run it whenever detection/risk_score.py or ingestion/data_models.py
change, then commit the updated fixture file.

The CI script (scripts/check_contract_vectors.py) will fail if the committed
fixture diverges from the canonical Python models.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure we can import from the repo root
repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

from detection.risk_score import RiskScore
from ingestion.data_models import Asset, Trade, TradeType


def _isoformat(dt: datetime) -> str:
    """Return a UTC-aware ISO 8601 string."""
    return dt.isoformat()


def build_contract_vectors() -> dict:
    """Build the canonical contract vectors from live Python models."""

    # -- RiskScore vectors --

    risk_complete = RiskScore(
        wallet="GABCDE1234567890ABCDE1234567890ABCDE1234567890ABCDE123456",
        asset_pair="XLM/USDC",
        score=82,
        benford_flag=True,
        ml_flag=True,
        confidence=91,
        disputed=False,
        timestamp=datetime(2026, 8, 25, 14, 0, 0, tzinfo=timezone.utc),
        latency_ms=47.3,
        score_lower=75.0,
        score_upper=89.0,
        prediction_set=[1],
        coverage_guarantee=0.9,
    )

    risk_minimal = RiskScore(
        wallet="GTEST000000000000000000000000000000000000000000000000000",
        asset_pair="XLM/BTC",
        score=10,
        benford_flag=False,
        ml_flag=False,
        confidence=60,
        disputed=False,
        timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )

    risk_disputed = RiskScore(
        wallet="GDISPUTED00000000000000000000000000000000000000000000000",
        asset_pair="ETH/XLM",
        score=55,
        benford_flag=False,
        ml_flag=True,
        confidence=70,
        disputed=True,
        timestamp=datetime(2026, 6, 15, 9, 30, 0, tzinfo=timezone.utc),
        score_lower=48.0,
        score_upper=62.0,
        prediction_set=[0, 1],
        coverage_guarantee=0.9,
    )

    risk_zero = RiskScore(
        wallet="GCLEAN00000000000000000000000000000000000000000000000000",
        asset_pair="XLM/USDC",
        score=0,
        benford_flag=False,
        ml_flag=False,
        confidence=99,
        disputed=False,
        timestamp=datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc),
        latency_ms=5.0,
        score_lower=0.0,
        score_upper=5.0,
        prediction_set=[0],
        coverage_guarantee=0.9,
    )

    risk_hundred = RiskScore(
        wallet="GWASH000000000000000000000000000000000000000000000000000",
        asset_pair="USDC/XLM",
        score=100,
        benford_flag=True,
        ml_flag=True,
        confidence=100,
        disputed=False,
        timestamp=datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc),
        latency_ms=120.5,
        score_lower=95.0,
        score_upper=100.0,
        prediction_set=[1],
        coverage_guarantee=0.95,
    )

    def risk_to_dict(r: RiskScore) -> dict:
        """Serialize a RiskScore to the canonical wire format."""
        d = r.model_dump(mode="json")
        return d

    # -- Trade vectors --

    trade_orderbook = Trade(
        id="trade-abc-001",
        paging_token="page-001",
        ledger_close_time=datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc),
        base_account="GABCDE1234567890ABCDE1234567890ABCDE1234567890ABCDE123456",
        counter_account="GXYZ000000000000000000000000000000000000000000000000000",
        base_asset=Asset(code="XLM", issuer=None),
        counter_asset=Asset(
            code="USDC",
            issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
        ),
        base_amount=1000.0,
        counter_amount=120.50,
        price=0.1205,
        base_is_seller=True,
        trade_type=TradeType.ORDERBOOK,
        transaction_hash="abc123def456",
        source="stellar",
    )

    trade_lp = Trade(
        id="trade-lp-001",
        paging_token="page-002",
        ledger_close_time=datetime(2026, 8, 25, 12, 30, 0, tzinfo=timezone.utc),
        base_account="GABCDE1234567890ABCDE1234567890ABCDE1234567890ABCDE123456",
        counter_account=None,
        base_asset=Asset(code="XLM", issuer=None),
        counter_asset=Asset(
            code="USDC",
            issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
        ),
        base_amount=500.0,
        counter_amount=60.25,
        price=0.1205,
        base_is_seller=False,
        trade_type=TradeType.LIQUIDITY_POOL,
        liquidity_pool_id="pool-xyz-789",
        transaction_hash="def456ghi789",
        source="stellar",
    )

    # -- Asset vectors --

    asset_xlm = Asset(code="XLM", issuer=None)
    asset_usdc = Asset(
        code="USDC",
        issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
    )
    asset_btc = Asset(
        code="BTC",
        issuer="GAUTUYY2THLF7SGITDFMXJVYH3LHDSMGEAKSBU267M2K7A3W543CKUEF",
    )

    # -- Determine required fields from live models --
    risk_schema = RiskScore.model_json_schema()
    required_risk_fields = list(risk_schema.get("properties", {}).keys())

    return {
        "_contract_version": "1.0.0",
        "_description": (
            "Canonical serialized contract vectors for cross-language schema enforcement. "
            "Python core models (detection/risk_score.py and ingestion/data_models.py) are "
            "authoritative. All language SDKs must deserialize every vector without error and "
            "re-serialize to a payload containing all required fields with the correct types. "
            "See docs/adr/ADR-005-schema-contract-enforcement.md."
        ),
        "_generated_from": "detection/risk_score.py, ingestion/data_models.py",
        "_generation_script": "scripts/generate_contract_vectors.py",

        "risk_score": {
            "_description": "RiskScore contract vectors. Every SDK must deserialize these without error.",
            "complete": {
                "_id": "risk_score_complete",
                "_description": "Full RiskScore with all fields populated, including v2+ uncertainty fields and latency_ms.",
                **risk_to_dict(risk_complete),
            },
            "minimal": {
                "_id": "risk_score_minimal",
                "_description": "Minimal RiskScore: only required/non-optional fields, all optional fields null.",
                **risk_to_dict(risk_minimal),
            },
            "disputed": {
                "_id": "risk_score_disputed",
                "_description": "RiskScore where disputed=true; tests boolean field handling.",
                **risk_to_dict(risk_disputed),
            },
            "score_boundary_zero": {
                "_id": "risk_score_boundary_zero",
                "_description": "RiskScore with score=0 (minimum boundary).",
                **risk_to_dict(risk_zero),
            },
            "score_boundary_hundred": {
                "_id": "risk_score_boundary_hundred",
                "_description": "RiskScore with score=100 (maximum boundary).",
                **risk_to_dict(risk_hundred),
            },
        },

        "risk_score_adversarial": {
            "_description": (
                "Adversarial vectors. Deserializers MUST reject these or map them to a "
                "missing-field error. Tests that divergence detection works, not just that "
                "valid vectors parse."
            ),
            "wrong_field_name": {
                "_id": "risk_score_adversarial_wrong_field",
                "_description": "Uses 'risk_score' instead of 'score'. Strict deserializers must reject this.",
                "wallet": "GADVERSARIAL000000000000000000000000000000000000000000000",
                "asset_pair": "XLM/USDC",
                "risk_score": 75,  # wrong field name
                "benford_flag": True,
                "ml_flag": False,
                "confidence": 80,
                "disputed": False,
                "timestamp": "2026-08-25T12:00:00+00:00",
                "latency_ms": None,
                "score_lower": None,
                "score_upper": None,
                "prediction_set": None,
                "coverage_guarantee": None,
            },
            "score_out_of_range": {
                "_id": "risk_score_adversarial_out_of_range",
                "_description": "score=999 is outside the 0-100 range. Strict deserializers must reject this.",
                "wallet": "GADVERSARIAL000000000000000000000000000000000000000000001",
                "asset_pair": "XLM/USDC",
                "score": 999,  # out of range
                "benford_flag": True,
                "ml_flag": False,
                "confidence": 80,
                "disputed": False,
                "timestamp": "2026-08-25T12:00:00+00:00",
                "latency_ms": None,
                "score_lower": None,
                "score_upper": None,
                "prediction_set": None,
                "coverage_guarantee": None,
            },
        },

        "trade": {
            "_description": "Trade contract vectors.",
            "orderbook": {
                "_id": "trade_orderbook",
                "_description": "Standard orderbook trade with both counterparties present.",
                **trade_orderbook.model_dump(mode="json"),
            },
            "liquidity_pool": {
                "_id": "trade_liquidity_pool",
                "_description": "Liquidity pool trade with no counter_account.",
                **trade_lp.model_dump(mode="json"),
            },
        },

        "asset": {
            "_description": "Asset contract vectors.",
            "native_xlm": {
                "_id": "asset_native_xlm",
                "_description": "Native XLM asset. issuer must be null.",
                **asset_xlm.model_dump(mode="json"),
            },
            "issued_usdc": {
                "_id": "asset_issued_usdc",
                "_description": "Issued credit asset (USDC). issuer must be present.",
                **asset_usdc.model_dump(mode="json"),
            },
            "issued_btc": {
                "_id": "asset_issued_btc",
                "_description": "Issued credit asset (BTC wrapped). issuer must be present.",
                **asset_btc.model_dump(mode="json"),
            },
        },

        "required_risk_score_fields": required_risk_fields,
        "required_trade_fields": [
            "id", "ledger_close_time", "base_account", "base_asset",
            "counter_asset", "base_amount", "counter_amount", "price",
            "base_is_seller", "trade_type", "source",
        ],
        "required_asset_fields": ["code"],
    }


def main() -> None:
    output_path = repo_root / "tests" / "fixtures" / "contract_vectors.json"
    vectors = build_contract_vectors()
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(vectors, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"✅ Written {output_path}")
    print(f"   RiskScore fields: {vectors['required_risk_score_fields']}")


if __name__ == "__main__":
    main()
