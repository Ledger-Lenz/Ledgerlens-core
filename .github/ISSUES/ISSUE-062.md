---
title: "Build Analyst Feedback Store with Active Learning Loop for Continuous Model Improvement"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/feedback_store.py` to persist analyst label corrections (true wash-trade / false positive) with importance weights and feed them back into the retraining pipeline. Implement importance-weighted sampling in `detection/model_training.py` so the next retrain over-samples recently corrected examples, anchoring the model against the specific failure modes analysts have identified in production. This closes the human-in-the-loop gap in LedgerLens's continuous retraining architecture.

## Background & Context

LedgerLens produces risk scores that are surfaced to analysts through the REST API and dashboard. When an analyst determines that a flagged wallet is a false positive (e.g., a legitimate market-maker) or that a low-scoring wallet is genuinely washing trades, that domain knowledge is currently lost — there is no mechanism to record the correction or feed it back into subsequent retraining cycles.

Active learning addresses this by treating analyst corrections as high-value labelled examples. Importance weighting (also called instance weighting) ensures the model pays proportionally more attention to examples that were previously mislabelled during training. The weighting scheme used here follows a recency-decay model: corrections made recently receive higher weight, and weight decays exponentially over time to prevent stale corrections from dominating future training distributions.

The existing continuous retraining pipeline (`cli.py retrain-check`, `detection/model_training.py`) trains from scratch on synthetic data. This issue adds a second data source — the analyst correction store — that is merged with the synthetic training set at retrain time, with corrections receiving sample weights that reflect both recency and analyst confidence.

The `detection/feedback_store.py` module may already exist as a stub; if not, it should be created from scratch. The SQLite schema must be additive and schema-migration safe (see `cli.py db-migrate`).

## Objectives

- [ ] Define a `FeedbackRecord` dataclass: `id`, `wallet`, `asset_pair`, `analyst_label` (0=clean, 1=wash), `original_score`, `confidence` (analyst-supplied 0–1), `created_at`, `importance_weight` (float, computed at insert time).
- [ ] Implement `FeedbackStore.add_correction(wallet, asset_pair, analyst_label, original_score, confidence)` that persists a record and computes `importance_weight = confidence * recency_factor` where `recency_factor = 1.0` at insertion.
- [ ] Implement `FeedbackStore.get_weighted_corrections(since_days=90)` returning a list of `(feature_vector, label, weight)` tuples for all corrections within the window.
- [ ] Implement recency decay: when `get_weighted_corrections` is called, recompute `recency_factor = exp(-λ * days_since_correction)` with `λ=0.05` (configurable via `FEEDBACK_DECAY_LAMBDA`). Multiply by stored `confidence` to yield final weight.
- [ ] Extend `detection/model_training.py` to call `FeedbackStore.get_weighted_corrections()` at the start of each retrain, merge corrections with the synthetic training set using `sample_weight` parameter in `sklearn`/`XGBoost`/`LightGBM` `fit()` calls.
- [ ] Add `POST /feedback` endpoint to `api/main.py` accepting `{wallet, asset_pair, analyst_label, confidence}` and persisting via `FeedbackStore`.
- [ ] Add `GET /feedback` endpoint returning paginated correction history (most recent first), admin-key gated.
- [ ] Implement SQLite migration for the `analyst_feedback` table via `cli.py db-migrate`.
- [ ] Ensure corrections referencing wallets with no existing feature vector are accepted but flagged with `has_feature_vector=False`; they are excluded from training but retained for auditing.
- [ ] Write tests covering weight computation, recency decay, merge logic, and API endpoints.

## Technical Requirements

### `FeedbackRecord` schema

```python
@dataclass
class FeedbackRecord:
    id: Optional[int]           # SQLite ROWID, None before insert
    wallet: str
    asset_pair: str
    analyst_label: int          # 0 = clean, 1 = wash
    original_score: int         # 0-100, the score at time of correction
    confidence: float           # analyst-supplied confidence [0.0, 1.0]
    importance_weight: float    # confidence * recency_factor at query time
    has_feature_vector: bool    # True if feature vector exists in feature store
    created_at: datetime
```

### SQLite schema (`cli.py db-migrate`)

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
CREATE INDEX IF NOT EXISTS idx_feedback_wallet ON analyst_feedback(wallet);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON analyst_feedback(created_at);
```

### `FeedbackStore` interface

```python
class FeedbackStore:
    def __init__(self, db_path: str, feature_store: "FeatureStore"):
        ...

    def add_correction(
        self,
        wallet: str,
        asset_pair: str,
        analyst_label: int,
        original_score: int,
        confidence: float = 1.0,
    ) -> FeedbackRecord:
        """Persist a correction. Checks feature store for existing vector."""
        ...

    def get_weighted_corrections(
        self,
        since_days: int = 90,
        decay_lambda: float = 0.05,
    ) -> list[tuple[dict[str, float], int, float]]:
        """
        Returns [(feature_vector, label, weight), ...] for corrections
        where has_feature_vector=True and created_at >= now - since_days.
        weight = confidence * exp(-decay_lambda * days_elapsed)
        """
        ...

    def correction_count(self) -> int:
        """Total number of persisted corrections."""
        ...
```

### Training integration (`detection/model_training.py`)

```python
def build_training_dataset(
    synthetic_df: pd.DataFrame,
    feedback_store: FeedbackStore,
    feedback_weight_multiplier: float = 5.0,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """
    Merge synthetic data with analyst corrections.
    Synthetic samples receive weight=1.0.
    Correction samples receive weight = feedback_weight_multiplier * importance_weight.
    Returns (X, y, sample_weights).
    """
    corrections = feedback_store.get_weighted_corrections()
    if not corrections:
        X = synthetic_df[FEATURE_NAMES]
        y = synthetic_df["label"].values
        w = np.ones(len(y))
        return X, y, w
    # merge and return
    ...
```

All three model `fit()` calls (`RandomForestClassifier`, `XGBClassifier`, `LGBMClassifier`) must pass `sample_weight=w` to honour the importance weights.

### API endpoints (`api/main.py`)

```python
@router.post("/feedback", status_code=201, response_model=FeedbackRecordOut)
async def submit_feedback(payload: FeedbackSubmission, ...): ...

@router.get("/feedback", response_model=PaginatedFeedback)
async def list_feedback(page: int = 1, page_size: int = 50, ...): ...
```

`FeedbackSubmission`:
```python
class FeedbackSubmission(BaseModel):
    wallet: str = Field(..., pattern=r"^G[A-Z2-7]{55}$")
    asset_pair: str
    analyst_label: int = Field(..., ge=0, le=1)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
```

## Security Considerations

- `wallet` must be validated against the Stellar G-address regex (`^G[A-Z2-7]{55}$`) before storage to prevent SQLite injection via malformed wallet strings. Use the pydantic `pattern` validator.
- `POST /feedback` should be gated behind `LEDGERLENS_ADMIN_API_KEY` — analyst corrections influence training data and must not be writable by unauthenticated parties.
- `GET /feedback` similarly requires the admin key; it exposes analyst correction history which may reveal internal fraud investigation priorities.
- The `confidence` field must be bounded [0, 1] server-side even if the client sends out-of-range values; reject with HTTP 422 rather than clamping silently (fail loudly so integrators notice misconfiguration).
- Store no PII in the feedback table. `wallet` is a pseudonymous public key, which is acceptable.
- Analyst-label poisoning: an attacker with API access could flood the feedback store with false corrections to degrade model quality. Rate-limit `POST /feedback` to 100 corrections per hour per IP.

## Testing Requirements

- **Unit — `add_correction()`**: assert record is persisted to SQLite with correct fields; assert `has_feature_vector` flag is set based on feature store lookup.
- **Unit — `get_weighted_corrections()`**: insert corrections at mock timestamps 0, 30, 60 days ago; assert weights decrease monotonically; assert total sum of weights is finite and positive.
- **Unit — decay parameter**: with `decay_lambda=0.0`, all recency factors should equal 1.0.
- **Unit — corrections without feature vectors**: assert such records appear in `GET /feedback` but are excluded from `get_weighted_corrections()` results.
- **Unit — `build_training_dataset()`**: assert `len(X) == len(synthetic) + len(corrections_with_vectors)`; assert correction sample weights are `feedback_weight_multiplier * importance_weight`.
- **Integration — `POST /feedback` 201**: valid payload returns 201 with `FeedbackRecordOut`.
- **Integration — `POST /feedback` 401**: missing admin key returns 401.
- **Integration — `POST /feedback` 422**: invalid wallet or out-of-range label returns 422.
- **Integration — `GET /feedback` pagination**: insert 60 corrections; assert page 1 returns 50, page 2 returns 10.
- **End-to-end — retrain with corrections**: generate synthetic data + 10 corrections; run `build_training_dataset`; assert returned `sample_weights` array has length `n_synthetic + 10` and corrections have weight > 1.0.

## Documentation Requirements

- Docstrings on all public methods of `FeedbackStore` explaining the weighting formula and parameters.
- Update `README.md` CLI Reference section: document that `retrain-check` now merges analyst corrections when available.
- New file `docs/active_learning.md` covering: feedback workflow, weight formula derivation, decay constant guidance, and poisoning risk mitigation.
- `POST /feedback` and `GET /feedback` added to the API endpoint table in README.
- `CHANGELOG.md` entry under `## Unreleased`.
- Migration instructions for the `analyst_feedback` table in `docs/active_learning.md`.

## Definition of Done

- [ ] `FeedbackRecord` dataclass and `FeedbackStore` class implemented in `detection/feedback_store.py`.
- [ ] SQLite `analyst_feedback` table created via `cli.py db-migrate` migration.
- [ ] `add_correction()` persists records with correct `has_feature_vector` flag.
- [ ] `get_weighted_corrections()` applies recency decay correctly; verified by unit tests.
- [ ] `build_training_dataset()` in `model_training.py` merges synthetic + correction data with correct sample weights.
- [ ] All three model `fit()` calls pass `sample_weight`.
- [ ] `POST /feedback` and `GET /feedback` endpoints operational with admin-key auth.
- [ ] Rate limiting applied to `POST /feedback`.
- [ ] All unit and integration tests pass; ≥90% branch coverage on `feedback_store.py`.
- [ ] `docs/active_learning.md` written and complete.
- [ ] `README.md` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience building active learning or human-in-the-loop labelling pipelines, ideally in fraud detection or anomaly detection contexts. You understand importance-weighted training for ensemble classifiers (XGBoost/LightGBM `sample_weight` semantics) and are comfortable working across the full stack — SQLite schema design, Python backend, and FastAPI endpoints. Familiarity with exponential decay weighting schemes or temporal reweighting in ML pipelines is particularly valuable.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., active learning, ML pipelines, Python backend, data engineering).
2. **Relevant experience**: specific projects involving human-in-the-loop labelling, importance weighting, or continuous retraining; links welcome.
3. **Approach / thoughts**: how you would handle the edge case of corrections without feature vectors, and your thoughts on the decay constant λ=0.05 — is it appropriate for SDEX trading patterns?
4. **Estimated time**: your realistic estimate to deliver implementation, tests, and documentation to the Definition of Done standard.
