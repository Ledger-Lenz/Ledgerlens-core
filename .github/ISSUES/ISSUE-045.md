---
title: "Add Conformal Prediction Uncertainty Intervals to Every RiskScore"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary

Harden `detection/conformal.py` so every `RiskScore` produced by the pipeline includes a valid 90% conformal prediction interval (`score_lower`, `score_upper`) and a prediction set. Implement split-conformal calibration with coverage guarantee validation. The uncertainty fields are already defined in the `RiskScore` schema (`score_lower`, `score_upper`, `prediction_set`, `coverage_guarantee`) but are currently populated only when calibration artifacts exist. This issue makes conformal intervals mandatory for all production scores.

## Background & Context

Conformal prediction is a distribution-free method for producing prediction intervals with a finite-sample coverage guarantee: for any new input, the interval contains the true label with probability ≥ 1−α. For LedgerLens, this means: "wallet W's true wash-trade status (0=clean, 1=wash) is included in the prediction set with probability ≥ 90%."

The split-conformal approach splits the training data into a proper training set and a calibration set. On the calibration set, nonconformity scores (e.g., `1 - p̂(y_true)`) are computed. The (1−α)-quantile of these scores becomes the threshold for the prediction set at test time.

Current gaps:
1. `conformal.py` has `ConformalCalibrator` but `calibrate()` is not called during `cli.py train`, so no calibration artifacts exist in the default pipeline
2. `model_inference.py` wraps `score_lower/score_upper` in a try/except that silently sets them to `None` when the calibrator is uninitialised
3. No coverage validation test confirms the empirical coverage matches the target 90%

This issue closes all three gaps and makes conformal intervals a first-class output of every pipeline run.

## Objectives

- [ ] Integrate `ConformalCalibrator.calibrate()` into `cli.py train` and `model_training.py` so calibration artifacts are always produced alongside model files
- [ ] Remove the silent `None` fallback in `model_inference.py`; raise `CalibrationMissingError` instead
- [ ] Implement `ConformalCalibrator.predict_interval(score: float, proba: np.ndarray) -> tuple[float, float]` using the stored quantile threshold
- [ ] Implement `ConformalCalibrator.predict_set(proba: np.ndarray) -> list[int]` returning class indices in the conformal prediction set
- [ ] Add `ConformalCalibrator.validate_coverage(X_val, y_val) -> float` returning empirical coverage on a held-out validation set
- [ ] Fail training if empirical coverage < 0.88 (warn if < 0.90)
- [ ] Persist calibration artifact as `models/conformal_calibration_v{hash}.pkl` alongside model files
- [ ] Expose `GET /admin/conformal-coverage` returning current calibration coverage and threshold
- [ ] Write tests confirming coverage ≥ 0.90 on synthetic data with known labels

## Technical Requirements

### Updated `ConformalCalibrator`

```python
# detection/conformal.py

import numpy as np
import pickle
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

@dataclass
class CalibrationArtifact:
    alpha: float                     # significance level, e.g. 0.10
    quantile_threshold: float        # (1-alpha)-quantile of calibration nonconformity scores
    n_calibration: int               # number of calibration samples
    empirical_coverage: float        # measured on calibration set
    model_version: str               # links to the model hash
    created_at: str                  # ISO timestamp

class CalibrationMissingError(RuntimeError):
    """Raised when conformal intervals are requested but no artifact is loaded."""

class ConformalCalibrator:
    def __init__(self, alpha: float = 0.10): ...

    def calibrate(
        self,
        model,                          # fitted sklearn-compatible estimator
        X_cal: np.ndarray,
        y_cal: np.ndarray,
        model_version: str,
    ) -> CalibrationArtifact:
        """
        Split-conformal calibration.
        Nonconformity score for class k: s_i = 1 - p̂_i(y_i)
        Quantile: q̂ = np.quantile(scores, np.ceil((n+1)*(1-alpha))/n)
        Store artifact and return it.
        """
        proba = model.predict_proba(X_cal)
        scores = 1.0 - proba[np.arange(len(y_cal)), y_cal.astype(int)]
        n = len(scores)
        q = np.quantile(scores, np.ceil((n + 1) * (1 - self.alpha)) / n)
        empirical = float(np.mean(scores <= q))
        self._artifact = CalibrationArtifact(
            alpha=self.alpha,
            quantile_threshold=float(q),
            n_calibration=n,
            empirical_coverage=empirical,
            model_version=model_version,
            created_at=...,
        )
        return self._artifact

    def predict_interval(
        self, score_raw: float, proba: np.ndarray
    ) -> tuple[float, float]:
        """
        Returns (score_lower, score_upper) in [0, 100] integer-equivalent scale.
        Uses the stored quantile threshold to widen the interval symmetrically
        around score_raw proportional to the threshold margin.
        Raises CalibrationMissingError if no artifact is loaded.
        """
        if self._artifact is None:
            raise CalibrationMissingError("No calibration artifact loaded.")
        margin = self._artifact.quantile_threshold * 100
        return (
            max(0.0, score_raw - margin),
            min(100.0, score_raw + margin),
        )

    def predict_set(self, proba: np.ndarray) -> list[int]:
        """
        Returns list of class indices k where 1 - proba[k] <= quantile_threshold.
        Typical result is [0] (clean), [1] (wash), or [0, 1] (uncertain).
        """
        ...

    def validate_coverage(
        self, X_val: np.ndarray, y_val: np.ndarray, model
    ) -> float:
        """
        Compute empirical coverage on a held-out set.
        Returns fraction of samples whose true label is in predict_set(proba_i).
        """
        ...

    def save(self, path: Path) -> None:
        with open(path, "wb") as f:
            pickle.dump(self._artifact, f)

    def load(self, path: Path) -> None:
        with open(path, "rb") as f:
            self._artifact = pickle.load(f)
```

### Integration with training pipeline

```python
# detection/model_training.py  (addition)
def train_and_calibrate(
    X_train, y_train,
    X_cal, y_cal,
    X_val, y_val,
    model_version: str,
    min_coverage: float = 0.88,
) -> tuple[dict, ConformalCalibrator]:
    """
    1. Train ensemble on X_train / y_train.
    2. Calibrate on X_cal / y_cal.
    3. Validate coverage on X_val / y_val; raise if < min_coverage.
    4. Save calibration artifact alongside model files.
    """
    ...
```

Data split rationale (document in code comments):
- 60% proper training
- 20% calibration (for conformal)
- 20% validation (for coverage check + standard AUC-ROC)

### Integration with inference

```python
# detection/model_inference.py  (updated)
def score_wallet(
    wallet: str, features: dict, calibrator: ConformalCalibrator
) -> RiskScore:
    proba = ensemble.predict_proba([feature_vector])[0]
    raw_score = int(proba[1] * 100)
    lower, upper = calibrator.predict_interval(raw_score, proba)
    prediction_set = calibrator.predict_set(proba)
    return RiskScore(
        wallet=wallet,
        ...
        score=raw_score,
        score_lower=lower,
        score_upper=upper,
        prediction_set=prediction_set,
        coverage_guarantee=1.0 - calibrator._artifact.alpha,
    )
```

### API endpoint

```python
@router.get("/admin/conformal-coverage")
async def conformal_coverage(
    x_admin_key: str = Header(..., alias="X-LedgerLens-Admin-Key"),
) -> dict:
    return {
        "alpha": ...,
        "quantile_threshold": ...,
        "n_calibration": ...,
        "empirical_coverage": ...,
        "model_version": ...,
    }
```

### Configuration

```
CONFORMAL_ALPHA=0.10
CONFORMAL_MIN_COVERAGE=0.88
```

## Security Considerations

- **Calibration artifact integrity**: the `.pkl` file must be SHA-256 hashed after writing and the hash stored in `training_metadata.json`. On load, re-verify the hash; raise `IntegrityError` if it doesn't match
- **Pickle deserialization**: the calibration artifact is a `CalibrationArtifact` dataclass containing only floats, ints, and strings. Validate the type after unpickling; reject any unexpected types to mitigate pickle injection (defense-in-depth)
- **`CalibrationMissingError` in production**: this error must be logged at `ERROR` level and surfaced in the `/health` endpoint (`models` field); it must never silently degrade to `score_lower=None`
- **Coverage regression**: if a newly trained model's calibration produces `empirical_coverage < min_coverage`, the build must fail (raise `ValueError`) and the old artifact must not be overwritten
- **Interval bounds**: `score_lower` and `score_upper` must always satisfy `0 <= score_lower <= score <= score_upper <= 100`; add an assertion in `predict_interval` that raises on violation

## Testing Requirements

- [ ] `tests/test_conformal.py` — unit and integration tests
- [ ] Test: `calibrate()` on 1000-sample synthetic calibration set produces `empirical_coverage >= 0.90`
- [ ] Test: `predict_interval()` always returns `lower <= score <= upper` and both in `[0, 100]`
- [ ] Test: `predict_set()` contains the true class for ≥ 90% of a held-out validation set
- [ ] Test: `validate_coverage()` raises `ValueError` when coverage < `min_coverage`
- [ ] Test: `save()` and `load()` round-trip produces identical `CalibrationArtifact`
- [ ] Test: loading a tampered artifact (wrong hash) raises `IntegrityError`
- [ ] Test: `CalibrationMissingError` is raised by `predict_interval()` when no artifact loaded
- [ ] Integration test: `cli.py train` produces a `conformal_calibration_v*.pkl` file
- [ ] Integration test: `GET /admin/conformal-coverage` returns correct fields (requires admin key)

## Documentation Requirements

- [ ] Full docstrings on `ConformalCalibrator` and `CalibrationArtifact`
- [ ] Update `docs/uncertainty_quantification.md` with the split-conformal methodology, the 60/20/20 data split rationale, and how to interpret `prediction_set = [0, 1]` (uncertain region)
- [ ] Update `README.md` `RiskScore` schema table to show `score_lower/score_upper` are now guaranteed non-null
- [ ] Document `GET /admin/conformal-coverage` in the API reference
- [ ] Update `.env.example` with `CONFORMAL_ALPHA` and `CONFORMAL_MIN_COVERAGE`

## Definition of Done

- [ ] `ConformalCalibrator` fully implemented with `calibrate`, `predict_interval`, `predict_set`, `validate_coverage`, `save`, `load`
- [ ] `cli.py train` always produces a calibration artifact alongside model files
- [ ] `model_inference.py` populates `score_lower`, `score_upper`, `prediction_set`, `coverage_guarantee` on every `RiskScore`
- [ ] `CalibrationMissingError` is raised (not silently ignored) when artifact is absent
- [ ] All tests pass including coverage ≥ 0.90 validation test
- [ ] Artifact integrity hash check implemented
- [ ] `GET /admin/conformal-coverage` live
- [ ] `docs/uncertainty_quantification.md` updated

## For Contributors

**Ideal contributor profile**: You have a solid understanding of conformal prediction theory (Venn predictors, split conformal, mondrian conformal) and have applied it in a production ML system. You are comfortable with NumPy array operations and scikit-learn's `predict_proba` interface. Familiarity with calibration techniques (Platt scaling, isotonic regression) provides useful context, though this issue specifically requires conformal (not Bayesian) intervals. Knowledge of Python pickle security is a bonus.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "conformal prediction", "ML calibration and uncertainty quantification", "production ML systems"
2. **Relevant experience** — ML systems where you implemented prediction intervals; any publications or notebooks on conformal prediction
3. **Approach / initial thoughts** — your view on split conformal vs cross-conformal for this use case; thoughts on the 60/20/20 data split
4. **Estimated time** — breakdown by component (calibrator, training integration, inference integration, API, tests, docs)
