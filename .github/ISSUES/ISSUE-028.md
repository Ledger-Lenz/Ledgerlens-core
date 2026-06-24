---
title: "Build a SHAP Waterfall and Force Plot Exporter for Per-Score Explanations"
labels: ["difficulty: advanced", "area: interpretability", "type: feature"]
assignees: []
---

## Summary
`detection/shap_explainer.py` computes SHAP values for each risk score but currently does not export human-readable visualisations. The LedgerLens dashboard and API consumers need per-score SHAP waterfall plots (showing how each feature pushes the prediction from the base value to the final score) and force plots (inline HTML showing feature contributions). This issue builds a `SHAPPlotExporter` that produces SVG/PNG waterfall plots and structured JSON summaries suitable for both server-side rendering and dashboard API responses.

## Background & Context
`detection/shap_explainer.py` uses the `shap` library to compute `shap_values` for each wallet/asset-pair score. The SHAP `TreeExplainer` is used for all three tree ensemble models (Random Forest, XGBoost, LightGBM), and the current implementation returns raw `shap.Explanation` objects that are logged but not persisted or exported.

The `ledgerlens-dashboard` needs:
1. **SVG/PNG waterfall plots**: one per score, showing the 10 most influential features, base value, and final predicted probability; these are static images suitable for embedding in dashboards, PDFs, and email alerts
2. **Structured JSON explanations**: `{"feature_name": "...", "shap_value": 0.34, "feature_value": 12.5, "direction": "increases_risk"}` for each of the top-10 features; used by the dashboard's interactive SHAP table and by the `/scores/{wallet}` API response

The SHAP `plots.waterfall()` and `plots.force()` functions exist in `shap >= 0.42` but require careful matplotlib backend configuration for server-side (headless) rendering. The exporter must work without a display server (`matplotlib.use("Agg")` before any plot import).

The JSON output must be stable across `shap` library versions — it must not depend on internal `shap.Explanation` attributes that change between minor versions.

## Objectives
- [ ] Implement `SHAPPlotExporter` class in `detection/shap_explainer.py` with `export_waterfall(explanation, wallet, asset_pair, output_dir) -> Path` returning the path to the generated SVG/PNG file
- [ ] Implement `explanation_to_json(explanation, top_n=10) -> List[Dict]` that extracts the top-N features by absolute SHAP value and returns structured JSON suitable for the API response schema
- [ ] Integrate the exporter into the pipeline: after each `model_inference.py` scoring run, call `export_waterfall()` for all scores above `RISK_SCORE_THRESHOLD` and persist the JSON summary to the `risk_scores` SQLite table (add a `shap_explanation JSON` column)
- [ ] Add `GET /scores/{wallet}/explanation` endpoint to `api/main.py` that returns the stored SHAP JSON for the most recent score, and `GET /scores/{wallet}/explanation/plot` that streams the pre-generated SVG file

## Technical Requirements

**`SHAPPlotExporter` class:**
```python
class SHAPPlotExporter:
    def __init__(self, output_dir: Path, format: str = "svg", dpi: int = 150, max_display: int = 10):
        self.output_dir = output_dir
        self.format = format  # "svg" or "png"
        self.dpi = dpi
        self.max_display = max_display
        output_dir.mkdir(parents=True, exist_ok=True)

    def export_waterfall(
        self,
        explanation: shap.Explanation,  # single-sample, shape (n_features,)
        wallet: str,
        asset_pair: str,
        score: int,
    ) -> Path:
        """Export waterfall plot to {output_dir}/{wallet}_{asset_pair_safe}_{score}.{format}"""
```

**Headless matplotlib configuration:**
```python
import matplotlib
matplotlib.use("Agg")  # must be called before any other matplotlib import
import matplotlib.pyplot as plt
```

- This must be the **first** matplotlib-related code in the module; document it with a comment explaining the headless requirement
- The `Agg` backend produces identical output across Linux/macOS/Windows without a display server

**Waterfall plot specifications:**
- Use `shap.plots.waterfall(explanation, max_display=self.max_display, show=False)` to render to current Axes
- Features sorted by absolute SHAP value descending; "other features" bar aggregates remaining features
- Base value annotation: show `E[f(X)] = {base_value:.3f}` in plot subtitle
- Final value annotation: show `f(x) = {final_value:.3f}` and corresponding risk score `(score: {score}/100)`
- File naming: `{wallet[:8]}_{sanitise_asset_pair(asset_pair)}_{score}.{format}` — truncate wallet to first 8 chars for readability

**`explanation_to_json` output schema:**
```json
[
  {
    "rank": 1,
    "feature_name": "chi2_24h",
    "feature_value": 34.7,
    "shap_value": 0.412,
    "direction": "increases_risk",
    "plain_english": "24-hour Benford chi-square is 34.7 (threshold: 15.5) — strongly abnormal digit distribution"
  },
  ...
]
```
- `direction`: `"increases_risk"` when `shap_value > 0`, `"decreases_risk"` when `shap_value < 0`
- `plain_english`: generate from a feature → human-readable template dict; each of the 35+ features should have a template
- `plain_english` must not expose raw model internals (e.g., "SHAP value = 0.412"); translate to domain language

**API endpoints:**
```
GET /scores/{wallet}/explanation
  → 200: {"wallet": "...", "asset_pair": "...", "score": 85, "top_features": [...], "generated_at": "..."}
  → 404: wallet/score not found
  → 503: SHAP explanation not available (computed on demand, not pre-generated)

GET /scores/{wallet}/explanation/plot
  → 200: SVG file (Content-Type: image/svg+xml) or PNG (Content-Type: image/png)
  → 404: plot not found; generate on demand if score exists
```

**On-demand generation:**
- If the plot file does not exist when `GET /explanation/plot` is called, generate it on-the-fly using the stored SHAP JSON (reconstruct a minimal `shap.Explanation` from JSON) and the stored feature values
- Cache generated plots for 24 hours; clean up files older than 7 days from `output_dir`

**Performance:**
- Single waterfall plot generation: < 500 ms (matplotlib SVG rendering)
- `explanation_to_json()`: < 1 ms (pure Python dict construction)
- Bulk export for 100 high-risk scores: < 60 seconds

## Security Considerations
- `wallet` and `asset_pair` used in file naming must be sanitised (`re.sub(r"[^A-Z0-9_/.-]", "", ...)`) before use in `Path` construction; path traversal via crafted wallet strings must be impossible
- Plot output directory must be outside the web server root or served via a dedicated static file handler — never from a directory that allows directory listing
- SHAP JSON stored in SQLite must not include raw private keys, wallet balances, or other sensitive fields beyond the feature names and values defined in `FEATURE_NAMES`
- The `/explanation/plot` endpoint must rate-limit on-demand generation to 10 requests per minute per IP to prevent DoS via compute-intensive matplotlib rendering

## Testing Requirements
- Unit tests covering:
  - `explanation_to_json()` returns list of length `min(top_n, n_features)` sorted by `|shap_value|` descending
  - `direction` field is `"increases_risk"` for positive SHAP values and `"decreases_risk"` for negative
  - `sanitise_asset_pair()` strips non-alphanumeric characters
  - File naming: `wallet="GABCDEF123...", asset_pair="XLM/USDC"` → filename contains `"GABCDEF1_XLM_USDC"`
- Integration tests covering:
  - `export_waterfall()` creates an SVG file at the expected path on a headless system
  - `GET /scores/{wallet}/explanation` returns 200 with valid JSON when score and SHAP data exist
  - `GET /scores/{wallet}/explanation/plot` returns 200 with `Content-Type: image/svg+xml`
- Edge cases:
  - `explanation` with all zero SHAP values: waterfall shows flat plot, no division by zero
  - `asset_pair` containing `/` → sanitised to `_` in filename
  - Wallet not in database → 404 response
  - On-demand generation triggered by missing plot file → plot created and returned

## Documentation Requirements
- Update `detection/shap_explainer.py` module docstring with `SHAPPlotExporter` usage examples
- Add `SHAP_PLOT_OUTPUT_DIR` and `SHAP_PLOT_FORMAT` to `config/settings.py` and `.env.example`
- Update `api/main.py` API docstrings for new `/explanation` endpoints
- Add a `docs/shap_explanations.md` with examples of waterfall plots and the `plain_english` feature template list

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: SHAP library, matplotlib headless rendering, FastAPI file responses, SVG generation
- Your approach or initial thoughts on the `plain_english` template strategy
- Estimated time to complete

**Ideal contributor profile:** Python ML engineer with hands-on SHAP experience and FastAPI knowledge; experience with headless server-side chart generation (matplotlib Agg backend) is important.
