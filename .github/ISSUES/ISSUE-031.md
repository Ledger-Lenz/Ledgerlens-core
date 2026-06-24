---
title: "Build an Ensemble Stacking Layer with a Meta-Learner on Top of RF/XGB/LGBM"
labels: ["difficulty: advanced", "area: ml", "type: enhancement"]
assignees: []
---

## Summary
The current LedgerLens ensemble in `detection/model_training.py` averages the probability outputs of Random Forest, XGBoost, and LightGBM with equal weights. Simple averaging ignores the complementary strengths of each model and may produce a worse combined prediction than an optimally weighted or learned combination. A stacking ensemble with a logistic regression meta-learner trained on out-of-fold (OOF) base model predictions learns the optimal combination and can outperform simple averaging by 2–5% AUC-PR in practice. This issue implements full stacking with OOF generation and integrates the meta-learner into the inference pipeline.

## Background & Context
In `detection/model_inference.py`, `ModelInference.score()` calls all three base models and computes `ensemble_score = np.mean([rf_proba, xgb_proba, lgbm_proba])`. This equal-weight average is a reasonable baseline but:
- Does not account for relative model performance on the specific training distribution
- Cannot learn that XGBoost tends to be more reliable for high-score boundary cases while RF is better calibrated for borderline cases
- Cannot leverage the fact that model disagreement itself is informative (high variance across models signals uncertainty)

Stacking (Wolpert, 1992) trains a meta-learner on the base models' OOF predictions to learn the optimal combination. The OOF generation process:
1. Split the training set into K folds (K=5)
2. For each fold: train base models on K-1 folds, predict on the held-out fold
3. Collect all K held-out predictions to form a full-length OOF prediction array
4. Train the meta-learner on `[rf_oof_proba, xgb_oof_proba, lgbm_oof_proba]` → `true_label`

The meta-learner must use temporal folds (see ISSUE-027) to avoid leakage.

## Objectives
- [ ] Implement `generate_oof_predictions(X_train, y_train, timestamps, models, n_splits=5) -> np.ndarray` in `model_training.py` that generates a `(n_train, 3)` matrix of OOF predictions from the three base models using temporal folds
- [ ] Train a `LogisticRegression(C=1.0, max_iter=1000, solver="lbfgs")` meta-learner on the OOF prediction matrix and save it as `models/meta_learner.joblib`
- [ ] Update `detection/model_inference.py` `ModelInference` to load the meta-learner alongside base models and use it when available, falling back to equal-weight averaging when absent
- [ ] Add `meta_learner_auc_pr` and `meta_learner_auc_roc` to `models/training_metadata.json`, alongside base model metrics, to track whether stacking improves over averaging

## Technical Requirements

**OOF generation procedure:**
```python
def generate_oof_predictions(
    X_train: np.ndarray,
    y_train: np.ndarray,
    timestamps: np.ndarray,
    base_models: Dict[str, Any],  # {"rf": model, "xgb": model, "lgbm": model}
    n_splits: int = 5,
    gap_days: float = 7.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns:
        oof_proba: shape (n_train, 3) — OOF probabilities for each base model
        oof_indices: shape (n_train,) — mapping from oof rows to original X_train rows
    """
    oof_proba = np.zeros((len(X_train), 3))
    oof_mask = np.zeros(len(X_train), dtype=bool)
    for fold_idx, (train_idx, val_idx) in enumerate(walk_forward_cv(X_train, y_train, timestamps, n_splits, gap_days)):
        for model_col, (name, model_cls) in enumerate(base_models.items()):
            model = clone(model_cls)
            X_res, y_res = apply_oversampler(X_train[train_idx], y_train[train_idx])
            model.fit(X_res, y_res)
            oof_proba[val_idx, model_col] = model.predict_proba(X_train[val_idx])[:, 1]
        oof_mask[val_idx] = True
    return oof_proba[oof_mask], y_train[oof_mask]
```

**Meta-learner hyperparameters:**
```python
from sklearn.linear_model import LogisticRegression
meta_learner = LogisticRegression(
    C=1.0,               # regularisation; tune if OOF AUC-PR < base model average
    max_iter=1000,
    solver="lbfgs",
    class_weight="balanced",
    random_state=42,
)
```

**Why Logistic Regression as meta-learner?**
- Low variance: won't overfit the 3-dimensional OOF input (only 3 features)
- Produces calibrated probability outputs (important for the `RiskScore.confidence` field)
- Interpretable: the fitted coefficients reveal which base model the meta-learner trusts most
- Fast: < 1 ms inference on 3 inputs

**Feature engineering for the meta-learner (optional, configurable):**
In addition to the 3 raw OOF probabilities, optionally add:
- `model_disagreement = max(oof_proba, axis=1) - min(oof_proba, axis=1)` — high disagreement signals uncertainty
- `oof_mean = mean(oof_proba, axis=1)` — the current ensemble score as a feature

Enable with `STACKING_USE_DISAGREEMENT_FEATURES: bool = True` in `config/settings.py`.

**Inference integration:**
```python
class ModelInference:
    def score(self, features: np.ndarray) -> ScoringResult:
        rf_p = self.rf.predict_proba(features)[:, 1]
        xgb_p = self.xgb.predict_proba(features)[:, 1]
        lgbm_p = self.lgbm.predict_proba(features)[:, 1]
        stack_input = np.column_stack([rf_p, xgb_p, lgbm_p])
        if self.meta_learner is not None:
            ensemble_p = self.meta_learner.predict_proba(stack_input)[:, 1]
        else:
            ensemble_p = stack_input.mean(axis=1)
        return ScoringResult(...)
```

**Meta-learner persistence:**
- Save to `models/meta_learner.joblib` using `joblib.dump`
- `ModelInference.__init__` attempts to load `meta_learner.joblib`; if absent, logs `INFO "meta-learner not found; using equal-weight averaging"` and continues
- Add `meta_learner.joblib` to the `/health` endpoint model file check (see README health contract)

**Performance:**
- OOF generation (5 folds × 3 models × fit + predict): ~3× training cost — acceptable for offline training
- Meta-learner `predict_proba` on 3-dimensional input: < 0.1 ms per sample
- No impact on inference latency relative to base model scoring

**Coefficient logging:**
After meta-learner training, log at INFO level:
```
Meta-learner coefficients: rf=0.31, xgb=0.45, lgbm=0.24
Meta-learner intercept: -0.12
Meta-learner AUC-PR: 0.891 (vs. equal-weight average: 0.873)
```

## Security Considerations
- `meta_learner.joblib` is a serialised scikit-learn object; it must be cryptographically signed alongside the base models (see ISSUE-035) to prevent tampering
- The OOF generation process trains 15 model instances (3 models × 5 folds); ensure none of these intermediate models are persisted to disk, only the final trained base models and meta-learner
- Meta-learner coefficients logged at INFO level must not expose training data statistics; coefficients are model-internal parameters, not PII, so logging is acceptable

## Testing Requirements
- Unit tests covering:
  - `generate_oof_predictions()` with 5-fold temporal CV returns array of shape `(n_oof_samples, 3)` where `n_oof_samples < n_train` due to purge gaps
  - Meta-learner coefficients sum: `np.sum(meta_learner.coef_)` is positive (models contribute positively)
  - Inference with meta-learner loaded returns probabilities in [0, 1]
  - Inference with meta-learner absent falls back to equal-weight average
- Integration tests covering:
  - Full stacking pipeline on synthetic data: meta-learner AUC-PR ≥ equal-weight baseline AUC-PR (not guaranteed but expected on clean synthetic data)
  - `meta_learner.joblib` created after `train_models()` completes
  - `/health` endpoint returns `"models": "ok"` when `meta_learner.joblib` is present and non-empty
- Edge cases:
  - All base models output identical probabilities: meta-learner reduces to a constant weight (degenerate but valid)
  - One base model fails (exception): `generate_oof_predictions()` raises immediately with model name in error message
  - OOF set contains only one class: meta-learner logs WARNING and falls back to equal-weight averaging

## Documentation Requirements
- Update `detection/model_training.py` module docstring with the stacking pipeline architecture diagram
- Update `detection/model_inference.py` with an inline comment explaining the meta-learner fallback logic
- Add `meta_learner_auc_pr` and `meta_learner_coef` fields to the `training_metadata.json` schema documentation
- Add a `docs/ensemble_stacking.md` explaining OOF generation, why LR meta-learner is used, and how to interpret coefficients

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: sklearn stacking, out-of-fold prediction generation, logistic regression meta-learners, ensemble methods
- Your approach or initial thoughts on temporal OOF generation
- Estimated time to complete

**Ideal contributor profile:** ML engineer with experience building production stacking ensembles and understanding of OOF leakage prevention; familiarity with `sklearn.ensemble.StackingClassifier` internals is helpful but the custom OOF generation is required for temporal compliance.
