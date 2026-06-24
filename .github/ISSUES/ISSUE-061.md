---
title: "Implement SHAP Waterfall Explanation Endpoint for Per-Wallet Risk Score Interpretability"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/shap_explainer.py` to produce waterfall-style SHAP explanation payloads, and expose a new `GET /scores/{wallet}/explain` endpoint in `api/main.py`. The response returns the SHAP base value, per-feature contributions sorted by magnitude, a human-readable summary sentence, and cached SHAP values keyed on `(wallet, model_version)` in the feature store. This makes LedgerLens risk scores actionable and auditable for analysts, protocol teams, and compliance integrators who need to understand *why* a wallet was flagged.

## Background & Context

LedgerLens already integrates SHAP via `detection/shap_explainer.py` for offline interpretability during model evaluation. However, SHAP explanations are not yet surfaced through the REST API, which means operators and downstream consumers see a risk score of, say, 87 with no understanding of which features drove that score. For compliance use cases — and for the dispute resolution workflow defined in `docs/governance_protocol.md` — this opacity is a significant gap.

SHAP (SHapley Additive exPlanations) attributes a model prediction to each input feature as a signed contribution value. For tree-based models (RF, XGBoost, LightGBM), `shap.TreeExplainer` computes exact Shapley values efficiently. The waterfall format — base value + ordered contributions summing to the final prediction — is the standard display format used in the SHAP library's own visualisation tools and is directly parseable by the LedgerLens dashboard.

The existing `detection/shap_explainer.py` must be extended to:
1. Accept a pre-computed feature vector and return a structured `ShapExplanation` dataclass.
2. Cache the result in the feature store to avoid recomputing SHAP values on every API call.
3. Generate a plain-English summary sentence describing the top contributing features.

The new `GET /scores/{wallet}/explain` endpoint should be consistent with the existing `/scores/{wallet}` contract and return HTTP 404 when no score exists for the wallet.

## Objectives

- [ ] Define a `ShapExplanation` dataclass in `detection/shap_explainer.py` with fields: `wallet`, `asset_pair`, `model_version`, `base_value`, `contributions` (list of `FeatureContribution`), `predicted_score`, `summary_sentence`, `computed_at`.
- [ ] Implement `ShapExplainer.explain(wallet, asset_pair, feature_vector) -> ShapExplanation` that calls `shap.TreeExplainer` on the active XGBoost model and returns the structured result.
- [ ] Sort `contributions` by `abs(shap_value)` descending so the most influential features appear first.
- [ ] Generate a human-readable `summary_sentence` of the form: *"Score driven primarily by high round_trip_trade_frequency (+18.3), elevated benford_chi_square_24h (+12.1), and wash_ring_membership (+9.7)."*
- [ ] Cache `ShapExplanation` results in `detection/feature_store.py` under key `shap:(wallet):(asset_pair):(model_version)`, with TTL of 1 hour.
- [ ] Add `GET /scores/{wallet}/explain` to `api/main.py` that retrieves the cached explanation or recomputes it on demand.
- [ ] Return HTTP 404 with `{"detail": "No score found for wallet"}` when the wallet has no `RiskScore` record.
- [ ] Add query parameter `?asset_pair=XLM/USDC` to scope the explanation to a specific trading pair.
- [ ] Ensure the endpoint is covered by the existing admin-key auth when `LEDGERLENS_ADMIN_API_KEY` is set.
- [ ] Write unit and integration tests achieving ≥90% branch coverage on the new code paths.

## Technical Requirements

### `ShapExplanation` schema (`detection/shap_explainer.py`)

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

@dataclass
class FeatureContribution:
    feature_name: str
    feature_value: float
    shap_value: float          # signed; positive = pushes score up
    rank: int                  # 1 = highest |shap_value|

@dataclass
class ShapExplanation:
    wallet: str
    asset_pair: str
    model_version: str
    base_value: float          # E[f(x)] across training set
    contributions: List[FeatureContribution]   # sorted by |shap_value| desc
    predicted_score: float     # base_value + sum(shap_values), clamped 0-100
    summary_sentence: str
    computed_at: datetime
```

### `ShapExplainer.explain()` interface

```python
class ShapExplainer:
    def __init__(self, model_dir: str, feature_store: "FeatureStore"):
        # Load shap.TreeExplainer for each model at construction time
        ...

    def explain(
        self,
        wallet: str,
        asset_pair: str,
        feature_vector: dict[str, float],
        model_name: str = "xgboost",
    ) -> ShapExplanation:
        """
        Compute or retrieve cached SHAP waterfall explanation.
        Cache key: f"shap:{wallet}:{asset_pair}:{model_version}"
        TTL: 3600 seconds
        Raises ValueError if model_name is not one of {random_forest, xgboost, lightgbm}.
        """
        ...

    def _build_summary_sentence(
        self, contributions: List[FeatureContribution], top_n: int = 3
    ) -> str:
        """Return a plain-English sentence listing the top_n drivers."""
        ...
```

### API endpoint (`api/main.py`)

```python
@router.get(
    "/scores/{wallet}/explain",
    response_model=ShapExplanationResponse,
    summary="Return SHAP waterfall explanation for a wallet's most recent risk score",
)
async def get_explanation(
    wallet: str,
    asset_pair: Optional[str] = Query(None, description="Filter by asset pair, e.g. XLM/USDC"),
    model: str = Query("xgboost", description="Model to explain: random_forest | xgboost | lightgbm"),
    db: RiskScoreStore = Depends(get_db),
    explainer: ShapExplainer = Depends(get_explainer),
):
    score = db.get_latest(wallet, asset_pair)
    if score is None:
        raise HTTPException(status_code=404, detail="No score found for wallet")
    explanation = explainer.explain(wallet, score.asset_pair, score.feature_vector, model)
    return ShapExplanationResponse.from_domain(explanation)
```

### Feature store caching (`detection/feature_store.py`)

Add cache methods:
```python
def cache_shap(self, key: str, explanation: ShapExplanation, ttl_seconds: int = 3600) -> None: ...
def get_cached_shap(self, key: str) -> Optional[ShapExplanation]: ...
```

Cache storage must use the existing SQLite-backed feature store; no external dependencies (e.g., Redis) are required at this stage.

### Response model

```python
class FeatureContributionOut(BaseModel):
    feature_name: str
    feature_value: float
    shap_value: float
    rank: int

class ShapExplanationResponse(BaseModel):
    wallet: str
    asset_pair: str
    model_version: str
    base_value: float
    contributions: List[FeatureContributionOut]
    predicted_score: float
    summary_sentence: str
    computed_at: datetime

    class Config:
        json_schema_extra = {
            "example": {
                "wallet": "GABC123",
                "asset_pair": "XLM/USDC",
                "model_version": "xgboost_v12a3b4c5",
                "base_value": 22.4,
                "contributions": [
                    {"feature_name": "round_trip_trade_frequency", "feature_value": 0.83,
                     "shap_value": 18.3, "rank": 1}
                ],
                "predicted_score": 87.0,
                "summary_sentence": "Score driven primarily by high round_trip_trade_frequency (+18.3).",
                "computed_at": "2026-06-24T10:00:00Z"
            }
        }
```

### Performance constraint

SHAP computation for a single 35-feature vector must complete in <200 ms on a single core. Use `shap.TreeExplainer` with `check_additivity=False` to skip the additivity assertion in production paths.

## Security Considerations

- The `summary_sentence` is generated from feature names and numeric values only — never from user-supplied strings — so there is no injection risk.
- `feature_vector` values come from `RiskScore.feature_vector` stored in SQLite; validate that all keys match `FEATURE_NAMES` and that values are finite floats before passing to SHAP. Reject any vector containing NaN, Inf, or unexpected keys with HTTP 422.
- The `/explain` endpoint exposes internal model structure (base values, feature weights). Gate it behind `LEDGERLENS_ADMIN_API_KEY` if the admin key is configured, or document that it is intended for authenticated use only in the deployment guide.
- Cache keys must be constructed without unsanitised user input: use `wallet` values that have been validated as valid Stellar account IDs (56-char G-addresses) before constructing the cache key.
- Do not log raw `feature_vector` contents at INFO level — log only wallet, asset_pair, and model_version to avoid leaking model features into log aggregation systems.

## Testing Requirements

- **Unit — `ShapExplainer.explain()`**: mock `shap.TreeExplainer` output; assert `contributions` are sorted by `|shap_value|` descending; assert `predicted_score ≈ base_value + sum(shap_values)` within 0.01.
- **Unit — `_build_summary_sentence()`**: assert output is a non-empty string; assert top-3 feature names appear in the sentence; assert sign (+/-) is rendered correctly.
- **Unit — cache hit**: call `explain()` twice with same inputs; assert the underlying `TreeExplainer` is called only once (mock confirms single call).
- **Unit — cache miss after TTL**: freeze time past TTL; assert `TreeExplainer` is called again.
- **Integration — API 200**: seed a `RiskScore` with a feature vector; `GET /scores/{wallet}/explain`; assert 200, correct `wallet`, non-empty `contributions`.
- **Integration — API 404**: `GET /scores/GUNKNOWN/explain`; assert 404 with `detail` field.
- **Integration — invalid model name**: `GET /scores/{wallet}/explain?model=catboost`; assert 422.
- **Integration — feature vector validation**: store a `RiskScore` with a NaN feature value; assert API returns 422, not 500.
- All tests in `tests/test_shap_explainer.py` and `tests/test_api_explain.py`.

## Documentation Requirements

- Add docstrings to `ShapExplainer`, `explain()`, and `_build_summary_sentence()` following the existing project style.
- Update `README.md` CLI Reference and API section to document the new endpoint with example `curl` command and sample response.
- Add `GET /scores/{wallet}/explain` to the endpoint table in the Quick Start → Serve section.
- Document the cache TTL and invalidation strategy in a `## SHAP Caching` subsection within `docs/` (new file: `docs/shap_explanation.md`).
- Add an entry to `CHANGELOG.md` under `## Unreleased`.

## Definition of Done

- [ ] `ShapExplanation` and `FeatureContribution` dataclasses defined and importable from `detection/shap_explainer.py`.
- [ ] `ShapExplainer.explain()` returns correct waterfall data for all three models (RF, XGBoost, LightGBM).
- [ ] Contributions sorted by magnitude descending; `rank` field is 1-indexed and contiguous.
- [ ] `summary_sentence` is grammatically correct and names top-3 features with signed SHAP values.
- [ ] Cache stores and retrieves explanations correctly; TTL-based invalidation works.
- [ ] `GET /scores/{wallet}/explain` returns HTTP 200 / 404 / 422 correctly.
- [ ] Endpoint integrated with existing admin-key auth guard.
- [ ] All unit and integration tests pass (`pytest tests/test_shap_explainer.py tests/test_api_explain.py`).
- [ ] ≥90% branch coverage on new modules.
- [ ] `README.md` and `docs/shap_explanation.md` updated.
- [ ] No raw exception text or feature vectors appear in API error responses.
- [ ] `CHANGELOG.md` entry added.

## For Contributors

**Ideal contributor profile**: You have hands-on experience with the SHAP library — specifically `TreeExplainer` for gradient-boosted or random-forest models — and are comfortable extending FastAPI applications with new response models and dependency injection patterns. Familiarity with LedgerLens's 35-feature schema (Benford, trade-pattern, graph, cross-pair) will accelerate the work considerably. Experience with SQLite-backed caching or TTL-aware key-value stores is a plus.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., ML interpretability, FastAPI, Python backend).
2. **Relevant experience**: specific SHAP or interpretability projects you have shipped; links to code or PRs appreciated.
3. **Approach / thoughts**: how you would structure the `explain()` method, handle cache invalidation, and generate the summary sentence — any tradeoffs you foresee.
4. **Estimated time**: your realistic estimate to complete implementation, tests, and documentation to the Definition of Done standard.
