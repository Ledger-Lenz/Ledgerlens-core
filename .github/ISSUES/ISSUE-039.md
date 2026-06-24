---
title: "Implement Counterfactual Explanation Generation for High-Risk Scores"
labels: ["difficulty: advanced", "area: interpretability", "type: feature"]
assignees: []
---

## Summary
SHAP explanations (see `detection/shap_explainer.py` and ISSUE-028) tell users *which features contributed most* to a high risk score, but they do not tell users *what would need to change* to reduce the score below the alert threshold. Counterfactual explanations bridge this gap: they find the smallest feature-space perturbation to a wallet's current feature vector that would result in a risk score below 70 (the `RISK_SCORE_THRESHOLD`), then translate those feature changes into actionable, plain-language advice for wallet owners who want to understand what legitimate behavioural changes would reduce their risk flag. This is essential for dispute resolution and for maintaining the credibility of LedgerLens scores.

## Background & Context
A counterfactual explanation for a data point `x` with label `y=1` (wash-trading) is a point `x'` such that:
1. The model predicts `f(x') < threshold` (the counterfactual would be classified as clean)
2. `‖x' - x‖` is minimised (the counterfactual is as close as possible to the original — "what is the minimal change needed?")
3. `x'` is plausible — it lies within the observed range of clean wallet feature distributions (out-of-distribution counterfactuals are not actionable)

The `dice-ml` library (DiCE: Diverse Counterfactual Explanations) and the `alibi` library both implement algorithmic counterfactual generation for tabular ML models. For LedgerLens, the primary method should be gradient-free optimisation (since RF and XGB don't provide exact gradients) using CFRL (Counterfactual RL) or the simpler genetic algorithm approach in DiCE.

`detection/counterfactual_engine.py` is the planned location for this functionality.

## Objectives
- [ ] Implement `CounterfactualEngine` class in `detection/counterfactual_engine.py` with `generate(wallet: str, asset_pair: str, top_n_actions: int = 5) -> CounterfactualResult` method
- [ ] Implement a feature-space optimisation that finds the nearest `x'` satisfying `f(x') < 0.5` (probability threshold) using genetic algorithm or CFRL, constrained to the observed clean wallet distribution
- [ ] Translate `x' - x` (feature delta) into plain-English action items mapped to concrete on-chain behaviour (e.g., "reduce trade frequency in the 24-hour window from 150 to 45 trades")
- [ ] Add `GET /scores/{wallet}/counterfactual` API endpoint returning the counterfactual result, and persist the result to SQLite for repeat queries

## Technical Requirements

**`CounterfactualResult` dataclass:**
```python
@dataclass
class CounterfactualResult:
    wallet: str
    asset_pair: str
    original_score: int
    counterfactual_score: int             # predicted score for x'
    original_features: Dict[str, float]
    counterfactual_features: Dict[str, float]
    feature_deltas: Dict[str, float]      # x'[i] - x[i] for changed features only
    actions: List[CounterfactualAction]   # plain-English action items
    plausibility_score: float             # 0–1; fraction of clean wallets within epsilon of x'
    computation_time_ms: float
    generated_at: datetime
```

**`CounterfactualAction` dataclass:**
```python
@dataclass
class CounterfactualAction:
    rank: int                  # 1 = most impactful change
    feature_name: str
    current_value: float
    target_value: float
    delta: float               # target - current
    direction: str             # "decrease" or "increase"
    plain_english: str         # actionable instruction
    estimated_score_reduction: int  # estimated score points reduction from this change alone
```

**Plain-English action templates (feature → instruction mapping):**
```python
ACTION_TEMPLATES = {
    "chi2_24h": "Diversify trade amounts in the 24-hour window. "
                "Current Benford chi-square {current:.1f} is {ratio:.0f}× above the normal threshold of 15.5. "
                "Varying trade sizes more naturally (rather than fixed lot sizes) would bring this to {target:.1f}.",
    "counterparty_concentration_ratio": "Trade with a wider range of counterparties. "
                "Currently {pct:.0f}% of trades are with the same counterparty; "
                "reducing this to {target_pct:.0f}% or below would significantly reduce the concentration signal.",
    "wash_ring_membership": "This wallet is currently part of a detected trading ring. "
                "Ceasing circular trade patterns (buy A→B then sell B→A within the same cluster) "
                "would remove this flag.",
    "round_trip_trade_frequency": "Reduce round-trip trades: buying and selling the same asset pair "
                "with the same counterparty within a {window} window currently accounts for "
                "{pct:.0f}% of activity. Reducing this to {target_pct:.0f}% would lower the signal.",
    # ... one template per controllable feature
}
```

**Counterfactual optimisation algorithm:**
Use a genetic algorithm (GA) for model-agnostic, gradient-free optimisation:

```python
def genetic_counterfactual(
    x_original: np.ndarray,
    predict_fn: Callable,       # model.predict_proba(...)[0][1]
    feature_names: List[str],
    X_clean_reference: np.ndarray,  # distribution of clean wallets for plausibility
    controllable_mask: np.ndarray,  # same as adversarial mask in ISSUE-036
    target_threshold: float = 0.5,
    population_size: int = 100,
    max_generations: int = 200,
    mutation_rate: float = 0.1,
    seed: int = 42,
) -> np.ndarray:
    """
    Find x' minimising ||x'-x||_2 such that predict_fn(x') < target_threshold.
    Uses tournament selection, uniform crossover, and Gaussian mutation.
    """
    rng = np.random.default_rng(seed)
    # Initialise population by sampling from clean reference distribution
    population = X_clean_reference[rng.choice(len(X_clean_reference), population_size), :].copy()
    # Replace non-controllable features with original values
    for ind in population:
        ind[~controllable_mask] = x_original[~controllable_mask]

    for gen in range(max_generations):
        # Evaluate fitness: penalise distance + penalise score above threshold
        scores = np.array([predict_fn(ind) for ind in population])
        distances = np.linalg.norm(population - x_original, axis=1)
        fitness = scores + 0.5 * distances / distances.max()  # minimise
        # Elitism: keep best candidate
        best_idx = fitness.argmin()
        if scores[best_idx] < target_threshold:
            return population[best_idx]
        # Tournament selection + crossover + mutation
        ...  # standard GA operators
    return population[fitness.argmin()]  # best found even if threshold not met
```

**DiCE integration (alternative/optional):**
```python
# If dice-ml is available, prefer it over the custom GA:
import dice_ml
data = dice_ml.Data(dataframe=clean_df, continuous_features=continuous_features, outcome_name="label")
model = dice_ml.Model(model=ensemble_model, backend="sklearn")
exp = dice_ml.Dice(data, model, method="genetic")
cf = exp.generate_counterfactuals(x_df, total_CFs=1, desired_class=0)
```

**Plausibility score:**
```python
def compute_plausibility(x_cf: np.ndarray, X_clean: np.ndarray, epsilon: float = 0.5) -> float:
    """Fraction of clean wallets within L2 distance epsilon of the counterfactual."""
    distances = np.linalg.norm(X_clean - x_cf, axis=1)
    return float((distances < epsilon).mean())
```

**Performance:**
- GA with 100 population × 200 generations × 3-model ensemble: ~60,000 `predict_proba` calls → target < 30s for XGBoost
- Cache the counterfactual result for 24 hours per wallet/asset-pair; recompute when score is updated
- The endpoint should return `202 Accepted` immediately if computation is not complete, with a `Location` header pointing to a polling URL

**`GET /scores/{wallet}/counterfactual` endpoint:**
```
GET /scores/{wallet}/counterfactual?asset_pair=XLM/USDC
→ 200: CounterfactualResult JSON
→ 202: {"status": "computing", "poll_url": "/scores/{wallet}/counterfactual/status"}
→ 404: wallet/score not found
→ 422: score below threshold (no counterfactual needed — score is already clean)
```

**Counterfactual persistence table:**
```sql
CREATE TABLE IF NOT EXISTS counterfactual_cache (
    wallet TEXT,
    asset_pair TEXT,
    original_score INTEGER,
    counterfactual_json TEXT,  -- JSON blob of CounterfactualResult
    generated_at TIMESTAMP,
    expires_at TIMESTAMP,
    PRIMARY KEY (wallet, asset_pair)
);
```

## Security Considerations
- The counterfactual explains how to *reduce* a risk score by changing features; this information could in principle help a wash-trading bot evade detection. Mitigations:
  - Only generate counterfactuals for wallets that have gone through the dispute process (or require the admin API key)
  - Do not expose which features are *not* perturbed (the non-controllable mask) — this would reveal that account age is not counterfactual-accessible
  - Rate-limit `GET /scores/{wallet}/counterfactual` to 5 requests per hour per wallet
- The `X_clean_reference` distribution used for plausibility must not be exposed via the API; it contains aggregate statistics about the clean-wallet population
- Plain-English instructions must not include specific threshold values that reveal model decision boundaries beyond what is already public

## Testing Requirements
- Unit tests covering:
  - `genetic_counterfactual()` with a linear model: converges to a counterfactual within 50 generations
  - `compute_plausibility()`: x_cf identical to a clean sample → plausibility = fraction of clean samples within epsilon
  - `CounterfactualAction.plain_english`: template rendered correctly for `chi2_24h`, `wash_ring_membership`
  - Non-controllable features unchanged in counterfactual (`account_age` in `x'` equals `account_age` in `x`)
- Integration tests covering:
  - `CounterfactualEngine.generate()` on a high-risk synthetic wallet: returns `CounterfactualResult` with `counterfactual_score < 70`
  - `GET /scores/{wallet}/counterfactual` returns 200 with valid JSON when cached result exists
  - Cache hit: second call to `generate()` for same wallet/asset-pair within 24h returns cached result
- Edge cases:
  - Score already below threshold (< 70): `generate()` raises `ValueError("Score {score} is below threshold; no counterfactual needed")`
  - GA fails to find counterfactual within max_generations: returns best attempt with `found_below_threshold=False`
  - All features at boundary values: GA cannot perturb further; returns original features with `plausibility_score=0`

## Documentation Requirements
- Create `detection/counterfactual_engine.py` with full docstrings and `ACTION_TEMPLATES` dict documentation
- Add `COUNTERFACTUAL_MAX_GENERATIONS` and `COUNTERFACTUAL_CACHE_HOURS` to `config/settings.py`
- Add optional `dice-ml` to `requirements.txt` with a comment
- Create `docs/counterfactual_explanations.md` explaining what counterfactuals are, how to interpret actions, and the dispute use case

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: counterfactual ML explanations, genetic algorithms, `dice-ml`, `alibi`, explainable AI
- Your approach or initial thoughts on balancing counterfactual actionability vs. evasion risk
- Estimated time to complete

**Ideal contributor profile:** ML interpretability engineer with experience generating counterfactual explanations for tabular classifiers; understanding of the tension between user transparency and adversarial evasion risk is essential for this security-sensitive feature.
