---
title: "Implement Model Performance Monitoring with Automated Degradation Alerts"
labels: ["difficulty: advanced", "area: mlops", "type: feature"]
assignees: []
---

## Summary
`detection/drift_monitor.py` currently tracks feature distribution drift (PSI) but does not monitor the actual prediction performance of the deployed models over time. Without performance monitoring, model degradation — caused by concept drift as wash-trading strategies evolve — is invisible until it manifests as missed fraud. This issue implements a feedback loop that collects ground-truth labels from human analysts or confirmed fraud reports, computes precision/recall/F1 against those labels, and automatically raises an alert and triggers the retraining pipeline when F1 drops more than 5 percentage points from the baseline established at training time.

## Background & Context
The `detection/drift_monitor.py` module provides `DriftMonitor.check_psi()` which computes Population Stability Index for each feature dimension. PSI measures *input* distribution shift but cannot distinguish between (a) the input distribution shifting in a way that preserves model accuracy, and (b) the input distribution shifting because the model has become less accurate.

A model performance monitoring loop requires:
1. **Ground-truth collection**: when a wallet is flagged as wash-trading by LedgerLens (score > threshold), an analyst can confirm or dismiss the flag. These labels are stored in a `feedback_labels` table in SQLite.
2. **Rolling performance computation**: on each `retrain-check` run, compute precision, recall, and F1 on all feedback-labelled samples from the last 30 days.
3. **Degradation detection**: if `F1_current < F1_baseline - 0.05`, raise `ModelDegradationAlert` and trigger the retraining pipeline.
4. **Alerting**: write the alert to the SQLite `degradation_alerts` table and (if configured) call the webhook delivery worker with a `model_degradation` event.

The baseline F1 is recorded in `models/training_metadata.json` at training time and should be computed on the temporal validation set (see ISSUE-027), not the training set.

## Objectives
- [ ] Implement `PerformanceMonitor` class in `detection/drift_monitor.py` with `record_feedback(wallet, asset_pair, predicted_score, true_label)` and `compute_performance_metrics(days=30) -> PerformanceReport` methods
- [ ] Add `ModelDegradationAlert` exception and `check_degradation(baseline_f1, f1_threshold_drop=0.05) -> bool` method that raises the alert when degradation exceeds the threshold
- [ ] Create `feedback_labels` and `degradation_alerts` SQLite tables (via `detection/storage.py`'s migration mechanism) and add `POST /feedback` API endpoint to `api/main.py` for analyst label submission
- [ ] Integrate `PerformanceMonitor.check_degradation()` into `cli.py retrain-check` so degradation triggers automatic retraining alongside drift-based retraining

## Technical Requirements

**`feedback_labels` table schema:**
```sql
CREATE TABLE IF NOT EXISTS feedback_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    asset_pair TEXT NOT NULL,
    predicted_score INTEGER NOT NULL,
    true_label INTEGER NOT NULL CHECK(true_label IN (0, 1)),  -- 0=clean, 1=wash
    submitted_by TEXT,          -- analyst ID or "api"
    evidence_url TEXT,          -- optional link to evidence
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score_version TEXT          -- model version that produced predicted_score
);
CREATE INDEX IF NOT EXISTS idx_feedback_recorded_at ON feedback_labels(recorded_at);
```

**`degradation_alerts` table schema:**
```sql
CREATE TABLE IF NOT EXISTS degradation_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    baseline_f1 REAL,
    current_f1 REAL,
    f1_drop REAL,
    precision_current REAL,
    recall_current REAL,
    n_feedback_samples INTEGER,
    model_version TEXT,
    retrain_triggered INTEGER DEFAULT 0
);
```

**`PerformanceReport` dataclass:**
```python
@dataclass
class PerformanceReport:
    precision: float
    recall: float
    f1: float
    n_samples: int
    n_positive_labels: int
    n_negative_labels: int
    window_days: int
    computed_at: datetime
    degradation_detected: bool
    f1_drop: float | None
```

**Precision/Recall/F1 computation:**
- Threshold the `predicted_score` at `RISK_SCORE_THRESHOLD` (default 70) to produce binary predictions
- `precision = TP / (TP + FP)`, `recall = TP / (TP + FN)`, `F1 = 2 × precision × recall / (precision + recall)`
- Handle zero-division: if `precision + recall = 0`, `F1 = 0.0` and log WARNING "no positive predictions in feedback window"
- Minimum sample size: require ≥ 20 feedback samples before computing performance metrics; log WARNING and skip alert check if fewer available

**Degradation threshold:**
```python
PERFORMANCE_DEGRADATION_THRESHOLD: float = 0.05  # F1 drop in config/settings.py
PERFORMANCE_MIN_FEEDBACK_SAMPLES: int = 20
PERFORMANCE_MONITORING_WINDOW_DAYS: int = 30
```

**Baseline F1 loading:**
- Read `baseline_f1` from `models/training_metadata.json` key `"val_f1_score"` (add this field to the training pipeline output)
- If `training_metadata.json` does not contain `val_f1_score`, fall back to `0.0` and log WARNING "baseline F1 not available; degradation check skipped"

**`POST /feedback` API endpoint:**
```
POST /feedback
Content-Type: application/json
{
  "wallet": "GABCDEF...",
  "asset_pair": "XLM/USDC",
  "true_label": 1,
  "evidence_url": "https://stellarexplorer.org/..."
}
→ 201 Created: {"feedback_id": 42, "recorded_at": "..."}
→ 422: validation error (true_label not 0 or 1)
→ 404: wallet/score not found in risk_scores table
```

**Webhook integration:**
- When `ModelDegradationAlert` is raised, construct a `model_degradation` webhook event payload and insert it into the webhook delivery queue (same mechanism as risk score alerts)
- Payload: `{"event": "model_degradation", "data": {"f1_drop": 0.08, "current_f1": 0.71, "baseline_f1": 0.79, "model_version": "abc12345"}}`

**`GET /admin/performance-report` endpoint:**
- Returns latest `PerformanceReport` as JSON
- Requires `X-LedgerLens-Admin-Key` header (same admin auth as `/admin/drift-reports`)

## Security Considerations
- `submitted_by` field in `feedback_labels` must be derived from an authenticated API call, not user-supplied; in the local API, default to `"local_api"` and document that production should use an authenticated analyst identity
- `evidence_url` must be validated: HTTPS only, maximum 500 characters, validated with `urllib.parse.urlparse`; reject HTTP URLs and private/reserved IP ranges (same SSRF protection as webhook subscriber URLs)
- Feedback labels influence retraining decisions; a malicious actor submitting false feedback could degrade model quality intentionally — document this threat model in the security considerations section of `docs/`
- The `degradation_alerts` table contains model performance metrics that reveal internal model quality; restrict API access to admin key holders only

## Testing Requirements
- Unit tests covering:
  - `compute_performance_metrics()`: 10 TP, 5 FP, 3 FN → precision=0.667, recall=0.769, F1=0.714
  - `check_degradation()`: `baseline_f1=0.80, current_f1=0.74` → `True` (drop = 0.06 > 0.05)
  - `check_degradation()`: `baseline_f1=0.80, current_f1=0.76` → `False` (drop = 0.04 < 0.05)
  - Zero-division: 0 TP, 0 FP → F1=0.0, no exception
- Integration tests covering:
  - `POST /feedback` with valid payload → 201, record written to SQLite
  - `POST /feedback` with `true_label=2` → 422
  - `GET /admin/performance-report` without admin key → 503
  - Full `retrain-check` run triggers retraining when mock `check_degradation()` returns `True`
- Edge cases:
  - 19 feedback samples (< 20 minimum): report computed but degradation check skipped
  - `training_metadata.json` missing `val_f1_score`: graceful fallback, no crash
  - All feedback samples negative (no wash labels): precision=1.0, recall=0.0, F1=0.0

## Documentation Requirements
- Update `detection/drift_monitor.py` module docstring with `PerformanceMonitor` class documentation and the feedback loop architecture
- Add `feedback_labels` and `degradation_alerts` table schemas to `detection/storage.py` docstring
- Update `cli.py` help text explaining that `retrain-check` now checks both PSI drift and performance degradation
- Add a `docs/performance_monitoring.md` covering the feedback collection workflow, analyst guide to submitting labels, and alert interpretation

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: MLOps, model monitoring, precision/recall evaluation, FastAPI, SQLite
- Your approach or initial thoughts on the feedback collection workflow
- Estimated time to complete

**Ideal contributor profile:** MLOps engineer with experience building model monitoring and feedback loops in production; familiarity with fraud detection operational workflows (analyst triage, label quality) is a strong plus.
