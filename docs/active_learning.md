# Active Learning — Analyst Feedback Loop

LedgerLens supports a human-in-the-loop feedback mechanism where analysts can submit label corrections (true wash-trade or false positive) that feed back into the retraining pipeline via importance-weighted sampling.

## Feedback Workflow

1. Analyst reviews a wallet's risk score via the dashboard or API.
2. If the score is incorrect, they submit a correction via `POST /v1/feedback` with:
   - `wallet`: Stellar wallet address
   - `asset_pair`: the asset pair being evaluated
   - `analyst_label`: `0` (clean) or `1` (wash)
   - `confidence`: analyst confidence in the correction `[0.0, 1.0]`
3. The correction is persisted to the `analyst_feedback` SQLite table.
4. On the next `cli.py retrain-check` cycle, corrections with existing feature vectors are merged into the training dataset with importance weights.

## Weight Formula

Each correction's importance weight at query time is:

```
weight = confidence × exp(-λ × days_since_correction)
```

Where:
- `confidence` ∈ [0.0, 1.0] is the analyst's stated confidence
- `λ` = `FEEDBACK_DECAY_LAMBDA` (default 0.05)
- `days_since_correction` = elapsed days since the correction was submitted

During training, correction sample weights are further multiplied by `feedback_weight_multiplier` (default 5.0) so corrections receive 5× the weight of synthetic samples at insertion time.

## Decay Constant Guidance

| λ     | Half-life (days) | Use case |
|-------|-------------------|----------|
| 0.01  | 69                | Slow decay — corrections stay relevant for months |
| 0.05  | 14                | Default — corrections fade over ~2 weeks |
| 0.10  | 7                 | Aggressive decay — only very recent corrections matter |

## Poisoning Risk Mitigation

An attacker with API access could flood the feedback store with false corrections to degrade model quality. Mitigations:

- `POST /v1/feedback` is gated behind `LEDGERLENS_ADMIN_API_KEY`
- Rate limited to 100 corrections per hour per IP
- The `confidence` field is bounded `[0, 1]` server-side (422 on violation)
- Corrections without feature vectors are stored for audit but excluded from training

## SQLite Migration

The `analyst_feedback` table is created automatically on first access. Schema:

```sql
CREATE TABLE IF NOT EXISTS analyst_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    asset_pair      TEXT NOT NULL,
    analyst_label   INTEGER NOT NULL CHECK(analyst_label IN (0, 1)),
    original_score  INTEGER NOT NULL CHECK(original_score BETWEEN 0 AND 100),
    confidence      REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    has_feature_vector INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `FEEDBACK_DECAY_LAMBDA` | 0.05 | Exponential decay constant for recency weighting |
