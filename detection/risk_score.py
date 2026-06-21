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
    ) -> "RiskScore":
        """Combine Benford metrics and an ML probability into a single score.

        `score` is a 0-100 blend weighted toward the ML probability, with
        the Benford signal acting as a corroborating flag.
        """
        benford_flag = benford_mad > benford_mad_threshold
        ml_flag = ml_probability >= 0.5

        benford_component = min(benford_mad / benford_mad_threshold, 1.0) * 100 if benford_mad_threshold else 0.0
        ml_component = ml_probability * 100

        score = round(0.3 * benford_component + 0.7 * ml_component)
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


def temporal_risk_adjustment(
    snapshot_score: int,
    temporal_score: float | None,
    history_days: int,
    temporal_weight: float = 0.3,
) -> int:
    """Blend temporal risk probability (0-1) and snapshot score (0-100).

    When a wallet has < 7 days of history, or temporal_score is None,
    fall back to snapshot-only mode.
    """
    if history_days < 7 or temporal_score is None:
        return snapshot_score

    snapshot_weight = 1.0 - temporal_weight
    final_score = snapshot_weight * snapshot_score + temporal_weight * (temporal_score * 100.0)
    return max(0, min(100, round(final_score)))

