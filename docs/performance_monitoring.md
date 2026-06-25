# Model Performance Monitoring

LedgerLens tracks actual prediction accuracy against analyst-verified labels
using the `PerformanceMonitor` class. When F1 drops by more than 5 percentage
points from the training baseline, an automatic retraining is triggered.

## Feedback Collection Workflow

### Analyst Guide: Submitting Labels

When an analyst confirms or dismisses a LedgerLens flag, they submit a label
via the API:

```http
POST /performance/feedback
Content-Type: application/json

{
  "wallet": "GABCDEF...",
  "asset_pair": "XLM/USDC",
  "true_label": 1,
  "evidence_url": "https://stellarexplorer.org/tx/abc..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `wallet` | string | yes | Stellar account ID (G…) |
| `asset_pair` | string | yes | e.g. `"XLM/USDC"` |
| `true_label` | int | yes | `0` = clean, `1` = confirmed wash trade |
| `evidence_url` | string | no | HTTPS link to supporting evidence |

Responses:
- `201 Created`: `{"feedback_id": N, "recorded_at": "..."}` — label stored.
- `422`: `true_label` is not 0 or 1, or `evidence_url` is not HTTPS.
- `404`: no risk score found for the wallet/asset pair.

### Viewing the Performance Report

Administrators can view current performance metrics:

```http
GET /admin/performance-report
X-LedgerLens-Admin-Key: <admin-key>
```

Response includes `precision`, `recall`, `f1`, `n_samples`, and
`degradation_detected`.

## How Degradation Detection Works

1. **Baseline F1**: read from `models/training_metadata.json` key
   `val_f1_score` at training time.
2. **Rolling window**: feedback labels from the last 30 days
   (configurable via `PERFORMANCE_MONITORING_WINDOW_DAYS`).
3. **Threshold**: if `F1_current < F1_baseline − 0.05`, a
   `ModelDegradationAlert` is raised (configurable via
   `PERFORMANCE_DEGRADATION_THRESHOLD`).
4. **Minimum samples**: at least 20 feedback labels are required before the
   check runs (configurable via `PERFORMANCE_MIN_FEEDBACK_SAMPLES`).

## CLI Integration

`cli.py retrain-check` now checks both PSI drift and performance degradation:

```bash
python cli.py retrain-check
```

If degradation is detected, retraining is triggered regardless of PSI.

## Alert Interpretation

| Scenario | Action |
|----------|--------|
| F1 drop ≤ 0.05 | No action required |
| F1 drop > 0.05, n_samples < 20 | Collect more analyst labels |
| F1 drop > 0.05, n_samples ≥ 20 | Retraining triggered automatically |

## Security Considerations

- `submitted_by` is always set to `"local_api"` and is never user-supplied.
- `evidence_url` must be HTTPS, max 500 characters, and must not reference
  private/reserved IP ranges (SSRF protection).
- Feedback labels influence retraining decisions. A malicious actor submitting
  false labels could intentionally degrade model quality. In production,
  analyst identity should be verified via an authenticated API key.
- The `GET /admin/performance-report` endpoint is restricted to admin key
  holders only.
