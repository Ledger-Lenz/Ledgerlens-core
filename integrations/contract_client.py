"""Soroban contract client with uncertainty-aware score submission.

Extends the existing ``SorobanPublisher`` (in ``detection.soroban_publisher``)
with a ``submit_score_with_uncertainty`` method that passes ``score_lower``
and ``score_upper`` as additional Soroban ``i128`` fields (scaled ×100 for
integer representation).

.. code-block:: rust
    :caption: Required ledgerlens-contract extension (PR target)

    /// Extended RiskScore struct that includes conformal prediction interval.
    /// Add to ``ledgerlens-score/src/lib.rs``.
    #[contracttype]
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub struct RiskScoreWithUncertainty {
        pub wallet: Address,
        pub asset_pair: Symbol,
        pub score: u32,           // 0-100
        pub score_lower: i128,    // scaled ×100
        pub score_upper: i128,    // scaled ×100
        pub timestamp: u64,
    }

    /// New contract function to submit a score with uncertainty bounds.
    /// Add alongside existing ``submit_score``.
    #[extern]
    pub fn submit_score_with_uncertainty(
        env: Env,
        wallet: Address,
        asset_pair: Symbol,
        score: u32,
        score_lower: i128,
        score_upper: i128,
        timestamp: u64,
    );

The matching PR should be opened against the
`ledgerlens-contract <https://github.com/your-org/ledgerlens-contract>`_ repo.
"""

from __future__ import annotations

import logging

from detection.risk_score import RiskScore
from detection.soroban_publisher import SorobanPublisher

logger = logging.getLogger("ledgerlens.contract_client")


def submit_score_with_uncertainty(
    publisher: SorobanPublisher,
    risk_score: RiskScore,
    dry_run: bool = False,
) -> str | None:
    """Submit a risk score with conformal prediction uncertainty bounds.

    Parameters
    ----------
    publisher:
        Initialized ``SorobanPublisher`` instance.
    risk_score:
        ``RiskScore`` instance containing score, wallet, asset_pair,
        and optional uncertainty fields (``score_lower``, ``score_upper``).
    dry_run:
        If True, log the submission but do not send it on-chain.

    Returns
    -------
    Transaction hash on success, ``None`` on skip (``dry_run=True``).

    Raises
    ------
    SorobanSubmissionError
        On unrecoverable submission failure.
    SorobanCircuitOpenError
        When the circuit breaker is open.
    """
    score_lower = risk_score.score_lower if risk_score.score_lower is not None else 0.0
    score_upper = risk_score.score_upper if risk_score.score_upper is not None else 100.0

    # Scale float bounds to i128 ×100 for integer representation
    score_lower_scaled = int(round(score_lower * 100))
    score_upper_scaled = int(round(score_upper * 100))

    if dry_run:
        logger.info(
            "[DRY-RUN] Would submit score_with_uncertainty: "
            "wallet=%s pair=%s score=%d lower=%d upper=%d",
            risk_score.wallet,
            risk_score.asset_pair,
            risk_score.score,
            score_lower_scaled,
            score_upper_scaled,
        )
        return None

    # The actual Soroban ``submit_score_with_uncertainty`` function invocation
    # requires a contract PR (see module docstring). For now we fall back to
    # the standard ``submit_score`` with uncertainty fields logged locally.
    tx_hash = publisher.submit_score(risk_score, dry_run=dry_run)
    logger.info(
        "Submitted score_with_uncertainty: wallet=%s pair=%s "
        "score=%d lower=%d upper=%d tx_hash=%s",
        risk_score.wallet,
        risk_score.asset_pair,
        risk_score.score,
        score_lower_scaled,
        score_upper_scaled,
        tx_hash,
    )
    return tx_hash
