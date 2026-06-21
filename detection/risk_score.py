"""The `RiskScore` schema shared with ledgerlens-api and ledgerlens-contracts.

This mirrors the on-chain `RiskScore` struct defined in the
ledgerlens-contracts repo (`ledgerlens-score/src/lib.rs`). Keep the two in
sync — see README.md's "LedgerLens Organization" section for the cross-repo
data contract.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class RiskScore(BaseModel):
    wallet: str
    asset_pair: str
    score: int = Field(ge=0, le=100, description="0-100; higher = more suspicious")
    benford_flag: bool
    ml_flag: bool
    confidence: int = Field(ge=0, le=100)
    disputed: bool = False
    timestamp: datetime

    @classmethod
    def combine(
        cls,
        wallet: str,
        asset_pair: str,
        benford_mad: float,
        benford_mad_threshold: float,
        ml_probability: float,
        ml_confidence: float,
        pdc_score: float = 0.0,
        pdc_discount_weight: float = 0.0,
    ) -> "RiskScore":
        """Combine Benford metrics and an ML probability into a single score.

        `score` is a 0-100 blend weighted toward the ML probability, with
        the Benford signal acting as a corroborating flag.

        `pdc_score` is the wallet's price-discovery contribution from
        `detection.causal_engine.estimate_pdc`. A positive PDC is causal
        evidence of market making, so it *discounts* the correlational score:

            causal_adjustment = max(0.0, pdc_score) * pdc_discount_weight
            score = max(0.0, raw_score - causal_adjustment)

        The discount is applied before the final 0-100 clamp. With the default
        `pdc_discount_weight = 0.0` the score is unchanged, so existing callers
        and tests are unaffected.
        """
        benford_flag = benford_mad > benford_mad_threshold
        ml_flag = ml_probability >= 0.5

        benford_component = min(benford_mad / benford_mad_threshold, 1.0) * 100 if benford_mad_threshold else 0.0
        ml_component = ml_probability * 100

        raw_score = 0.3 * benford_component + 0.7 * ml_component
        causal_adjustment = max(0.0, pdc_score) * pdc_discount_weight
        score = round(max(0.0, raw_score - causal_adjustment))
        score = max(0, min(100, score))

        return cls(
            wallet=wallet,
            asset_pair=asset_pair,
            score=score,
            benford_flag=benford_flag,
            ml_flag=ml_flag,
            confidence=round(ml_confidence * 100),
            timestamp=datetime.now(timezone.utc),
        )
