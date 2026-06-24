---
title: "Implement Causal Inference Engine to Distinguish Causal vs Correlational Risk Drivers"
labels: ["difficulty: advanced", "area: detection", "type: research"]
assignees: []
---

## Summary

Extend `detection/causal_engine.py` using DoWhy to build a causal DAG over the ML features and risk score output. Implement do-calculus interventions so analysts can ask "if we remove the Benford signal, what is the causal contribution of graph topology to the score?" This transforms LedgerLens from a correlation-based detector into one that provides causal explanations — a major step toward regulatory defensibility and adversarial robustness.

## Background & Context

SHAP values explain *which features contributed most to a score* but they conflate causal and correlational effects. If Benford features and graph features are correlated (as they are: wash traders are non-Benford AND in rings), SHAP will attribute shared credit to both. A regulator asking "would this wallet still be flagged if it fixed its Benford distribution?" cannot be answered by SHAP — only by causal intervention.

DoWhy (Microsoft Research) provides a Python API for causal reasoning: define a causal DAG, fit structural equations from data, then use `do(X=x)` interventions to compute counterfactual expected outcomes. For LedgerLens, the causal DAG encodes domain knowledge:

- Wash-ring membership → round_trip_trade_frequency → risk_score
- wash_ring_membership → volume_to_unique_counterparty_ratio → risk_score  
- Benford signals are caused by the trade-amount distribution, which is partially caused by wash ring activity
- Graph centrality → risk_score (direct path)
- Account age → wash_ring_membership (older accounts are harder to Sybil)

This issue builds the DAG, fits structural equations (linear + nonlinear), exposes a `CausalQuery` API, and integrates results into the score explanation API.

## Objectives

- [ ] Define the LedgerLens causal DAG as a NetworkX DiGraph with documented edge justifications
- [ ] Implement `CausalEngine` class using `dowhy.CausalModel` and `econml` for nonlinear structural equations
- [ ] Implement `CausalEngine.estimate_effect(treatment, outcome, intervention_value)` for `do()` interventions
- [ ] Implement `CausalEngine.feature_ate(feature_name)` returning the average treatment effect of each feature on risk_score
- [ ] Implement `CausalEngine.counterfactual_score(wallet, feature_overrides)` answering "what would the score be if feature X were Y?"
- [ ] Expose `GET /scores/{wallet}/causal-explanation` in `api/main.py`
- [ ] Write tests validating that interventions on non-causal features have smaller effects than interventions on causal ones (using synthetic data with known ground truth)
- [ ] Produce a `docs/causal_inference.md` explaining the DAG and methodology

## Technical Requirements

### Causal DAG definition

```python
# detection/causal_engine.py

import networkx as nx
from dowhy import CausalModel

# Nodes: feature names + "risk_score" + latent "wash_activity"
CAUSAL_DAG_EDGES = [
    # Wash activity → observable features
    ("wash_activity",              "wash_ring_membership"),
    ("wash_activity",              "round_trip_trade_frequency"),
    ("wash_activity",              "chi_sq_24h"),                  # Benford signal
    ("wash_activity",              "cycle_volume_ratio"),
    # Feature → feature (structural paths)
    ("wash_ring_membership",       "volume_to_unique_counterparty_ratio"),
    ("wash_ring_membership",       "round_trip_trade_frequency"),
    ("account_age_days",           "wash_ring_membership"),         # older = harder to Sybil
    ("network_centrality",         "wash_ring_membership"),
    # Features → risk_score (direct causal paths)
    ("wash_ring_membership",       "risk_score"),
    ("round_trip_trade_frequency", "risk_score"),
    ("chi_sq_24h",                 "risk_score"),
    ("cycle_volume_ratio",         "risk_score"),
    ("volume_to_unique_counterparty_ratio", "risk_score"),
    ("network_centrality",         "risk_score"),
    ("account_age_days",           "risk_score"),
    ("gnn_wash_ring_prob",         "risk_score"),
]

def build_causal_dag() -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_edges_from(CAUSAL_DAG_EDGES)
    return G
```

### CausalEngine class

```python
class CausalEngine:
    def __init__(
        self,
        dag: nx.DiGraph,
        estimation_method: str = "backdoor.linear_regression",
    ): ...

    def fit(self, df: "pd.DataFrame") -> None:
        """
        Fit structural equations using DoWhy CausalModel.
        df must contain columns for all non-latent nodes + 'risk_score'.
        Latent node 'wash_activity' is treated as unobserved.
        """
        self._model = CausalModel(
            data=df,
            treatment=None,           # set per query
            outcome="risk_score",
            graph=self._dag_to_gml(),
        )

    def estimate_ate(
        self,
        treatment_feature: str,
        control_value: float,
        treatment_value: float,
    ) -> "dowhy.CausalEstimate":
        """
        Estimate E[risk_score | do(feature=treatment_value)]
            - E[risk_score | do(feature=control_value)]
        using the fitted model and backdoor linear regression.
        """
        identified_estimand = self._model.identify_effect(
            treatment=treatment_feature,
            outcome="risk_score",
        )
        return self._model.estimate_effect(
            identified_estimand,
            method_name=self.estimation_method,
            control_value=control_value,
            treatment_value=treatment_value,
        )

    def feature_ate_table(self, df: "pd.DataFrame") -> dict[str, float]:
        """
        For each feature in the DAG, compute ATE when feature is set to 0 vs 1
        (normalised scale). Returns {feature_name: ate}.
        """
        ...

    def counterfactual_score(
        self,
        wallet_features: dict[str, float],
        overrides: dict[str, float],
    ) -> float:
        """
        Compute predicted risk_score if the specified features were set to override values.
        Uses linear structural equations for speed.
        Returns score in [0, 100].
        """
        ...

    def refutation_tests(self) -> dict[str, float]:
        """
        Run DoWhy refutation tests on the fitted model:
          - random_common_cause
          - placebo_treatment_refuter
          - data_subset_refuter
        Returns {test_name: p_value}. p_values < 0.05 indicate model issues.
        """
        ...
```

### API endpoint

```python
@router.get("/scores/{wallet}/causal-explanation")
async def causal_explanation(
    wallet: str,
    feature_override: Optional[str] = Query(None),  # "feature=value" format
) -> CausalExplanationResponse:
    """
    Returns:
      - feature_ate_table: ATE of each feature on risk_score
      - top_causal_features: top-3 by absolute ATE
      - counterfactual_score: score if feature_override is applied (optional)
    """
    ...

@dataclass
class CausalExplanationResponse:
    wallet: str
    current_score: int
    feature_ate_table: dict[str, float]
    top_causal_features: list[tuple[str, float]]
    counterfactual_score: Optional[float]
    coverage_note: str   # "Based on N scored wallets; causal estimates may be noisy"
```

### Configuration

```
CAUSAL_ESTIMATION_METHOD=backdoor.linear_regression
CAUSAL_REFUTATION_RUNS=100
CAUSAL_MIN_SAMPLE_SIZE=500   # minimum rows to fit the model
```

### SQLite persistence

Persist the fitted ATE table per model version so API calls don't refit the model on every request:

```sql
CREATE TABLE IF NOT EXISTS causal_ate_cache (
    model_version TEXT NOT NULL,
    feature_name  TEXT NOT NULL,
    ate           REAL NOT NULL,
    computed_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (model_version, feature_name)
);
```

## Security Considerations

- **DoWhy refutation tests as mandatory gate**: if `placebo_treatment_refuter` p-value < 0.05 for more than 3 features, the causal model is likely misspecified. Log ERROR and refuse to serve `feature_ate_table` values, returning a `503` with a descriptive error
- **User-controlled `feature_override` parameter**: the format `"feature=value"` must be strictly validated — `feature` must be in `FEATURE_NAMES`, `value` must parse as a finite float in `[-1000, 1000]`. Reject and return 422 on any violation
- **DAG modification attacks**: the `CAUSAL_DAG_EDGES` list is hardcoded and not configurable at runtime. Do not expose a runtime DAG-modification API — the causal structure is a domain-knowledge artefact, not a user-tunable parameter
- **Sensitive score exposure**: `counterfactual_score` reveals internal model sensitivity. Do not cache per-wallet counterfactuals publicly. Rate-limit `GET /scores/{wallet}/causal-explanation` to 10 requests/minute per IP
- **DoWhy dependency pinning**: DoWhy's API has changed significantly between 0.9 and 0.11. Pin the version in `requirements.txt` and test against that exact version

## Testing Requirements

- [ ] `tests/test_causal_engine.py` — unit and validation tests
- [ ] Test: `build_causal_dag()` is a valid DAG (no cycles) — use `nx.is_directed_acyclic_graph`
- [ ] Test: `CausalEngine.fit()` on 1000-row synthetic DataFrame completes without error
- [ ] Test: ATE of `wash_ring_membership` > ATE of `account_age_days` on synthetic data where wash_ring_membership is the true causal driver
- [ ] Test: `counterfactual_score(wallet_features, {"wash_ring_membership": 0.0})` returns lower score than baseline for a flagged wallet
- [ ] Test: `feature_override` parameter rejects invalid feature name with 422
- [ ] Test: `feature_override` parameter rejects out-of-range value with 422
- [ ] Test: `refutation_tests` returns dict with three keys and float p-values
- [ ] Integration test: `GET /scores/{wallet}/causal-explanation` returns expected response schema

## Documentation Requirements

- [ ] Docstrings on `CausalEngine`, all public methods, and the `CAUSAL_DAG_EDGES` constant (each edge must have a one-line justification comment)
- [ ] `docs/causal_inference.md` — full methodology: why causal vs SHAP, DAG design choices, do-calculus intuition for non-experts, ATE interpretation guide, known limitations (latent variable `wash_activity` is unobserved)
- [ ] Update `README.md` interpretability section to mention causal explanations alongside SHAP
- [ ] Document `causal_ate_cache` table in `docs/database_schema.md`
- [ ] Update `.env.example` with three new configuration variables

## Definition of Done

- [ ] `CausalEngine` fully implemented with `fit`, `estimate_ate`, `feature_ate_table`, `counterfactual_score`, `refutation_tests`
- [ ] `GET /scores/{wallet}/causal-explanation` endpoint live with rate limiting
- [ ] DAG is a valid DAG (test passes)
- [ ] Causal directionality test passes (true causal features have larger ATE)
- [ ] `docs/causal_inference.md` authored with DAG diagram
- [ ] All tests pass; no new lint errors

## For Contributors

**Ideal contributor profile**: You have practical experience with causal inference in Python — DoWhy, CausalML, or econml. You understand d-separation, backdoor criterion, and structural causal models. Familiarity with SHAP and its limitations (no causal guarantees) will help you articulate the value of this approach. Experience applying causal inference to financial fraud or algorithmic decision-making is highly relevant. A background in econometrics or statistics is a strong plus.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "causal inference / DoWhy", "structural causal models", "ML interpretability for compliance"
2. **Relevant experience** — DoWhy or econml projects; publications on causal ML; experience with regulatory interpretability requirements
3. **Approach / initial thoughts** — your thoughts on the proposed DAG (any edges you would add or remove); concerns about the latent `wash_activity` variable; alternative estimation methods to `backdoor.linear_regression`
4. **Estimated time** — breakdown by component (DAG, engine, refutation, API, tests, docs)
