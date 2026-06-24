---
title: "Add Feature Importance Stability Tracking Across Model Versions"
labels: ["difficulty: advanced", "area: mlops", "type: feature"]
assignees: []
---

## Summary
When LedgerLens retrains its models after concept drift is detected, the relative importance of features may shift significantly — indicating that wash-trading bots have changed their tactics. Currently, `detection/model_registry.py` stores model artifacts but does not track SHAP feature importances per version, making it impossible to audit whether a new model version relies on the same signals as its predecessor. This issue adds per-version SHAP importance tracking, rank-order change detection, and an alert when the top-10 feature ranking changes significantly between versions.

## Background & Context
`detection/model_registry.py` manages versioned model artifacts (`.joblib` files, `latest.txt` pointers, `training_metadata.json`). `detection/shap_explainer.py` computes SHAP values at inference time but does not store aggregate importance summaries per model version.

Feature importance stability is a model governance concern: if a new model version suddenly promotes `timing_tightness_score` from rank 8 to rank 1, this warrants human review before the model is promoted to production. Possible causes include:
- A genuine shift in wash-trading tactics (bots now cluster trades more tightly in time)
- A data pipeline bug introducing spurious correlation
- Model overfitting to a noisy feature that happened to correlate with labels in the retraining set

The stability check should compare the top-10 SHAP importances between consecutive model versions using Spearman rank correlation. A drop below ρ = 0.7 triggers an alert requiring manual review before auto-promotion.

`models/training_metadata.json` is the natural location to store per-version SHAP summaries, augmented with a `shap_importances` key per model name.

## Objectives
- [ ] After each model training run, compute mean absolute SHAP values for all features using a background SHAP summary (100-sample subsample of the training set) and store `top_10_shap: List[{"feature": str, "mean_abs_shap": float, "rank": int}]` per model in `training_metadata.json`
- [ ] Implement `compare_importance_stability(old_metadata: dict, new_metadata: dict) -> StabilityReport` in `detection/model_registry.py` that computes Spearman ρ between old and new top-10 feature rankings for each model
- [ ] Add a `SHAP_STABILITY_THRESHOLD: float = 0.70` configuration parameter and block auto-promotion of new models when ρ < threshold for any model, requiring a `--force-promote` CLI flag to override
- [ ] Add `GET /admin/feature-importance/{version}` API endpoint returning stored SHAP importance data for a given model version

## Technical Requirements

**SHAP summary computation at training time:**
```python
def compute_shap_summary(model, X_train: np.ndarray, feature_names: List[str], n_background: int = 100) -> List[Dict]:
    """Compute mean absolute SHAP values using a background subsample."""
    background = shap.sample(X_train, n_background, random_state=42)
    if hasattr(model, "estimators_"):  # RF
        explainer = shap.TreeExplainer(model, background)
    else:
        explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(background)
    if isinstance(shap_values, list):  # multi-class RF
        shap_values = shap_values[1]  # positive class
    mean_abs = np.abs(shap_values).mean(axis=0)
    ranked = sorted(
        [{"feature": f, "mean_abs_shap": float(v), "rank": 0} for f, v in zip(feature_names, mean_abs)],
        key=lambda x: -x["mean_abs_shap"]
    )
    for i, item in enumerate(ranked):
        item["rank"] = i + 1
    return ranked[:10]  # top-10 only
```

**`training_metadata.json` schema addition:**
```json
{
  "version": "abc12345",
  "shap_importances": {
    "random_forest": [
      {"rank": 1, "feature": "chi2_24h", "mean_abs_shap": 0.312},
      {"rank": 2, "feature": "wash_ring_membership", "mean_abs_shap": 0.287},
      ...
    ],
    "xgboost": [...],
    "lightgbm": [...]
  }
}
```

**`StabilityReport` dataclass:**
```python
@dataclass
class StabilityReport:
    version_old: str
    version_new: str
    spearman_rho: Dict[str, float]   # {"random_forest": 0.91, "xgboost": 0.83, "lightgbm": 0.76}
    stable: bool                      # True when all rho >= SHAP_STABILITY_THRESHOLD
    changed_features: Dict[str, List[str]]  # features that entered/left top-10 per model
    computed_at: datetime
```

**Spearman ρ computation:**
```python
from scipy.stats import spearmanr

def compute_spearman_rho(old_top10: List[Dict], new_top10: List[Dict]) -> float:
    """Compute Spearman rank correlation between old and new feature rankings."""
    # Build union of feature names
    all_features = list({item["feature"] for item in old_top10 + new_top10})
    old_ranks = {item["feature"]: item["rank"] for item in old_top10}
    new_ranks = {item["feature"]: item["rank"] for item in new_top10}
    # Assign rank 11 (outside top-10) to features absent from a top-10 list
    old_vec = [old_ranks.get(f, 11) for f in all_features]
    new_vec = [new_ranks.get(f, 11) for f in all_features]
    rho, _ = spearmanr(old_vec, new_vec)
    return float(rho)
```

**Promotion gate in `cli.py retrain-check`:**
```python
stability = compare_importance_stability(old_metadata, new_metadata)
if not stability.stable:
    logger.warning(
        "Feature importance stability check FAILED: min Spearman ρ = %.3f "
        "(threshold: %.3f). Models NOT auto-promoted. "
        "Rerun with --force-promote to override.",
        min(stability.spearman_rho.values()), SHAP_STABILITY_THRESHOLD
    )
    if not force_promote:
        return  # skip promotion
```

**`GET /admin/feature-importance/{version}` endpoint:**
```
GET /admin/feature-importance/abc12345
→ 200: {"version": "abc12345", "shap_importances": {...}, "generated_at": "..."}
→ 404: version not found
→ 503: admin key not configured
```
Accepts `?model_name=xgboost` filter; returns all models if omitted.

**`GET /admin/feature-importance/diff` endpoint:**
```
GET /admin/feature-importance/diff?old=abc12345&new=def67890
→ 200: StabilityReport JSON
```

**Performance:**
- SHAP summary computation (100 background samples): < 30s for RF (slowest model), < 5s for XGBoost/LightGBM
- `compare_importance_stability()`: < 10 ms (pure Python Spearman computation)

## Security Considerations
- SHAP importance data reveals which features drive model decisions; this is internal model governance data and must be gated behind the admin API key
- The `--force-promote` CLI flag must be logged at `WARN` level with the calling user's identity (or "CLI" if no user context) to create an audit trail
- SHAP summary computations use a background sample of training data; ensure this subsample is not persisted beyond the function call to avoid training data exposure

## Testing Requirements
- Unit tests covering:
  - `compute_shap_summary()` returns list of exactly 10 dicts with `rank` values 1–10
  - `compute_spearman_rho()`: identical rankings → ρ = 1.0; completely reversed → ρ = -1.0
  - `compare_importance_stability()`: returns `stable=True` when all models have ρ ≥ 0.70
  - `compare_importance_stability()`: returns `stable=False` when any model has ρ < 0.70
- Integration tests covering:
  - Full training run writes `shap_importances` to `training_metadata.json`
  - `GET /admin/feature-importance/{version}` returns correct data after training
  - Promotion blocked when stability check fails (mock `compare_importance_stability` to return `stable=False`)
- Edge cases:
  - First training run (no previous version): stability check skipped, `stable=True` by default
  - Old and new top-10 share zero features: ρ computed on union with rank-11 placeholders
  - Single model (XGB only): `StabilityReport.spearman_rho` has one key; `stable` checks only that one model

## Documentation Requirements
- Update `detection/model_registry.py` module docstring with importance tracking and stability check workflow
- Add `SHAP_STABILITY_THRESHOLD` to `config/settings.py` with an explanatory comment
- Update `cli.py` help text with `--force-promote` flag and stability check explanation
- Add a `docs/model_governance.md` explaining the stability threshold rationale, what to investigate when stability fails, and the `--force-promote` audit process

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: SHAP, model versioning, MLOps governance, `scipy.stats.spearmanr`
- Your approach or initial thoughts on handling missing features in rank comparison
- Estimated time to complete

**Ideal contributor profile:** MLOps engineer with model governance experience; familiarity with SHAP `TreeExplainer` and `shap.summary_plot` internals, plus understanding of Spearman rank correlation for feature stability analysis.
