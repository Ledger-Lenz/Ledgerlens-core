---
title: "Implement Probabilistic Cross-Chain Wallet Linker with Bayesian Confidence Scoring"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/cross_chain_linker.py` to assign probabilistic confidence scores (0–1) to Stellar↔EVM wallet link hypotheses using a Bayesian model over bridge event timing similarity, amount matching (within 0.5%), and address-pattern features. Reject links below confidence=0.7. This replaces the current deterministic linking logic with a principled probabilistic framework that quantifies uncertainty in cross-chain identity assertions and reduces false-positive wallet links.

## Background & Context

LedgerLens already detects cross-chain wash-trade patterns by linking Stellar wallets to EVM counterparts (Ethereum, Base, Polygon) via Allbridge bridge events (`docs/cross_chain_detection.md`). The current implementation matches wallets based on hard thresholds (e.g., bridge events within a fixed time window + amount within some tolerance). This deterministic approach has two weaknesses:

1. **False positives**: two unrelated wallets that happen to bridge similar amounts at similar times can be incorrectly linked, inflating wash-trade risk scores for innocent parties.
2. **Opacity**: the linking decision is binary and provides no uncertainty signal to downstream features or the dispute mechanism.

A Bayesian probabilistic model addresses both weaknesses. Each link hypothesis `H: (stellar_wallet, evm_wallet)` is scored by computing the posterior probability `P(same_entity | evidence)` where the evidence features are:
- **Timing similarity**: how close are the bridge timestamps? (Gaussian likelihood on time delta)
- **Amount matching**: do the bridge amounts match within 0.5% after fee adjustment? (Bernoulli likelihood)
- **Address pattern**: does the EVM address prefix or suffix match known patterns of the Stellar wallet holder? (weak prior, logistic feature)
- **Bridge direction consistency**: does the Stellar→EVM vs EVM→Stellar direction of bridge events match expected round-trip wash-trade patterns?

Links below `confidence=0.7` are rejected and do not contribute to cross-chain ML features. Links above `0.7` but below `0.9` are flagged as `probable` in the cross-chain feature vector. Links above `0.9` are flagged as `confirmed`.

## Objectives

- [ ] Define `WalletLinkHypothesis` dataclass: `stellar_wallet`, `evm_wallet`, `evidence_features` dict, `confidence` float, `link_status` enum (`rejected`, `probable`, `confirmed`), `created_at`.
- [ ] Implement `CrossChainLinker.score_hypothesis(stellar_wallet, evm_wallet, bridge_events) -> WalletLinkHypothesis`.
- [ ] Implement Bayesian scoring: compute log-likelihood ratios for each evidence feature and combine via log-sum; apply logistic function to map to [0, 1] confidence.
- [ ] Implement `_timing_likelihood(time_delta_seconds) -> float` using a Gaussian likelihood with `σ=300s` (5 minutes).
- [ ] Implement `_amount_likelihood(stellar_amount, evm_amount) -> float`: return 1.0 if amounts match within 0.5%, else 0.1.
- [ ] Implement `_direction_consistency_likelihood(bridge_events) -> float` based on bridge direction patterns.
- [ ] Persist accepted hypotheses (`confidence >= 0.7`) to SQLite `cross_chain_links` table.
- [ ] Expose `GET /cross-chain/links/{stellar_wallet}` returning all accepted hypotheses for a Stellar wallet, sorted by confidence descending.
- [ ] Integrate confidence scores into the cross-chain feature vector in `detection/feature_engineering.py`: add `cross_chain_link_confidence` feature (max confidence across all links for the wallet).
- [ ] Add `GET /cross-chain/links/{stellar_wallet}/explain` returning the evidence feature breakdown for each hypothesis.
- [ ] All new code covered by tests with ≥90% branch coverage.

## Technical Requirements

### `WalletLinkHypothesis` dataclass

```python
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Dict

class LinkStatus(str, Enum):
    REJECTED = "rejected"
    PROBABLE = "probable"       # 0.7 <= confidence < 0.9
    CONFIRMED = "confirmed"     # confidence >= 0.9

@dataclass
class WalletLinkHypothesis:
    stellar_wallet: str
    evm_wallet: str
    evidence_features: Dict[str, float]   # feature_name -> raw likelihood value
    log_likelihood_ratio: float           # sum of log-likelihood ratios
    confidence: float                     # sigmoid(log_likelihood_ratio), in [0, 1]
    link_status: LinkStatus
    bridge_event_count: int
    created_at: datetime
```

### `CrossChainLinker` interface

```python
import math

CONFIDENCE_THRESHOLD = 0.70
CONFIRMED_THRESHOLD = 0.90
TIMING_SIGMA_SECONDS = 300.0
AMOUNT_TOLERANCE = 0.005        # 0.5%

class CrossChainLinker:
    def __init__(self, db_path: str, min_confidence: float = CONFIDENCE_THRESHOLD):
        self.db_path = db_path
        self.min_confidence = min_confidence

    def score_hypothesis(
        self,
        stellar_wallet: str,
        evm_wallet: str,
        bridge_events: list["BridgeEvent"],
    ) -> WalletLinkHypothesis:
        """
        Compute Bayesian confidence score for the link hypothesis.
        If no bridge events, returns confidence=0.0 with status=REJECTED.
        """
        if not bridge_events:
            return WalletLinkHypothesis(
                stellar_wallet=stellar_wallet, evm_wallet=evm_wallet,
                evidence_features={}, log_likelihood_ratio=-math.inf,
                confidence=0.0, link_status=LinkStatus.REJECTED,
                bridge_event_count=0, created_at=datetime.utcnow(),
            )
        features = self._compute_features(stellar_wallet, evm_wallet, bridge_events)
        llr = self._log_likelihood_ratio(features)
        confidence = 1.0 / (1.0 + math.exp(-llr))    # sigmoid
        status = self._classify(confidence)
        return WalletLinkHypothesis(
            stellar_wallet=stellar_wallet, evm_wallet=evm_wallet,
            evidence_features=features, log_likelihood_ratio=llr,
            confidence=confidence, link_status=status,
            bridge_event_count=len(bridge_events), created_at=datetime.utcnow(),
        )

    def _timing_likelihood(self, time_delta_seconds: float) -> float:
        """
        Gaussian likelihood: P(delta | same_entity) / P(delta | random)
        = Gaussian(0, sigma) / Uniform(0, 3600)
        """
        gaussian = math.exp(-0.5 * (time_delta_seconds / TIMING_SIGMA_SECONDS) ** 2)
        uniform = 1.0 / 3600.0
        return gaussian / (uniform + 1e-9)

    def _amount_likelihood(self, stellar_amount: float, evm_amount: float) -> float:
        """Returns 10.0 (strong evidence) if within tolerance, else 1.0 (neutral)."""
        if stellar_amount == 0:
            return 1.0
        pct_diff = abs(stellar_amount - evm_amount) / stellar_amount
        return 10.0 if pct_diff <= AMOUNT_TOLERANCE else 1.0

    def _log_likelihood_ratio(self, features: Dict[str, float]) -> float:
        return sum(math.log(max(v, 1e-9)) for v in features.values())

    def _classify(self, confidence: float) -> LinkStatus:
        if confidence >= CONFIRMED_THRESHOLD:
            return LinkStatus.CONFIRMED
        elif confidence >= self.min_confidence:
            return LinkStatus.PROBABLE
        return LinkStatus.REJECTED

    def persist_hypothesis(self, hypothesis: WalletLinkHypothesis) -> None:
        """Persist to cross_chain_links SQLite table if status != REJECTED."""
        ...
```

### SQLite schema

```sql
CREATE TABLE IF NOT EXISTS cross_chain_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stellar_wallet  TEXT NOT NULL,
    evm_wallet      TEXT NOT NULL,
    confidence      REAL NOT NULL,
    link_status     TEXT NOT NULL,
    log_likelihood_ratio REAL NOT NULL,
    evidence_json   TEXT NOT NULL,       -- JSON-encoded evidence_features dict
    bridge_event_count INTEGER NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(stellar_wallet, evm_wallet)   -- upsert on reconviction
);
CREATE INDEX IF NOT EXISTS idx_links_stellar ON cross_chain_links(stellar_wallet);
CREATE INDEX IF NOT EXISTS idx_links_confidence ON cross_chain_links(confidence DESC);
```

### Feature engineering integration (`detection/feature_engineering.py`)

```python
def compute_cross_chain_link_confidence(wallet: str, linker: CrossChainLinker) -> float:
    """
    Returns max confidence across all accepted cross-chain links for this wallet.
    Returns 0.0 if no accepted links exist.
    """
    links = linker.get_accepted_links(wallet)
    if not links:
        return 0.0
    return max(h.confidence for h in links)
```

### API endpoints

```python
@router.get("/cross-chain/links/{stellar_wallet}", response_model=List[WalletLinkOut])
async def get_cross_chain_links(stellar_wallet: str, min_confidence: float = Query(0.7, ge=0.0, le=1.0)):
    """Return accepted cross-chain link hypotheses for a Stellar wallet."""
    ...

@router.get("/cross-chain/links/{stellar_wallet}/explain", response_model=List[LinkExplanationOut])
async def explain_cross_chain_links(stellar_wallet: str):
    """Return evidence feature breakdown for each hypothesis."""
    ...
```

### Priors and calibration

The Bayesian priors should be documented and configurable via `config/settings.py`:
```python
CROSS_CHAIN_TIMING_SIGMA_SECONDS: float = 300.0
CROSS_CHAIN_AMOUNT_TOLERANCE: float = 0.005
CROSS_CHAIN_MIN_CONFIDENCE: float = 0.70
CROSS_CHAIN_CONFIRMED_CONFIDENCE: float = 0.90
```

## Security Considerations

- EVM wallet addresses entering the linker must pass EIP-55 checksum validation (see ISSUE-078) before any processing. Reject malformed addresses with `ValueError`.
- `confidence` is a floating-point value that influences risk scores. Ensure it is clamped to [0.0, 1.0] after sigmoid transformation (numerical edge cases near `-inf` or `+inf` log-likelihood ratios).
- The `evidence_json` field in SQLite stores feature values for audit. These are derived values, not raw transaction data — they reveal LedgerLens's internal model weights. Gate `GET /cross-chain/links/{wallet}/explain` behind admin key for this reason.
- Do not store EVM private keys or raw bridge transaction payloads in the `cross_chain_links` table — only derived features.
- Rate-limit `GET /cross-chain/links/{wallet}` to prevent enumeration of all cross-chain links in the system.

## Testing Requirements

- **Unit — `_timing_likelihood` values**: time_delta=0 yields maximum likelihood; time_delta=600 (2σ) yields lower but positive value; time_delta=3600 yields near-zero.
- **Unit — `_amount_likelihood` boundary**: 0.4% difference → 10.0; 0.5% difference → 10.0 (boundary inclusive); 0.51% → 1.0.
- **Unit — `score_hypothesis` no events**: returns confidence=0.0, status=REJECTED.
- **Unit — `score_hypothesis` strong evidence**: construct bridge events with 0 time delta and exact amount match; assert confidence > 0.9, status=CONFIRMED.
- **Unit — `score_hypothesis` weak evidence**: large time delta, large amount mismatch; assert confidence < 0.7, status=REJECTED.
- **Unit — `persist_hypothesis` upsert**: insert hypothesis; update with higher confidence; assert only one row in DB with new confidence.
- **Unit — feature engineering integration**: mock linker returning confidence=0.85; assert `compute_cross_chain_link_confidence` returns 0.85.
- **Integration — `GET /cross-chain/links/{wallet}` 200**: seed two confirmed links; assert 2 results sorted by confidence.
- **Integration — `min_confidence` filter**: seed one probable (0.75) and one confirmed (0.92); query `?min_confidence=0.9`; assert only confirmed returned.
- **Integration — explain endpoint**: assert response contains `evidence_features` keys matching `_compute_features` output.

## Documentation Requirements

- Docstrings on all public methods of `CrossChainLinker`.
- Update `docs/cross_chain_detection.md` with Bayesian linking methodology, confidence interpretation table, and prior parameter guidance.
- Add `GET /cross-chain/links/{wallet}` and explain endpoint to README API table.
- Document `CROSS_CHAIN_*` configuration variables in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `WalletLinkHypothesis`, `LinkStatus` implemented in `detection/cross_chain_linker.py`.
- [ ] All likelihood functions implemented and mathematically sound.
- [ ] Confidence score correctly derived via logistic function of log-likelihood ratio sum.
- [ ] Links below `min_confidence` are rejected and not persisted.
- [ ] `cross_chain_links` SQLite table created via `db-migrate` with upsert behaviour.
- [ ] `cross_chain_link_confidence` feature integrated into `feature_engineering.py`.
- [ ] `GET /cross-chain/links/{wallet}` and explain endpoints operational.
- [ ] All unit and integration tests pass; ≥90% branch coverage.
- [ ] `docs/cross_chain_detection.md` updated.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience with probabilistic modelling, Bayesian inference, or probabilistic entity resolution — ideally applied to blockchain or financial transaction data. You understand log-likelihood ratios and sigmoid functions for converting evidence to probabilities. Familiarity with LedgerLens's cross-chain detection architecture and Allbridge bridge events is a significant advantage. Experience linking identities across heterogeneous systems (record linkage, entity resolution) translates directly to this work.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., probabilistic modelling, entity resolution, cross-chain analytics, Python backend).
2. **Relevant experience**: Bayesian entity linking, cross-chain wallet analysis, or record linkage systems you have built.
3. **Approach / thoughts**: are the Gaussian timing likelihood and Bernoulli amount likelihood sufficient, or would you add additional evidence features? What is your view on the `σ=300s` timing prior?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
