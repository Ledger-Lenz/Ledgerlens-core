---
title: "Implement Adaptive Ensemble Reweighter Based on Recent Label Feedback"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/adaptive_reweighter.py` to dynamically reweight RF/XGBoost/LightGBM ensemble votes using a sliding window of analyst-confirmed true/false positives. Weights should converge to the best-performing model within 7 days of confirmed labels without requiring a full retrain. This allows the ensemble to self-correct when, for example, XGBoost starts producing false positives on a new market pattern while RF remains accurate.

## Background & Context

The LedgerLens ensemble currently uses fixed weights: `0.3 * RF + 0.4 * XGBoost + 0.3 * LightGBM`. These weights were set at training time based on validation AUC-ROC and do not adapt to production feedback. In practice, model performance diverges after deployment:

- New market patterns may exploit XGBoost's high sensitivity, producing false positives
- Random Forest's conservative nature may miss new wash-trade variants that XGBoost catches
- LightGBM may overfit to volume patterns that shift after a major SDEX protocol upgrade

Analyst-confirmed labels (true positive = wallet correctly flagged as wash trader; false positive = wallet incorrectly flagged) provide ground truth signals. With 5–10 confirmed labels per day over 7 days, we have 35–70 ground truth signals — sufficient to estimate per-model precision/recall and adjust weights accordingly.

The adaptive reweighter uses an online learning approach: weights are updated after each batch of confirmed labels using exponential moving averages of per-model F1 scores. A stability constraint prevents wild swings: no model's weight can change by more than 0.1 per 24-hour period, and the minimum weight for any model is 0.1 (no model is fully suppressed).

## Objectives

- [ ] Implement `LabelFeedback` dataclass for analyst-confirmed TP/FP events
- [ ] Implement `ModelPerformanceTracker` computing per-model rolling precision, recall, and F1 over a configurable window
- [ ] Implement `AdaptiveReweighter` that updates ensemble weights from `ModelPerformanceTracker` outputs
- [ ] Implement stability constraints (max_delta_per_day=0.1, min_weight=0.1)
- [ ] Persist weights in SQLite `ensemble_weights_history` table; on startup, load the most recent weights
- [ ] Expose `GET /admin/ensemble-weights` and `POST /admin/label-feedback` endpoints
- [ ] Integrate the reweighter into `detection/model_inference.py` so every scoring call uses the current weights
- [ ] Write tests verifying weight convergence, stability constraints, and cold-start behaviour

## Technical Requirements

### Data structures

```python
# detection/adaptive_reweighter.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ModelName = Literal["random_forest", "xgboost", "lightgbm"]

@dataclass
class LabelFeedback:
    wallet: str
    asset_pair: str
    true_label: int          # 1 = wash trader, 0 = clean
    model_scores: dict[str, float]  # {model_name: raw score 0–100}
    ensemble_score: int       # score that was shown to analyst
    analyst_id: str           # anonymised analyst identifier
    confirmed_at: datetime = field(default_factory=datetime.utcnow)

@dataclass
class ModelWeights:
    random_forest: float
    xgboost: float
    lightgbm: float
    computed_at: datetime = field(default_factory=datetime.utcnow)
    n_feedback_samples: int = 0

    def to_dict(self) -> dict[str, float]:
        return {
            "random_forest": self.random_forest,
            "xgboost": self.xgboost,
            "lightgbm": self.lightgbm,
        }

    def normalised(self) -> "ModelWeights":
        """Return copy with weights summing to 1.0."""
        total = self.random_forest + self.xgboost + self.lightgbm
        return ModelWeights(
            random_forest=self.random_forest / total,
            xgboost=self.xgboost / total,
            lightgbm=self.lightgbm / total,
            computed_at=self.computed_at,
            n_feedback_samples=self.n_feedback_samples,
        )
```

### ModelPerformanceTracker

```python
import collections
import numpy as np

class ModelPerformanceTracker:
    def __init__(
        self,
        window_days: int = 7,
        detection_threshold: float = 50.0,
    ): ...

    def add_feedback(self, feedback: LabelFeedback) -> None:
        """Add one analyst confirmation to the rolling window."""
        ...

    def f1_score(self, model: ModelName) -> float:
        """
        Compute F1 score for the specified model over the rolling window.
        Uses detection_threshold to binarize model_scores[model].
        Returns 0.5 (neutral) if < 10 samples in window.
        """
        ...

    def precision(self, model: ModelName) -> float:
        """True positives / (true positives + false positives) over rolling window."""
        ...

    def recall(self, model: ModelName) -> float:
        """True positives / (true positives + false negatives) over rolling window."""
        ...

    @property
    def n_samples(self) -> int:
        """Number of samples in the current rolling window."""
        ...
```

### AdaptiveReweighter

```python
DEFAULT_WEIGHTS = ModelWeights(
    random_forest=0.30,
    xgboost=0.40,
    lightgbm=0.30,
)

class AdaptiveReweighter:
    def __init__(
        self,
        tracker: ModelPerformanceTracker,
        ema_alpha: float = 0.1,            # exponential moving average alpha
        max_delta_per_day: float = 0.10,
        min_weight: float = 0.10,
        min_feedback_samples: int = 10,    # don't update weights below this
    ): ...

    def update_weights(self, current_weights: ModelWeights) -> ModelWeights:
        """
        Compute new weights based on per-model F1 scores.
        1. Compute F1 for each model from tracker.
        2. Softmax of F1 scores → target weights.
        3. EMA: new_w = alpha * target_w + (1 - alpha) * current_w
        4. Apply stability constraint: clip delta to [-max_delta_per_day, +max_delta_per_day].
        5. Apply minimum weight constraint: clip to [min_weight, 1 - 2*min_weight].
        6. Renormalise to sum to 1.0.
        Returns new ModelWeights (does not mutate current_weights).
        """
        if self._tracker.n_samples < self.min_feedback_samples:
            return current_weights  # cold start: don't change weights

        f1_scores = {m: self._tracker.f1_score(m) for m in ["random_forest", "xgboost", "lightgbm"]}
        f1_arr = np.array(list(f1_scores.values()))
        target = np.exp(f1_arr) / np.exp(f1_arr).sum()  # softmax
        current_arr = np.array([
            current_weights.random_forest,
            current_weights.xgboost,
            current_weights.lightgbm,
        ])
        ema_arr = self.ema_alpha * target + (1 - self.ema_alpha) * current_arr
        delta = np.clip(ema_arr - current_arr, -self.max_delta_per_day, self.max_delta_per_day)
        new_arr = np.clip(current_arr + delta, self.min_weight, 1.0)
        new_arr /= new_arr.sum()
        return ModelWeights(
            random_forest=float(new_arr[0]),
            xgboost=float(new_arr[1]),
            lightgbm=float(new_arr[2]),
            n_feedback_samples=self._tracker.n_samples,
        )
```

### SQLite persistence

```sql
CREATE TABLE IF NOT EXISTS ensemble_weights_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    random_forest REAL NOT NULL,
    xgboost     REAL NOT NULL,
    lightgbm    REAL NOT NULL,
    n_feedback_samples INTEGER NOT NULL,
    computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS label_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet      TEXT NOT NULL,
    asset_pair  TEXT NOT NULL,
    true_label  INTEGER NOT NULL,
    model_scores_json TEXT NOT NULL,
    ensemble_score INTEGER NOT NULL,
    analyst_id  TEXT NOT NULL,
    confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### API endpoints

```python
@router.get("/admin/ensemble-weights")
async def get_ensemble_weights(
    x_admin_key: str = Header(..., alias="X-LedgerLens-Admin-Key"),
) -> ModelWeightsResponse:
    """Return current and historical ensemble weights (last 30 days)."""
    ...

@router.post("/admin/label-feedback")
async def submit_label_feedback(
    body: LabelFeedbackRequest,
    x_admin_key: str = Header(..., alias="X-LedgerLens-Admin-Key"),
) -> dict:
    """Submit analyst-confirmed label. Triggers weight update if >= min_feedback_samples."""
    ...
```

### Configuration

```
ADAPTIVE_REWEIGHT_EMA_ALPHA=0.1
ADAPTIVE_REWEIGHT_MAX_DELTA_PER_DAY=0.10
ADAPTIVE_REWEIGHT_MIN_WEIGHT=0.10
ADAPTIVE_REWEIGHT_WINDOW_DAYS=7
ADAPTIVE_REWEIGHT_MIN_FEEDBACK_SAMPLES=10
ADAPTIVE_REWEIGHT_DETECTION_THRESHOLD=50.0
```

## Security Considerations

- **Label injection attack**: a malicious analyst could submit false labels to manipulate weights (e.g., marking all XGBoost TPs as FPs to suppress its weight). Mitigate by: (1) requiring `analyst_id` to be a pre-registered identifier in a separate admin allowlist table, (2) capping any single analyst's contribution to 30% of the rolling window
- **Weight floor enforcement**: the `min_weight=0.10` floor prevents any model from being suppressed to zero weight, which would be a single point of failure if the remaining models are both fooled by the same attack
- **Weight history auditability**: every weight update is stored in `ensemble_weights_history` with timestamp and sample count. Never update weights in-place — always append a new row
- **Analyst ID privacy**: `analyst_id` must never be a real name or email address in the database. Use a SHA-256 hash of the analyst's identifier at the API boundary; store only the hash
- **Cold start safety**: when fewer than `min_feedback_samples` labels exist (first days of deployment), the reweighter must return `DEFAULT_WEIGHTS` unchanged. Log INFO that the reweighter is in cold-start mode

## Testing Requirements

- [ ] `tests/test_adaptive_reweighter.py`
- [ ] Test: `ModelPerformanceTracker.f1_score` returns 0.5 for < 10 samples
- [ ] Test: correct F1 computation for 20 samples with known TP/FP/FN counts
- [ ] Test: weights converge toward best-performing model after 50 feedback samples
- [ ] Test: stability constraint — weight delta clamped to max_delta_per_day even when target diverges sharply
- [ ] Test: min_weight constraint — no model drops below 0.10 regardless of F1 scores
- [ ] Test: cold-start guard — `update_weights` returns DEFAULT_WEIGHTS when n_samples < min_feedback_samples
- [ ] Test: weights sum to 1.0 after normalisation (within 1e-9 tolerance)
- [ ] Test: `POST /admin/label-feedback` returns 422 for unregistered analyst_id
- [ ] Integration test: `GET /admin/ensemble-weights` returns historical weights in correct schema

## Documentation Requirements

- [ ] Docstrings on `AdaptiveReweighter`, `ModelPerformanceTracker`, `ModelWeights`, `LabelFeedback`
- [ ] Add `docs/adaptive_reweighting.md` covering the EMA update rule, stability constraints, cold-start policy, label injection threat model, and how to interpret weight history
- [ ] Update `README.md` ML layer section to mention adaptive ensemble reweighting
- [ ] Document both SQLite tables in `docs/database_schema.md`
- [ ] Update `.env.example` with six new configuration variables

## Definition of Done

- [ ] `AdaptiveReweighter` and `ModelPerformanceTracker` fully implemented
- [ ] Weights loaded from SQLite on startup; persisted after each update
- [ ] `detection/model_inference.py` uses adaptive weights in ensemble combination
- [ ] `GET /admin/ensemble-weights` and `POST /admin/label-feedback` endpoints live
- [ ] Stability and min_weight constraints verified by tests
- [ ] Cold-start guard verified by test
- [ ] `docs/adaptive_reweighting.md` authored

## For Contributors

**Ideal contributor profile**: You have experience with online learning algorithms, bandit algorithms, or adaptive ensemble methods in production ML systems. You understand exponential moving averages, softmax weight assignment, and the exploration-exploitation tradeoff. Familiarity with F1 score computation on imbalanced datasets and the practical challenges of collecting analyst feedback labels is important. Experience building feedback loops in fraud detection or content moderation systems is highly relevant.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "online learning / adaptive ensembles", "ML feedback loop systems", "fraud detection with human-in-the-loop"
2. **Relevant experience** — adaptive ensemble or bandit systems you have built; experience collecting and using analyst feedback in ML pipelines
3. **Approach / initial thoughts** — your view on EMA vs Bayesian updating for weight estimation; concerns about the label injection attack and the analyst-cap mitigation; whether 7 days is a sufficient convergence window
4. **Estimated time** — breakdown by component (tracker, reweighter, persistence, API, inference integration, tests, docs)
