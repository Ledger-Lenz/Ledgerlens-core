---
title: "Implement SMOTE Variants (ADASYN, Borderline-SMOTE) for Improved Class Imbalance Handling"
labels: ["difficulty: advanced", "area: ml", "type: enhancement"]
assignees: []
---

## Summary
`detection/model_training.py` currently uses standard SMOTE (Synthetic Minority Oversampling Technique) to address the severe class imbalance between wash-trading (minority) and clean (majority) wallets in the training dataset. Standard SMOTE generates synthetic minority samples uniformly along line segments between existing minority instances, which can introduce noise when minority samples are scattered in high-dimensional feature space. ADASYN (Adaptive Synthetic Sampling) and Borderline-SMOTE focus oversampling effort on the hardest-to-classify boundary regions, typically yielding better Precision-Recall AUC on imbalanced datasets. This issue implements both variants and selects the best-performing one for production use.

## Background & Context
In `detection/model_training.py`, the training pipeline applies `imblearn.over_sampling.SMOTE` before fitting the Random Forest, XGBoost, and LightGBM classifiers. The wash-trading prevalence in real SDEX data is estimated at 2–5%, creating an imbalance ratio of approximately 20:1 to 50:1 in the training set.

Standard SMOTE limitations in this context:
- Generates synthetic samples in all regions of minority space, including isolated outliers (noise amplification)
- Does not account for the difficulty of classifying near-boundary instances, which are the most discriminative for the classifier
- Does not adapt to local density, so sparse regions get the same oversampling as dense regions

ADASYN adaptively weights oversampling by the ratio of majority to minority neighbours in a k-NN neighbourhood, focusing generation on harder instances. Borderline-SMOTE-1 and Borderline-SMOTE-2 only oversample minority instances near the decision boundary (those with > 50% majority neighbours in their k-NN set).

The `imbalanced-learn` library (already a dependency via `imblearn`) provides all three variants. The comparison must use a fixed temporal train/val split (see ISSUE-027) and evaluate on AUC-PR, since accuracy and AUC-ROC can be misleading at high imbalance ratios.

## Objectives
- [ ] Add `imbalance_strategy: str` parameter to `train_models()` in `model_training.py` accepting `"smote"`, `"adasyn"`, `"borderline1"`, `"borderline2"`, `"none"` (default: `"smote"`)
- [ ] Implement `_get_oversampler(strategy: str, random_state: int) -> BaseOverSampler` factory function supporting all five strategies with tuned default hyperparameters
- [ ] Add a `compare_oversamplers()` function that trains all four oversampling strategies (SMOTE, ADASYN, Borderline-SMOTE-1, Borderline-SMOTE-2) on the same temporal split, evaluates AUC-PR on the validation set for each ensemble model, and returns a comparison DataFrame
- [ ] Persist the best-performing strategy name and its AUC-PR score to `models/training_metadata.json` alongside the existing AUC-ROC scores

## Technical Requirements

**Oversampler hyperparameters (defaults):**
```python
SMOTE:
  k_neighbors=5, sampling_strategy="minority", random_state=42

ADASYN:
  n_neighbors=5, sampling_strategy="minority", random_state=42
  # ADASYN may raise if all minority samples are within majority region;
  # catch ValueError and fall back to SMOTE, log WARNING

BorderlineSMOTE (strategy="borderline-1"):
  k_neighbors=5, m_neighbors=10, kind="borderline-1", random_state=42

BorderlineSMOTE (strategy="borderline-2"):
  k_neighbors=5, m_neighbors=10, kind="borderline-2", random_state=42
```

**Evaluation metric — AUC-PR (Priority Recall Curve AUC):**
- Use `sklearn.metrics.average_precision_score(y_val, proba_val[:, 1])`
- Report this alongside existing AUC-ROC; the primary selection criterion for oversampler is max AUC-PR
- In highly imbalanced regimes (positive rate < 5%), AUC-PR is more informative than AUC-ROC

**Comparison procedure:**
```
for oversampler in [SMOTE, ADASYN, Borderline-1, Borderline-2]:
    X_res, y_res = oversampler.fit_resample(X_train, y_train)
    for model in [RandomForest, XGBoost, LightGBM]:
        model.fit(X_res, y_res)
        proba = model.predict_proba(X_val)[:, 1]
        auc_pr = average_precision_score(y_val, proba)
        results[(oversampler_name, model_name)] = auc_pr
best_strategy = results.groupby(level=0).mean().idxmax()
```

**Selection and promotion:**
- The winning strategy is the one with the highest mean AUC-PR averaged across all three models
- If ADASYN raises `ValueError` (rare edge case), mark it as `auc_pr=0.0` and select from the remaining three
- Write `best_oversample_strategy` to `training_metadata.json`; use it as the default for all subsequent `train_models()` calls unless `--imbalance-strategy` CLI flag overrides it

**`cli.py` integration:**
- Add `--imbalance-strategy {smote,adasyn,borderline1,borderline2,compare}` flag to the `train` subcommand
- When `--imbalance-strategy compare` is passed, run `compare_oversamplers()` and print a formatted comparison table before training with the best strategy

**Performance:**
- ADASYN and Borderline-SMOTE are more computationally expensive than SMOTE (k-NN search); for 10,000 training samples: SMOTE < 2s, ADASYN < 10s, Borderline-SMOTE < 8s — acceptable for offline training
- The comparison run trains 12 model-oversampler combinations; target total runtime < 5 minutes

## Security Considerations
- The `random_state` for all oversamplers must be pinned at training time and stored in `training_metadata.json` to ensure reproducibility; using `random.randint()` or wall-clock seeds is prohibited
- ADASYN's `ValueError` fallback must not silently change the training data distribution without logging; always log `WARNING` when the fallback is triggered with the reason
- Oversampling must only be applied to the training split; it must never touch the validation or test splits, as this would cause data leakage

## Testing Requirements
- Unit tests covering:
  - `_get_oversampler("smote")` returns `SMOTE` instance with correct parameters
  - `_get_oversampler("adasyn")` returns `ADASYN` instance
  - ADASYN ValueError fallback: mock `ADASYN.fit_resample` to raise `ValueError`, verify SMOTE fallback and WARNING log
  - `compare_oversamplers()` returns DataFrame with 4 rows (one per strategy) × 3 columns (one per model)
- Integration tests covering:
  - Full `compare_oversamplers()` run on synthetic data (100 samples, 90% majority): completes without error, returns valid AUC-PR values
  - `training_metadata.json` updated with `best_oversample_strategy` after training
  - Validation set not modified by oversampler (assert `len(X_val)` unchanged)
- Edge cases:
  - Training set with exactly 1 minority sample: SMOTE, ADASYN, Borderline-SMOTE all fail gracefully (minimum 2 minority samples required by k-NN)
  - Perfectly balanced dataset (50/50): all strategies should produce comparable results (no dominant winner)
  - `imbalance_strategy="none"`: trains on unbalanced data, logs WARNING about imbalance ratio

## Documentation Requirements
- Update `detection/model_training.py` module docstring with the oversampler comparison workflow and selection criterion
- Update `cli.py` help text for the `train` subcommand with the new `--imbalance-strategy` flag
- Add a section to `docs/model_training.md` (create if absent) comparing SMOTE vs ADASYN vs Borderline-SMOTE with references and guidance on interpreting comparison results

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: `imbalanced-learn`, class imbalance in fraud detection, AUC-PR evaluation, sklearn pipelines
- Your approach or initial thoughts on the comparison procedure
- Estimated time to complete

**Ideal contributor profile:** ML engineer with hands-on experience with class-imbalance oversampling methods; familiarity with fraud detection evaluation metrics (AUC-PR vs AUC-ROC trade-offs) is essential.
