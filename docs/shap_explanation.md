# SHAP Explanation

LedgerLens provides per-wallet SHAP (SHapley Additive exPlanations) waterfall
explanations through the `GET /v1/scores/{wallet}/explain` endpoint, making
risk scores actionable and auditable for analysts, protocol teams, and
compliance integrators.

## Overview

The SHAP explanation endpoint returns:

- **base_value** — the expected model output (SHAP expected value)
- **contributions** — per-feature SHAP contributions sorted by absolute magnitude descending, each with a 1-indexed rank
- **summary_sentence** — a human-readable sentence naming the top-3 features with their signed SHAP values and direction (increasing/decreasing risk)
- **model_version** — the version of the model used for the explanation
- **model_name** — display name of the model (e.g. "Random Forest")

## Supported Models

The endpoint supports three tree-based models:

| Model          | Query Parameter       |
|----------------|----------------------|
| Random Forest  | `model=random_forest` |
| XGBoost        | `model=xgboost`       |
| LightGBM       | `model=lightgbm`      |

Invalid model names return HTTP 422.

## Endpoint

```
GET /v1/scores/{wallet}/explain?asset_pair=XLM/USDC&model=random_forest
```

### Example Request

```bash
curl -H "X-LedgerLens-Admin-Key: your-key" \
  "http://localhost:8000/v1/scores/GABCD...XYZ/explain?asset_pair=XLM/USDC&model=random_forest"
```

### Example Response

```json
{
  "wallet": "GABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWX",
  "model_version": "test0001",
  "model_name": "Random Forest",
  "base_value": 0.35,
  "contributions": [
    {"feature": "wash_ring_membership", "shap_value": 0.42, "rank": 1},
    {"feature": "round_trip_trade_frequency", "shap_value": 0.31, "rank": 2},
    {"feature": "network_centrality", "shap_value": -0.18, "rank": 3}
  ],
  "summary_sentence": "Random Forest risk score is driven primarily by wash_ring_membership (+0.42, increasing), round_trip_trade_frequency (+0.31, increasing), and network_centrality (-0.18, decreasing)."
}
```

## SHAP Caching

The `ShapExplainer` class implements an in-memory TTL-based cache keyed on
`(wallet, model_version)`. Each cached entry stores the serialised
`ShapExplanation` plus a monotonic timestamp.

### Cache TTL

The default TTL is **3600 seconds (1 hour)**. After the TTL expires, the
next `explain()` call recomputes the SHAP values from the underlying
`TreeExplainer`.

### Cache Invalidation

- **Automatic expiry**: Entries older than the TTL are evicted on read.
- **Model version change**: When a new model version is deployed, the
  cache key changes automatically because the `model_version` string
  (read from `{model}_latest.txt`) is part of the key.
- **No manual flush**: There is no manual cache flush API. To force
  recomputation before the TTL expires, restart the API process.

### Invalidation Strategy

The cache is per-process and lives in memory only. It does not survive
process restarts and is not shared across API instances. This design
is appropriate for a single-process local API (`api/main.py`). The
canonical `ledgerlens-api` service may implement a shared Redis-backed
cache when multi-replica deployments are supported.

## Error Responses

| Status | Condition |
|--------|-----------|
| 200    | Explanation computed successfully |
| 404    | No feature vector or scores found for the wallet |
| 422    | Invalid `model` parameter or non-finite feature values |
| 503    | Models not loaded at startup |
