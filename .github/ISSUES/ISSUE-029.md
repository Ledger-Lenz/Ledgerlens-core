---
title: "Add Conformal Prediction Set Generation for Multi-Class Risk Levels"
labels: ["difficulty: advanced", "area: uncertainty", "type: feature"]
assignees: []
---

## Summary
The existing `detection/conformal.py` provides scalar confidence intervals for the binary wash-trading score, but does not produce **prediction sets** for a three-class risk taxonomy (`clean`, `suspicious`, `wash`). Conformal prediction sets are finite subsets of class labels that are guaranteed to contain the true label with a user-specified probability (e.g., 90%), providing mathematically rigorous uncertainty quantification. This issue extends the RAPS (Regularised Adaptive Prediction Sets) conformal procedure to output `{clean}`, `{suspicious}`, `{clean, suspicious}`, `{suspicious, wash}`, or `{clean, suspicious, wash}` sets with valid 90% marginal coverage guarantees.

## Background & Context
`detection/conformal.py` currently implements a split-conformal calibration step for the binary case: it computes nonconformity scores on a held-out calibration set, finds the (1-α)-quantile threshold, and uses it to produce `score_lower` and `score_upper` bounds on the `RiskScore`.

Extending to multi-class conformal prediction requires:
1. **Defining the three-class taxonomy**: `0=clean (score 0–33)`, `1=suspicious (score 34–66)`, `2=wash (score 67–100)`
2. **Using RAPS** (Angelopoulos et al., 2021) rather than vanilla LAC (Label-Conditional) conformal, because RAPS's regularisation term reduces prediction set sizes while maintaining coverage
3. **Calibration set**: a held-out set of `(feature_vector, true_class)` pairs not used in model training — the existing calibration split in `conformal.py` can be reused
4. **Prediction set construction**: for a new test point, include class `k` in the prediction set if the conformal nonconformity score for class `k` is below the calibration threshold `q_hat`

The `RiskScore` schema in `detection/risk_score.py` already has `prediction_set: list[int] | None` and `coverage_guarantee: float | None` fields defined for this exact purpose. They are currently unpopulated — this issue implements the logic that fills them.

## Objectives
- [ ] Implement `RAPSConformal` class in `detection/conformal.py` with `calibrate(cal_softmax_probs, cal_labels)` and `predict_set(test_softmax_probs, alpha=0.10) -> List[int]` methods
- [ ] Implement `ConformalCalibrator.calibrate_multiclass()` method that takes the three-class ensemble output (softmax probabilities from all three models averaged) and produces RAPS calibration artifacts
- [ ] Populate `RiskScore.prediction_set` and `RiskScore.coverage_guarantee` in `detection/risk_score.py` `RiskScore.combine()` when conformal calibration artifacts are available
- [ ] Add an empirical coverage validation function `validate_coverage(cal_probs, cal_labels, alpha) -> float` that asserts the achieved coverage is within 2% of the target `1 − alpha`

## Technical Requirements

**Three-class taxonomy:**
```python
CLASS_LABELS = {0: "clean", 1: "suspicious", 2: "wash"}
CLASS_BOUNDARIES = {
    0: (0, 33),    # score 0–33 → clean
    1: (34, 66),   # score 34–66 → suspicious
    2: (67, 100),  # score 67–100 → wash
}

def score_to_class(score: int) -> int:
    if score <= 33: return 0
    if score <= 66: return 1
    return 2
```

**RAPS nonconformity score:**
The RAPS score for class `k` given softmax probabilities `π = [π_1, ..., π_K]` sorted in descending order is:
```
s(x, y) = Σ_{j: π_j ≥ π_y} π_j  +  λ · (o(y) − 1)
```
where `o(y)` is the rank of class `y` in the sorted softmax (1-indexed), and `λ` is a regularisation parameter (default `λ = 0.2`, `k_reg = 2`).

In Python:
```python
def raps_score(softmax_probs: np.ndarray, true_class: int, lambda_reg: float = 0.2, k_reg: int = 2) -> float:
    sorted_idx = np.argsort(-softmax_probs)  # descending
    rank = np.where(sorted_idx == true_class)[0][0]  # 0-indexed rank
    cumsum = np.cumsum(softmax_probs[sorted_idx])
    score = cumsum[rank] + lambda_reg * max(rank + 1 - k_reg, 0)
    return float(score)
```

**Calibration:**
```python
def calibrate(cal_softmax_probs: np.ndarray, cal_labels: np.ndarray, alpha: float = 0.10) -> float:
    """Returns q_hat: the (1-alpha)(1+1/n)-quantile of calibration scores."""
    n = len(cal_labels)
    scores = np.array([
        raps_score(cal_softmax_probs[i], cal_labels[i])
        for i in range(n)
    ])
    q_level = np.ceil((1 - alpha) * (1 + 1/n)) / (1 + 1/n)
    q_hat = np.quantile(scores, q_level, method="higher")
    return q_hat
```

**Prediction set construction:**
```python
def predict_set(softmax_probs: np.ndarray, q_hat: float, lambda_reg: float = 0.2, k_reg: int = 2) -> List[int]:
    """Returns list of class indices included in prediction set."""
    prediction_set = []
    for k in range(len(softmax_probs)):
        score = raps_score(softmax_probs, k, lambda_reg, k_reg)
        if score <= q_hat:
            prediction_set.append(k)
    return sorted(prediction_set)
```

**Ensemble softmax aggregation:**
- Average the softmax probabilities across all three models (RF, XGBoost, LightGBM) using `np.mean([rf_proba, xgb_proba, lgbm_proba], axis=0)` for the 3-class case
- For RF and LightGBM (binary classifiers by default), convert binary `[p_negative, p_positive]` to 3-class softmax by: map `p_positive` to class boundaries via `score_to_class(p_positive * 100)` and redistribute probability mass

**Coverage validation:**
```python
def validate_coverage(cal_probs, cal_labels, q_hat, alpha, tolerance=0.02) -> float:
    n = len(cal_labels)
    covered = sum(
        cal_labels[i] in predict_set(cal_probs[i], q_hat)
        for i in range(n)
    )
    achieved = covered / n
    assert abs(achieved - (1 - alpha)) <= tolerance, f"Coverage {achieved:.3f} deviates from target {1-alpha:.3f}"
    return achieved
```

**Calibration artifacts persistence:**
- Store `{"q_hat": float, "lambda_reg": float, "k_reg": int, "alpha": float, "n_calibration": int, "achieved_coverage": float}` in `models/conformal_calibration.json`
- Load at inference time in `detection/model_inference.py` before scoring

**`RiskScore` population:**
```python
risk_score.prediction_set = predict_set(ensemble_softmax, q_hat)
risk_score.coverage_guarantee = 1.0 - alpha  # e.g., 0.90
```

## Security Considerations
- `q_hat` must be validated as a finite positive float before use; a corrupted `conformal_calibration.json` with `q_hat=Inf` would include all classes in every prediction set, which is useless but not harmful — still validate and log WARNING
- The calibration set must be strictly held out from training and oversampling; contamination inflates coverage estimates (same leakage concern as ISSUE-027)
- Do not include calibration set samples (with their labels) in any API response; `conformal_calibration.json` should only contain aggregate statistics

## Testing Requirements
- Unit tests covering:
  - `raps_score()`: verify known values for simple 3-class softmax `[0.7, 0.2, 0.1]` with `true_class=0` (score = 0.7)
  - `raps_score()` with `true_class=2` (lowest probability): score includes cumulative sum over classes 0 and 1
  - `calibrate()` on 1000 calibration samples: `q_hat` is finite and positive
  - `predict_set()`: at `q_hat=Inf`, returns all 3 classes; at `q_hat=0`, returns empty set (or only highest-prob class)
- Integration tests covering:
  - `validate_coverage()` on calibration set achieves within 2% of target 90% coverage
  - `RiskScore.prediction_set` populated correctly after full pipeline run
  - `conformal_calibration.json` written and reloaded correctly
- Edge cases:
  - Calibration set with only 10 samples: `q_hat` computed but flagged as unreliable (n < 100)
  - All calibration samples from class 2 (wash): valid but single-class calibration; document as limitation
  - `alpha = 0.0` (100% target coverage): prediction set contains all classes for every sample

## Documentation Requirements
- Update `detection/conformal.py` module docstring with RAPS algorithm, parameter semantics, and coverage guarantee
- Update `detection/risk_score.py` `RiskScore` docstring explaining `prediction_set` semantics (which integers, what coverage means)
- Add a `docs/uncertainty_quantification.md` (update if exists) with plain-English explanation of prediction sets for non-statistician readers

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: conformal prediction, RAPS algorithm, uncertainty quantification, multi-class classification
- Your approach or initial thoughts on ensemble softmax aggregation for 3-class extension
- Estimated time to complete

**Ideal contributor profile:** ML researcher or engineer with solid statistical foundations; direct experience with conformal prediction theory (Angelopoulos et al., 2021) and Python implementation of nonconformity scores is ideal.
