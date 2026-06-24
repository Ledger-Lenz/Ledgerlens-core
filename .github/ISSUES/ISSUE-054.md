---
title: "Implement Counterfactual Explanation Engine for Score Reduction Paths"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/counterfactual_engine.py` to generate minimum-cost counterfactual feature vectors: the smallest perturbation to a wallet's features that would reduce its risk score below 50. Use DiCE (Diverse Counterfactual Explanations) and expose results via `GET /scores/{wallet}/counterfactual`. This gives flagged wallets an actionable explanation: "to reduce your risk score below 50, you would need to reduce your `wash_ring_size` from 8 to 0 and your `round_trip_trade_frequency` from 0.85 to below 0.15."

## Background & Context

SHAP explains *why* a score is high (feature attributions) but not *how to change it* (actionable recourse). For legitimate traders who receive a false positive, or for protocol operators who want to explain scoring to their users, SHAP alone is insufficient. Counterfactual explanations answer: "what is the minimum change to this wallet's behaviour that would result in a score below the alert threshold?"

DiCE (Mothilal et al., 2020) generates diverse counterfactuals by optimising a proximity-diversity objective. For LedgerLens, we adapt DiCE to:
1. Constrain counterfactuals to be actionable (you cannot change your account age, wash_ring_membership in the past, or network centrality retroactively — these are immutable or semi-immutable features)
2. Return multiple (≥ 3) diverse counterfactuals, each achieving `score < 50`
3. Express each counterfactual as a set of plain-language "suggested actions"

`detection/counterfactual_engine.py` exists as a stub. This issue is the full implementation.

## Objectives

- [ ] Implement `CounterfactualEngine` using `dice_ml.Dice` with the production ensemble model
- [ ] Implement `ActionabilityConstraints` defining which features are mutable vs immutable
- [ ] Implement `CounterfactualResult` dataclass encoding the set of counterfactual feature changes and their plain-language descriptions
- [ ] Implement `CounterfactualEngine.generate(wallet, n_counterfactuals=3)` returning diverse counterfactuals
- [ ] Implement `CounterfactualEngine.to_plain_language(cf)` generating human-readable action strings
- [ ] Expose `GET /scores/{wallet}/counterfactual` endpoint with rate limiting
- [ ] Cache counterfactuals in SQLite for 24 hours (re-generate if model version changed)
- [ ] Write tests verifying that returned counterfactuals actually score below 50 when re-scored

## Technical Requirements

### ActionabilityConstraints

```python
# detection/counterfactual_engine.py

from dataclasses import dataclass
from typing import Optional

# Features that cannot be changed retroactively
IMMUTABLE_FEATURES = {
    "account_age_days",
    "wash_ring_membership",    # historical ring membership can't be undone
    "network_centrality",      # depends on historical graph structure
    "sybil_cluster_size",      # funding chain is immutable
    "sybil_in_cluster",
}

# Features with bounded ranges for counterfactual search
FEATURE_BOUNDS = {
    "round_trip_trade_frequency":    (0.0, 1.0),
    "counterparty_concentration_ratio": (0.0, 1.0),
    "self_matching_rate":            (0.0, 1.0),
    "chi_sq_24h":                    (0.0, 500.0),
    "mad_24h":                       (0.0, 0.5),
    "volume_to_unique_counterparty_ratio": (0.0, 10000.0),
    "wash_ring_size":                (0.0, 100.0),
    "cycle_volume_ratio":            (0.0, 1.0),
    # ... all mutable features from FEATURE_NAMES
}
```

### CounterfactualResult

```python
@dataclass
class CounterfactualChange:
    feature: str
    original_value: float
    counterfactual_value: float
    delta: float
    plain_language: str   # e.g., "Reduce round-trip trade frequency from 85% to 12%"

@dataclass
class CounterfactualResult:
    wallet: str
    original_score: int
    counterfactual_score: float   # predicted score after changes
    changes: list[CounterfactualChange]
    proximity_cost: float          # L1 distance between original and CF feature vectors
    diversity_rank: int            # 1 = closest, 2 = second-closest, etc.
    generated_at: str              # ISO timestamp

@dataclass
class CounterfactualResponse:
    wallet: str
    original_score: int
    counterfactuals: list[CounterfactualResult]
    model_version: str
    disclaimer: str = (
        "These counterfactuals describe feature changes, not guaranteed score changes. "
        "Consult LedgerLens documentation before taking action."
    )
```

### CounterfactualEngine

```python
import dice_ml
import pandas as pd
import numpy as np

class CounterfactualEngine:
    def __init__(
        self,
        model_inference_engine,
        target_score_threshold: int = 50,
        n_counterfactuals: int = 3,
        proximity_weight: float = 0.5,
        diversity_weight: float = 1.0,
    ): ...

    def _build_dice_data(self, feature_df: pd.DataFrame) -> dice_ml.Data:
        """
        Build DiCE Data object with continuous features and their ranges.
        Outcome: 'risk_label' (0 = below threshold, 1 = above threshold).
        """
        return dice_ml.Data(
            dataframe=feature_df,
            continuous_features=list(FEATURE_BOUNDS.keys()),
            outcome_name="risk_label",
        )

    def _build_dice_model(self) -> dice_ml.Model:
        """Wrap the ensemble model in a DiCE-compatible model object."""
        return dice_ml.Model(
            model=self._scorer,
            backend="sklearn",
            model_type="classifier",
        )

    def generate(
        self,
        wallet: str,
        feature_dict: dict[str, float],
    ) -> CounterfactualResponse:
        """
        Generate n_counterfactuals diverse counterfactual feature vectors.
        Each must satisfy: ensemble.predict_proba(cf_features)[1] * 100 < target_score_threshold.
        """
        # Check cache first
        cached = self._cache.get(wallet, self._model_version)
        if cached:
            return cached
        query_instance = pd.DataFrame([feature_dict])
        dice_exp = self._dice.generate_counterfactuals(
            query_instance,
            total_CFs=self.n_counterfactuals,
            desired_class="opposite",
            features_to_vary=list(FEATURE_BOUNDS.keys()),
            permitted_range=FEATURE_BOUNDS,
            proximity_weight=self.proximity_weight,
            diversity_weight=self.diversity_weight,
        )
        results = self._parse_dice_output(wallet, feature_dict, dice_exp)
        self._cache.put(wallet, self._model_version, results)
        return results

    def to_plain_language(self, change: CounterfactualChange) -> str:
        """
        Convert a feature delta to a plain-language action string.
        Uses a lookup table of feature → action template.
        """
        templates = {
            "round_trip_trade_frequency": "Reduce round-trip trade frequency from {orig:.0%} to {cf:.0%}",
            "wash_ring_size":            "Exit wash ring (reduce ring size from {orig:.0f} to {cf:.0f})",
            "chi_sq_24h":               "Diversify trade amounts (Benford chi-square: {orig:.1f} → {cf:.1f})",
            "counterparty_concentration_ratio": "Trade with more counterparties (concentration: {orig:.0%} → {cf:.0%})",
        }
        tpl = templates.get(change.feature, "Change {feature} from {orig:.3f} to {cf:.3f}")
        return tpl.format(feature=change.feature, orig=change.original_value, cf=change.counterfactual_value)
```

### Cache schema

```sql
CREATE TABLE IF NOT EXISTS counterfactual_cache (
    wallet        TEXT NOT NULL,
    model_version TEXT NOT NULL,
    response_json TEXT NOT NULL,
    generated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wallet, model_version)
);
-- Expire after 24h: checked in Python, not a DB trigger
```

### API endpoint

```python
@router.get("/scores/{wallet}/counterfactual")
async def get_counterfactual(
    wallet: str,
    n: int = Query(3, ge=1, le=5),
) -> CounterfactualResponse:
    """
    Generate or return cached counterfactual explanations.
    Rate-limited: 10 req/min per IP.
    Requires wallet to have a current RiskScore with score >= 30.
    Returns 404 if wallet has no score; 400 if score < 30 (no action needed).
    """
    ...
```

### Configuration

```
COUNTERFACTUAL_TARGET_THRESHOLD=50
COUNTERFACTUAL_N_DEFAULT=3
COUNTERFACTUAL_PROXIMITY_WEIGHT=0.5
COUNTERFACTUAL_DIVERSITY_WEIGHT=1.0
COUNTERFACTUAL_CACHE_TTL_HOURS=24
COUNTERFACTUAL_RATE_LIMIT_PER_MIN=10
```

## Security Considerations

- **Model transparency abuse**: counterfactuals reveal the decision boundary of the model. Expose only to authenticated callers or enforce strict rate limiting (10/min per IP). Log all counterfactual requests with wallet and caller IP at INFO level
- **IMMUTABLE_FEATURES enforcement**: validate that no counterfactual includes changes to immutable features. If DiCE returns a counterfactual that changes an immutable feature (can happen with gradient methods), filter it out before returning. Log WARNING if this occurs
- **Score below 30 guard**: wallets with score < 30 should not receive counterfactuals (they are already low-risk). Return HTTP 400 with message "Score is already below alert threshold" — this prevents using the API as an oracle to probe the decision boundary from low-risk starting points
- **Plain-language action strings**: action strings must never include raw wallet addresses, internal feature weights, or model architecture details. Treat them as user-facing content and review for information leakage before adding to the template table
- **Cache invalidation**: cache entries must be invalidated when the model version changes (compare `model_version` in cache vs current `models/random_forest_latest.txt`). Stale counterfactuals from a previous model may be misleading

## Testing Requirements

- [ ] `tests/test_counterfactual_engine.py`
- [ ] Test: `generate()` returns exactly `n` counterfactuals for a wallet with score ≥ 50
- [ ] Test: each counterfactual re-scored with the production ensemble achieves score < 50
- [ ] Test: no counterfactual modifies an IMMUTABLE_FEATURE
- [ ] Test: `to_plain_language()` produces non-empty strings for all FEATURE_BOUNDS features
- [ ] Test: `generate()` returns cached result on second call (no DiCE re-generation)
- [ ] Test: cache miss when model version differs from cached version
- [ ] Test: `GET /scores/{wallet}/counterfactual` returns 404 for unknown wallet; 400 for score < 30
- [ ] Test: rate limit triggers 429 after 10 requests/min

## Documentation Requirements

- [ ] Docstrings on `CounterfactualEngine`, `CounterfactualResult`, `CounterfactualChange`, `ActionabilityConstraints`
- [ ] Comment on each entry in `IMMUTABLE_FEATURES` explaining why it is immutable
- [ ] Add `docs/counterfactual_explanations.md` covering the methodology, DiCE configuration, actionability constraints, how to interpret results, and limitations
- [ ] Update `README.md` interpretability section to mention counterfactual explanations
- [ ] Update `.env.example` with six new configuration variables

## Definition of Done

- [ ] `CounterfactualEngine` fully implemented with DiCE integration
- [ ] `GET /scores/{wallet}/counterfactual` endpoint live with rate limiting
- [ ] All counterfactuals verified to score < 50 on re-scoring (test passes)
- [ ] IMMUTABLE_FEATURES enforcement verified by test
- [ ] Cache implemented and tested
- [ ] `docs/counterfactual_explanations.md` authored
- [ ] All tests pass; no new lint errors

## For Contributors

**Ideal contributor profile**: You have experience with ML interpretability libraries — DiCE, LIME, SHAP, or Alibi — and understand the distinction between attribution-based (SHAP) and recourse-based (counterfactual) explanations. Familiarity with sklearn-compatible model interfaces and Pandas DataFrames is essential. Experience working on explainable AI for high-stakes decisions (credit scoring, fraud detection, compliance) is particularly relevant. Knowledge of the LedgerLens feature engineering pipeline will reduce onboarding time significantly.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "explainable AI / counterfactual reasoning", "ML recourse and fairness", "fraud detection interpretability"
2. **Relevant experience** — DiCE or algorithmic recourse projects; explainability work in regulated industries; any published work on counterfactual explanations
3. **Approach / initial thoughts** — your view on DiCE vs CARLA or Growing Spheres for this use case; concerns about actionability constraints and the trade-off between proximity and diversity
4. **Estimated time** — breakdown by component (engine, constraints, plain-language templates, cache, API, tests, docs)
