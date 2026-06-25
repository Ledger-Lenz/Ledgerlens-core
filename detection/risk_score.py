"""The `RiskScore` schema shared with ledgerlens-api and ledgerlens-contracts.

This mirrors the on-chain `RiskScore` struct defined in the
ledgerlens-contracts repo (`ledgerlens-score/src/lib.rs`). Keep the two in
sync — see README.md's "LedgerLens Organization" section for the cross-repo
data contract.

Starting from v2, the schema includes optional uncertainty fields
(``score_lower``, ``score_upper``, ``prediction_set``, ``coverage_guarantee``)
populated by ``ConformalCalibrator`` during inference.
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

    # Streaming latency field (optional, populated on the streaming path)
    latency_ms: float | None = Field(
        default=None,
        description="End-to-end latency in milliseconds from trade receipt to score update",
    )

    # Conformal prediction uncertainty fields (optional, v2+)
    score_lower: float | None = Field(
        default=None, ge=0.0, le=100.0,
        description="Lower bound of 90 % conformal prediction interval",
    )
    score_upper: float | None = Field(
        default=None, ge=0.0, le=100.0,
        description="Upper bound of 90 % conformal prediction interval",
    )
    prediction_set: list[int] | None = Field(
        default=None,
        description="Class indices in the conformal prediction set",
    )
    coverage_guarantee: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Target coverage level (1 - alpha) of the prediction set",
    )

    @classmethod
    def combine(
        cls,
        wallet: str,
        asset_pair: str,
        benford_mad: float,
        benford_mad_threshold: float,
        ml_probability: float,
        ml_confidence: float,
        score_lower: float | None = None,
        score_upper: float | None = None,
        prediction_set: list[int] | None = None,
        coverage_guarantee: float | None = None,
        sandwich_signal: float = 0.0,
        sandwich_weight: float = 0.0,
        pdc_score: float = 0.0,
        pdc_discount_weight: float = 0.0,
        benford_copula_pval: float = 1.0,
        benford_copula_weight: float = 0.0,
    ) -> "RiskScore":
        """Combine Benford metrics and an ML probability into a single score.

        `score` is a 0-100 blend weighted toward the ML probability, with
        the Benford signal acting as a corroborating flag.

        Optional uncertainty fields (``score_lower``, ``score_upper``,
        ``prediction_set``, ``coverage_guarantee``) are passed through to
        the returned ``RiskScore`` when provided.

        `sandwich_signal` (0-1) is an optional price-manipulation signal from
        `detection.sandwich_engine` (e.g. a normalised sandwich frequency or
        profit). It contributes a `sandwich_weight` fraction of the composite
        score; the Benford/ML blend supplies the remaining `1 - sandwich_weight`.
        With the default `sandwich_weight = 0.0` the score is identical to the
        legacy Benford/ML blend.

        `pdc_score` is the wallet's price-discovery contribution from
        `detection.causal_engine.estimate_pdc`. A positive PDC discounts the
        correlational score: `causal_adjustment = max(0.0, pdc_score) * pdc_discount_weight`.
        With the default `pdc_discount_weight = 0.0` the score is unchanged.

        `benford_copula_pval` is the cross-pair multivariate Benford dependence
        p-value. A small p-value adds a `benford_copula_weight` fraction of
        `1 - pval` to the composite score. With the default
        `benford_copula_weight = 0.0` the score is unchanged.
        """
        benford_flag = benford_mad > benford_mad_threshold
        ml_flag = ml_probability >= 0.5

        benford_component = min(benford_mad / benford_mad_threshold, 1.0) * 100 if benford_mad_threshold else 0.0
        ml_component = ml_probability * 100
        base_component = 0.3 * benford_component + 0.7 * ml_component

        sandwich_weight = max(0.0, min(1.0, sandwich_weight))
        sandwich_component = max(0.0, min(1.0, sandwich_signal)) * 100

        score = round((1.0 - sandwich_weight) * base_component + sandwich_weight * sandwich_component)
        copula_weight = max(0.0, min(1.0, benford_copula_weight))
        copula_component = max(0.0, min(1.0, 1.0 - benford_copula_pval)) * 100
        score = round((1.0 - copula_weight) * score + copula_weight * copula_component)
        causal_adjustment = max(0.0, pdc_score) * pdc_discount_weight
        score = round(max(0.0, score - causal_adjustment))
        score = max(0, min(100, score))

        return cls(
            wallet=wallet,
            asset_pair=asset_pair,
            score=score,
            benford_flag=benford_flag,
            ml_flag=ml_flag,
            confidence=round(ml_confidence * 100),
            timestamp=datetime.now(timezone.utc),
            score_lower=score_lower,
            score_upper=score_upper,
            prediction_set=prediction_set,
            coverage_guarantee=coverage_guarantee,
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

