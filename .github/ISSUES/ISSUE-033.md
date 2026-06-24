---
title: "Implement Causal Feature Selection to Remove Spurious Correlations"
labels: ["difficulty: advanced", "area: ml", "type: enhancement"]
assignees: []
---

## Summary
The LedgerLens feature set contains 35+ features derived from the same underlying trade data, many of which share statistical correlations that are not causal. For example, `volume_spike_frequency` and `intra_minute_clustering` are both high for wash-trading bots but one may be a *consequence* of the other rather than an independent causal indicator. Training on spuriously correlated features creates models that are brittle to adversarial evasion: a bot that learns to suppress one correlated feature (e.g., by spacing out trades slightly) can inadvertently evade the model even though the underlying wash-trading pattern is unchanged. Implementing the PC (Peter-Clark) algorithm in `detection/causal_engine.py` to learn a feature causal DAG and removing non-causal features before training improves adversarial robustness.

## Background & Context
`detection/causal_engine.py` is a planned file for causal inference analysis. The feature set in `detection/feature_engineering.py` includes several feature groups that may share causal structure:
- **Volume features** (`volume_to_unique_counterparty_ratio`, `volume_spike_frequency`) are both driven by the underlying wash-trading volume; one may be a causal descendant of the other
- **Benford features** across different windows (1h, 4h, 24h) are statistically correlated because the 1h window is a subset of the 4h window; this is a measurement dependency, not an independent signal
- **Graph features** (`wash_ring_membership`, `wash_ring_size`, `cycle_volume_ratio`) are derived from the same graph structure and share causal ancestry

The PC algorithm (Spirtes, Glymour, Scheines, 2000) learns a Partially Directed Acyclic Graph (PDAG) from observational data using conditional independence tests. Features that are d-separated from the target (wash-trading label) by other features are non-causal and can be pruned without loss of predictive information — and with a gain in robustness.

Implementation will use the `causal-learn` (formerly `causaldag`) Python library, which provides a production-ready PC algorithm implementation. Alternatively, a subset of the PC algorithm (skeleton learning + orientation) can be implemented directly for the feature-selection use case.

## Objectives
- [ ] Implement `CausalFeatureSelector` class in `detection/causal_engine.py` with `fit(X, y, feature_names) -> List[str]` returning the causally selected feature subset
- [ ] Implement the PC algorithm skeleton phase using Fisher's Z conditional independence test for continuous features and G² test for binary features
- [ ] Add `causal_feature_selection: bool = False` option to `train_models()` in `model_training.py`; when enabled, run `CausalFeatureSelector.fit()` before training and document the selected features in `training_metadata.json`
- [ ] Evaluate and document: compare AUC-PR and adversarial robustness (using the adversarial test suite from ISSUE-036 when available) between full-feature and causal-subset models

## Technical Requirements

**PC Algorithm — Skeleton Phase:**
The PC algorithm starts with a fully connected undirected graph and removes edges by testing conditional independence at increasing conditioning set sizes:

```
1. Start: complete undirected graph G over features F = {f1, ..., fn, y}
2. For conditioning_set_size l = 0, 1, 2, ...:
   For each adjacent pair (X, Y) in G:
     Find a conditioning set S ⊆ adj(X) \ {Y}, |S| = l
     If X ⊥ Y | S (conditionally independent given S):
       Remove edge (X, Y); store separation set sep(X,Y) = S
       Break (move to next pair)
   If no edge removed in this pass: stop
3. Result: skeleton (undirected graph of dependencies)
```

**Conditional Independence Test — Fisher's Z:**
For continuous features, test H₀: X ⊥ Y | S using:
```python
def fishers_z_test(X_vec, Y_vec, S_mat, alpha=0.01) -> bool:
    """Returns True if X and Y are conditionally independent given S."""
    n = len(X_vec)
    if len(S_mat) == 0:
        r = np.corrcoef(X_vec, Y_vec)[0, 1]
    else:
        # Partial correlation via linear regression residuals
        r = partial_correlation(X_vec, Y_vec, S_mat)
    r = np.clip(r, -1 + 1e-10, 1 - 1e-10)
    z = 0.5 * np.log((1 + r) / (1 - r))  # Fisher's Z transform
    se = 1.0 / np.sqrt(n - len(S_mat) - 3)
    p_value = 2 * (1 - norm.cdf(abs(z) / se))
    return p_value > alpha  # True = independent
```

**Partial correlation via regression:**
```python
def partial_correlation(X_vec, Y_vec, S_mat: np.ndarray) -> float:
    """Compute partial correlation of X and Y conditioned on S via residuals."""
    X_res = X_vec - S_mat @ np.linalg.lstsq(S_mat, X_vec, rcond=None)[0]
    Y_res = Y_vec - S_mat @ np.linalg.lstsq(S_mat, Y_vec, rcond=None)[0]
    return np.corrcoef(X_res, Y_res)[0, 1]
```

**Feature selection from skeleton:**
After skeleton learning, select only features that are adjacent to the target variable `y` in the undirected skeleton graph. Features with no edge to `y` after all conditioning tests are non-causal with respect to the wash-trading label.

**Complexity management:**
- Maximum conditioning set size: 3 (controlled by `max_conditioning_size: int = 3` config parameter)
- For 35 features: worst-case `C(35, 3) = 6545` conditioning set tests per edge — computationally feasible
- Cache `partial_correlation` results using `lru_cache(maxsize=10000)`
- Target: complete feature selection in < 5 minutes for a 10,000-sample training set

**Significance level:**
- Use `alpha = 0.01` (stricter than 0.05) to avoid over-pruning; false positives (incorrectly removing causal features) are more costly than false negatives (retaining non-causal features)
- Make `alpha` configurable via `CAUSAL_INDEPENDENCE_ALPHA: float = 0.01` in `config/settings.py`

**`causal-learn` integration (preferred over manual implementation):**
```python
from causallearn.search.ConstraintBased.PC import pc
from causallearn.utils.cit import fisherz

cg = pc(X_with_label, alpha=0.01, indep_test=fisherz)
# Extract adjacency to label column (last column)
label_col = X_with_label.shape[1] - 1
adjacent_to_label = [i for i in range(label_col) if cg.G.graph[i, label_col] != 0]
selected_features = [feature_names[i] for i in adjacent_to_label]
```

If `causal-learn` is unavailable, fall back to the manual Fisher's Z implementation above.

**Output and integration:**
```python
# training_metadata.json additions:
{
  "causal_feature_selection": true,
  "selected_features": ["chi2_24h", "wash_ring_membership", ...],
  "removed_features": ["chi2_1h", "chi2_4h", ...],  # removed as d-separated from y
  "n_selected": 18,
  "n_removed": 17
}
```

## Security Considerations
- The PC algorithm may incorrectly remove features that are genuinely causal due to statistical noise in small datasets; document the risk and require manual review of `removed_features` before deploying a causal-subset model to production
- `causal-learn` must be pinned to a specific version in `requirements.txt`; the library's API has changed significantly between minor versions
- Conditional independence tests must handle near-singular correlation matrices gracefully; add `np.linalg.matrix_rank` check before any matrix inversion and fall back to pairwise (zero-conditioning) test if the matrix is rank-deficient

## Testing Requirements
- Unit tests covering:
  - `fishers_z_test()` on uncorrelated data (two independent Gaussian vectors): returns `True` (independent)
  - `fishers_z_test()` on highly correlated data (r=0.95): returns `False` (dependent)
  - `partial_correlation()` on data where `X = Z + ε` and `Y = Z + δ` (common cause Z): conditioned on Z, partial correlation ≈ 0
  - `CausalFeatureSelector.fit()` on a known DAG: recovers correct feature subset
- Integration tests covering:
  - `CausalFeatureSelector.fit()` on synthetic LedgerLens feature matrix (100 samples, 35 features): completes in < 60s
  - Selected features are subset of full `FEATURE_NAMES`
  - Model trained on causal subset achieves AUC-PR within 5% of full-feature model (quality gate)
- Edge cases:
  - All features perfectly correlated with label (ideal case): all features selected
  - All features independent of label: no features selected; `train_models()` raises `ValueError`
  - `max_conditioning_size=0` (no conditioning): pure pairwise independence test, faster but less accurate

## Documentation Requirements
- Create `detection/causal_engine.py` with comprehensive module docstring explaining the PC algorithm, Fisher's Z test, and feature selection rationale
- Add `CAUSAL_INDEPENDENCE_ALPHA` and `CAUSAL_MAX_CONDITIONING_SIZE` to `config/settings.py`
- Add `causal-learn` to `requirements.txt` as an optional dependency with a fallback note
- Create `docs/causal_feature_selection.md` with a DAG diagram of expected feature relationships and explanation of d-separation in plain language

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: causal inference, PC algorithm, conditional independence testing, `causal-learn` library
- Your approach or initial thoughts on the feature-label adjacency extraction
- Estimated time to complete

**Ideal contributor profile:** ML researcher with causal inference expertise; direct experience with constraint-based causal discovery (PC, FCI) and Fisher's Z test implementations is essential; knowledge of DeFi wash trading mechanics is a bonus.
