---
title: "Implement Adversarial Feature Augmentation During Training"
labels: ["difficulty: advanced", "area: ml", "type: enhancement"]
assignees: []
---

## Summary
Wash-trading bots are adaptive adversaries that observe detection signals and modify their behaviour to evade them. A model trained only on historical patterns is vulnerable to evasion once bots learn to suppress the features that the model relies on (e.g., slightly randomising trade amounts to improve Benford conformity, or adding noise trades to dilute counterparty concentration). Adversarial feature augmentation generates synthetic evasion examples during training — slight perturbations of confirmed wash-trading feature vectors that push them below the decision boundary — and trains the model to still classify them as wash-trading, improving robustness against adaptive adversaries.

## Background & Context
The threat model for LedgerLens is that sophisticated wash-trading bots will query the API (or reverse-engineer the detection logic) and iteratively adjust their on-chain behaviour to reduce their risk score. The most effective evasion strategies target the features with the highest SHAP values (see `detection/shap_explainer.py`).

Adversarial training adds a min-max objective: while the model tries to classify wash-trading correctly, a simulated adversary tries to find small feature perturbations that fool the model. Two approaches:

1. **FGSM-style gradient-based perturbation** (Fast Gradient Sign Method adapted for tabular data): use the gradient of the loss with respect to the input features to find the direction that most reduces the wash-trading probability, then add a small step in that direction to create an adversarial example
2. **Random feature masking** (simpler, less theoretically grounded but more computationally tractable): randomly zero out the top-K highest-SHAP features to simulate a bot that has learned to suppress them

Both should be implemented; the primary method should be gradient-based for the gradient-supporting models (XGBoost via `xgboost`'s DART mode or PyTorch wrapper, LightGBM) and FGSM-style for the GNN. For Random Forest (which has no gradient), fall back to random feature masking.

## Objectives
- [ ] Implement `AdversarialAugmenter` class in a new `detection/adversarial_features.py` with `generate_adversarial(X: np.ndarray, y: np.ndarray, model, method: str, epsilon: float) -> np.ndarray`
- [ ] Implement FGSM-style gradient perturbation for XGBoost/LightGBM using their `predict` gradient output, and random feature masking for Random Forest
- [ ] Integrate `AdversarialAugmenter` into `model_training.py` training loop: after each epoch/training round, augment the training set with adversarial examples (ratio: 20% of positive-class training samples) and retrain
- [ ] Add `adversarial_robustness_score` metric to `training_metadata.json`: fraction of adversarial examples that the retrained model still correctly classifies as wash-trading

## Technical Requirements

**Threat model — adversarial perturbations:**
The adversary can perturb feature values within a bounded region:
- **Feature-space ε-ball**: `‖δ‖_∞ ≤ ε` where δ is the perturbation vector and ε = 0.1 (10% of feature range per dimension)
- **Feature validity constraints**: perturbed features must remain in valid ranges (e.g., Benford chi-square ≥ 0, ring membership ∈ {0.0, 1.0} — binary features are not perturbed)
- **Semantically valid perturbations**: only perturb features that an adversary could realistically manipulate (chi-square, MAD, volume metrics) — not features the adversary cannot control (account age, initial funding source)

**Controllable feature mask:**
```python
ADVERSARIALLY_CONTROLLABLE_FEATURES = [
    "chi2_1h", "chi2_4h", "chi2_24h", "chi2_7d", "chi2_30d",
    "mad_1h", "mad_4h", "mad_24h", "mad_7d", "mad_30d",
    "volume_to_unique_counterparty_ratio",
    "intra_minute_clustering",
    "off_hours_activity_ratio",
    "volume_spike_frequency",
    "round_trip_trade_frequency",
    "counterparty_concentration_ratio",
    # NOT included: wash_ring_membership (graph-structural, hard to fake)
    # NOT included: account_age (fixed), funding_source_similarity (hard to fake)
]
```

**FGSM-style perturbation for XGBoost/LightGBM:**
XGBoost and LightGBM support first-order gradient approximation:
```python
def fgsm_xgb(model, x: np.ndarray, y_true: int, epsilon: float, feature_mask: np.ndarray) -> np.ndarray:
    """
    Approximate FGSM for gradient-boosted trees using finite differences.
    For each controllable feature i, estimate gradient: dL/dx_i ≈ (L(x+h_i) - L(x)) / h
    """
    h = 1e-3  # finite difference step
    x_adv = x.copy()
    proba = model.predict_proba([x])[0][1]
    loss = -np.log(proba + 1e-10) if y_true == 1 else -np.log(1 - proba + 1e-10)
    for i in range(len(x)):
        if not feature_mask[i]:
            continue
        x_pert = x.copy()
        x_pert[i] += h
        proba_pert = model.predict_proba([x_pert])[0][1]
        loss_pert = -np.log(proba_pert + 1e-10) if y_true == 1 else -np.log(1 - proba_pert + 1e-10)
        gradient = (loss_pert - loss) / h
        x_adv[i] -= epsilon * np.sign(gradient)  # gradient descent on loss (adversary minimises proba)
    # Clip to feature ranges
    x_adv = np.clip(x_adv, feature_min, feature_max)
    return x_adv
```

**Random feature masking for Random Forest:**
```python
def random_feature_mask(x: np.ndarray, top_shap_indices: List[int], n_mask: int = 3, random_state: int = 42) -> np.ndarray:
    """Zero out top-N SHAP features to simulate bot suppressing them."""
    rng = np.random.default_rng(random_state)
    x_adv = x.copy()
    mask_indices = rng.choice(top_shap_indices, size=n_mask, replace=False)
    x_adv[mask_indices] = 0.0
    return x_adv
```

**Augmentation loop integration:**
```python
# In model_training.py, after initial training:
augmenter = AdversarialAugmenter(epsilon=0.10, method="fgsm", n_augment_per_positive=1)
X_adv, y_adv = augmenter.generate_adversarial(X_train, y_train, initial_models)
X_aug = np.vstack([X_train, X_adv])
y_aug = np.hstack([y_train, y_adv])
# Retrain on augmented data:
final_models = train_models(X_aug, y_aug)
```

**`adversarial_robustness_score` metric:**
```python
# On validation set adversarial examples:
X_val_adv, y_val_adv = augmenter.generate_adversarial(X_val, y_val, final_models)
# Only evaluate on original wash-trading samples (y=1):
wash_mask = y_val == 1
robustness_score = (final_models.predict(X_val_adv[wash_mask]) == 1).mean()
# Ideal: robustness_score ≈ 1.0 (still detects all wash-trading after perturbation)
```

**Hyperparameters (add to `config/settings.py`):**
```python
ADVERSARIAL_EPSILON: float = 0.10         # max perturbation per feature (fraction of range)
ADVERSARIAL_N_AUGMENT_RATIO: float = 0.20 # fraction of positive samples to augment
ADVERSARIAL_N_FD_STEPS: int = 1           # FGSM finite-difference steps (1 = single-step)
ADVERSARIAL_N_MASK_FEATURES: int = 3     # features to zero out in masking method
```

**Performance:**
- Finite-difference gradient computation: O(n_controllable_features) model calls per sample; for 16 controllable features and 500 positive training samples: ~8,000 `predict_proba` calls — expected < 30s for XGBoost
- Cap augmentation to 1000 adversarial examples regardless of training set size

## Security Considerations
- Adversarial examples must only be generated from confirmed wash-trading samples (y=1); generating adversarial examples from clean samples could inadvertently teach the model to be less sensitive to clean wallets
- `feature_min` and `feature_max` bounds used for clipping must be computed from the **training** set, not the full dataset, to avoid leakage
- The `adversarial_robustness_score` must be computed on the **validation** set, not the training set; computing it on training data would artificially inflate the score (the model has seen those adversarial examples)
- This feature must not be used as a sole reason to promote a new model; it is an additional metric, not a replacement for AUC-PR evaluation

## Testing Requirements
- Unit tests covering:
  - `fgsm_xgb()`: perturbed feature values are within `[feature_min, feature_max]`
  - `fgsm_xgb()`: non-controllable features are not perturbed (feature_mask correctly applied)
  - `random_feature_mask()`: exactly `n_mask` features are zeroed out
  - `adversarial_robustness_score`: value is in [0.0, 1.0]
- Integration tests covering:
  - Full adversarial augmentation run on synthetic data: augmented dataset has more samples than original
  - Model trained with augmentation has robustness_score ≥ model without augmentation (expected on synthetic data)
  - `training_metadata.json` contains `adversarial_robustness_score` field after training with augmentation
- Edge cases:
  - 0 positive training samples: augmentation skipped with WARNING
  - `epsilon=0.0`: adversarial examples identical to originals; robustness_score should be the same as normal recall
  - All controllable features at boundary (min or max): perturbation clipped, no out-of-range values

## Documentation Requirements
- Create `detection/adversarial_features.py` with module docstring explaining the threat model and two perturbation methods
- Update `detection/model_training.py` with inline comments explaining the augmentation loop
- Add `ADVERSARIAL_EPSILON` and related constants to `config/settings.py`
- Create `docs/adversarial_robustness.md` (reference from README) documenting the threat model, perturbation methods, and `adversarial_robustness_score` interpretation

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: adversarial ML, FGSM, robustness training, XGBoost/LightGBM gradient access
- Your approach or initial thoughts on finite-difference gradient estimation for tree models
- Estimated time to complete

**Ideal contributor profile:** ML security researcher or engineer with experience in adversarial robustness; specific knowledge of adversarial examples for tabular/tree-based models (not just image classifiers) is essential since standard FGSM is designed for differentiable models.
