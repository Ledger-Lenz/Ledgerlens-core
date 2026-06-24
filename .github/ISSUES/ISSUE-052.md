---
title: "Build Adversarial Feature Perturbation Defense Layer"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/adversarial_features.py` to detect and neutralise adversarial inputs: wallets that deliberately set trade amounts, timing, or graph topology to minimise their risk score. Implement feature-space anomaly detection (Isolation Forest on feature vectors) to flag suspiciously "clean" feature profiles — wallets whose features are statistically improbable in the context of their trading volume and counterparty graph, suggesting adversarial manipulation rather than genuine benign behaviour.

## Background & Context

As LedgerLens becomes public and its detection logic is understood by sophisticated adversaries, wash traders will attempt to craft their feature vectors to fall below detection thresholds. The attack surface includes:

1. **Amount camouflage**: perturbing trade amounts to follow a Benford-like distribution while maintaining the same net wash volume (e.g., using a Benford-sampled noise layer on top of round-lot trades)
2. **Timing jitter**: adding random delays to inter-arrival times to mask metronomic bot patterns
3. **Graph fragmentation**: breaking a large wash ring into smaller components just below the SCC threshold, using short-lived relay wallets

The defense is based on a key insight: adversarially "cleaned" feature vectors are anomalous in a different way from genuinely clean wallets. A genuine low-risk wallet has low volume AND low counterparty concentration AND low graph centrality — these co-occur naturally. An adversarially cleaned wallet may have high volume but artificially low Benford deviation — a combination rarely seen in the genuine-clean distribution. An Isolation Forest trained on the clean-wallet feature distribution will flag this as anomalous.

`detection/adversarial_features.py` exists as a stub. This issue is the full implementation.

## Objectives

- [ ] Implement `AdversarialFeatureDetector` using scikit-learn's `IsolationForest` trained on confirmed-clean wallet feature vectors
- [ ] Implement `FeatureConsistencyChecker` that validates internal feature consistency rules (e.g., high volume + low counterparty count = high concentration ratio)
- [ ] Emit `AdversarialAlert` records for wallets whose feature vectors are anomalous relative to the clean-wallet distribution
- [ ] Add `adversarial_feature_score` (0–1) as a new ML feature in `FEATURE_NAMES`
- [ ] Integrate with `model_inference.py` to boost risk scores when adversarial feature anomaly is detected
- [ ] Expose `GET /adversarial-alerts` endpoint in `api/main.py`
- [ ] Write tests including the three attack types (amount camouflage, timing jitter, graph fragmentation)

## Technical Requirements

### AdversarialAlert dataclass

```python
# detection/adversarial_features.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class AdversarialAlertType(str, Enum):
    ISOLATION_FOREST  = "isolation_forest"   # anomalous in clean-wallet space
    CONSISTENCY_FAIL  = "consistency_fail"   # internal feature contradiction
    HIGH_VOLUME_CLEAN = "high_volume_clean"  # volume/risk ratio implausibly low

@dataclass
class AdversarialAlert:
    wallet: str
    alert_type: AdversarialAlertType
    isolation_score: float       # raw IF anomaly score (-1 to 0; lower = more anomalous)
    inconsistency_flags: list[str]
    adversarial_feature_score: float   # 0–1 composite (higher = more adversarial)
    detected_at: datetime = field(default_factory=datetime.utcnow)
```

### Isolation Forest detector

```python
from sklearn.ensemble import IsolationForest
import numpy as np

class AdversarialFeatureDetector:
    def __init__(
        self,
        contamination: float = 0.05,   # expected fraction of adversarial in clean set
        n_estimators: int = 200,
        random_state: int = 42,
    ): ...

    def fit(self, clean_feature_matrix: np.ndarray) -> None:
        """
        Train on feature vectors of confirmed-clean wallets.
        clean_feature_matrix: shape (N, n_features).
        Use the feature vectors stored in 'feature_vectors' SQLite table
        where the wallet's last risk_score was < 20 and not disputed.
        """
        self._forest = IsolationForest(
            contamination=self.contamination,
            n_estimators=self.n_estimators,
            random_state=self.random_state,
        )
        self._forest.fit(clean_feature_matrix)
        self._fitted = True

    def score(self, feature_vector: np.ndarray) -> float:
        """
        Returns adversarial anomaly score in [0, 1].
        Converts IF raw score (negative; -1 to 0) to [0, 1]:
          adversarial_score = 1 + isolation_forest.score_samples([fv])[0]
        Higher = more anomalous = more likely adversarial.
        """
        if not self._fitted:
            return 0.0  # conservative: no penalty when not fitted
        raw = self._forest.score_samples(feature_vector.reshape(1, -1))[0]
        return float(max(0.0, min(1.0, 1.0 + raw)))
```

### Feature consistency checker

```python
# Internal consistency rules — each rule is a lambda that returns a flag string or None
CONSISTENCY_RULES = [
    # Rule 1: high volume must produce non-trivial counterparty count
    lambda fv: "high_volume_low_counterparty" if (
        fv["volume_to_unique_counterparty_ratio"] > 100
        and fv["counterparty_concentration_ratio"] < 0.05
    ) else None,

    # Rule 2: wash ring member must have non-trivial round-trip frequency
    lambda fv: "ring_member_no_round_trips" if (
        fv["wash_ring_membership"] > 0.5
        and fv["round_trip_trade_frequency"] < 0.01
    ) else None,

    # Rule 3: high Benford chi-square should not co-occur with zero MAD
    lambda fv: "chi_sq_mad_contradiction" if (
        fv["chi_sq_24h"] > 50
        and fv["mad_24h"] < 0.001
    ) else None,

    # Rule 4: account age vs graph centrality (new accounts shouldn't have high centrality)
    lambda fv: "new_account_high_centrality" if (
        fv.get("account_age_days", 999) < 7
        and fv.get("network_centrality", 0.0) > 0.3
    ) else None,
]

class FeatureConsistencyChecker:
    def check(self, feature_dict: dict[str, float]) -> list[str]:
        """Return list of triggered rule names (empty = no contradictions)."""
        return [
            flag for rule in CONSISTENCY_RULES
            if (flag := rule(feature_dict)) is not None
        ]
```

### Composite scoring and score boosting

```python
def compute_adversarial_feature_score(
    isolation_score: float,
    n_consistency_flags: int,
    base_risk_score: int,
) -> float:
    """
    0.5 * isolation_score
  + 0.3 * min(n_consistency_flags / 3, 1.0)
  + 0.2 * (1.0 if base_risk_score < 30 and isolation_score > 0.7 else 0.0)
    The last term catches "suspiciously clean" wallets (low score + high anomaly).
    """
    ...

# In model_inference.py — score boosting
def apply_adversarial_boost(
    base_score: int,
    adversarial_score: float,
    boost_threshold: float = 0.6,
    max_boost: int = 20,
) -> int:
    """
    If adversarial_score >= boost_threshold, add up to max_boost points to base_score.
    Boost = int(max_boost * (adversarial_score - boost_threshold) / (1 - boost_threshold))
    Final score clamped to [0, 100].
    """
    ...
```

### API endpoint

```python
@router.get("/adversarial-alerts")
async def list_adversarial_alerts(
    min_score: float = Query(0.6, ge=0.0, le=1.0),
    alert_type: Optional[AdversarialAlertType] = Query(None),
    limit: int = Query(100, le=500),
) -> list[AdversarialAlertResponse]:
    ...
```

### Configuration

```
ADVERSARIAL_IF_CONTAMINATION=0.05
ADVERSARIAL_IF_N_ESTIMATORS=200
ADVERSARIAL_BOOST_THRESHOLD=0.6
ADVERSARIAL_MAX_BOOST=20
ADVERSARIAL_MIN_CLEAN_SAMPLES=200   # minimum clean samples to fit IF
```

## Security Considerations

- **Training data poisoning**: if the "confirmed-clean" training set is contaminated with adversarial wallets, the IF will learn to treat adversarial patterns as normal. Mitigate by requiring `risk_score < 20 AND dispute_count == 0 AND age_days > 30` for inclusion in the training set
- **Consistency rule hardcoding**: CONSISTENCY_RULES are hardcoded lambdas, not configurable at runtime. This prevents adversaries from probing the rules by submitting arbitrary config changes
- **Score boost upper bound**: the `max_boost` parameter must be bounded to `[0, 30]` at configuration time; reject values outside this range at startup. This prevents a misconfigured boost from producing scores of 130+
- **IF not fitted fallback**: if `ADVERSARIAL_MIN_CLEAN_SAMPLES` is not met (pipeline just started), return `adversarial_feature_score=0.0` and no score boost. Never fail open by returning high adversarial scores when the detector isn't ready
- **Isolation Forest seed**: `random_state=42` is used for reproducibility. Document that changing this seed changes the anomaly boundary and requires re-validation

## Testing Requirements

- [ ] `tests/test_adversarial_features.py`
- [ ] Test: `AdversarialFeatureDetector.fit` on 500 clean vectors then `score` on identical clean vector → `adversarial_feature_score < 0.3`
- [ ] Test: amount camouflage attack vector (high volume, artificially low chi_sq, low MAD) → `adversarial_feature_score > 0.6`
- [ ] Test: timing jitter attack (round_trip=high, iat_variance artificially high, temporal_anomaly_score=0.1) → `isolation_score > 0.5`
- [ ] Test: graph fragmentation (network_centrality=0.0 for a wallet with 500 trades) → `consistency_fail` flag triggered
- [ ] Test: each of the 4 consistency rules triggers correctly on crafted feature dicts
- [ ] Test: `apply_adversarial_boost` clamps to 100
- [ ] Test: IF not fitted → `adversarial_feature_score = 0.0` (fail-safe)
- [ ] Integration test: `GET /adversarial-alerts?min_score=0.6` returns correct schema

## Documentation Requirements

- [ ] Docstrings on `AdversarialFeatureDetector`, `FeatureConsistencyChecker`, `AdversarialAlert`
- [ ] Each `CONSISTENCY_RULES` lambda has a comment explaining the domain rationale
- [ ] Add `docs/adversarial_defense.md` covering the three attack types, the IF defense, consistency rules, score boost policy, and known limitations (IF requires a clean training set — bootstrapping procedure)
- [ ] Update `docs/adversarial_robustness.md` to reference the new feature-space defense
- [ ] Update `.env.example` with five new configuration variables

## Definition of Done

- [ ] `AdversarialFeatureDetector`, `FeatureConsistencyChecker`, and composite scoring implemented
- [ ] `adversarial_feature_score` in `FEATURE_NAMES`
- [ ] Score boosting integrated in `model_inference.py`
- [ ] `GET /adversarial-alerts` endpoint live
- [ ] All three attack-type tests pass with expected scores
- [ ] Fail-safe (IF not fitted → 0.0) verified by test
- [ ] `docs/adversarial_defense.md` authored

## For Contributors

**Ideal contributor profile**: You have experience in adversarial machine learning — either attacking ML models (evasion attacks, feature poisoning) or defending against them. You understand Isolation Forest and its limitations as an anomaly detector. Familiarity with the LedgerLens feature set and wash-trading attack vectors is important for designing effective consistency rules. Experience with the `red_team/` module in this codebase is a strong advantage.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "adversarial ML / model evasion", "anomaly detection", "fraud evasion research"
2. **Relevant experience** — adversarial attack/defense systems you have built; any red-teaming work on fraud detection models; publications on evasion attacks
3. **Approach / initial thoughts** — your view on Isolation Forest vs other anomaly detectors (LOF, OCSVM) for this use case; additional consistency rules you would add; concerns about the score-boosting mechanism
4. **Estimated time** — breakdown by component (IF detector, consistency checker, scoring, integration, tests, docs)
