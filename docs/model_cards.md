# Model Cards for LedgerLens

This document covers the model card generation feature, which produces human‑readable, auditable model cards following the [Model Cards for Model Reporting (Mitchell et al.)](https://arxiv.org/abs/1810.03993) pattern, plus a Datasheet for Datasets section.

## Overview

Model cards are automatically generated when a new model version is promoted via `cli.py retrain-check`, or can be generated on‑demand with `cli.py generate-model-card`. Each model card includes:
* Model Details (name, version, training date, metrics)
* Intended Use and Out of Scope Use cases
* Performance Metrics (AUC‑ROC, PR‑AUC, F1)
* Top Features (by SHAP importance)
* Stability vs previous model version (if available)
* Datasheet for the training dataset (source, samples, features, class balance)
* Known Limitations

## Configuration

Model cards are configured in `config/settings.py` / your environment variables:
| Setting | Default | Description |
|---------|---------|-------------|
| `model_card_dir` | `./model_cards` | Directory to store generated model cards |
| `model_card_auto_generate` | `True` | Auto‑generate model cards when models are promoted |
| `model_card_pdf_enabled` | `False` | Enable PDF rendering (requires `weasyprint`) |

## Usage

### Auto‑Generation

When running `cli.py retrain-check`, if a new model version is promoted, model cards are automatically generated for all promoted models and stored in `model_card_dir`.

### On‑Demand Generation

To generate a model card for an existing model version:

```bash
python -m cli generate-model-card --model random_forest --version v123abc
```

### Admin API

Admin endpoints are available to fetch model cards:
* `GET /admin/model-cards/{model_name}/{version}` - returns model card metadata as JSON
* `GET /admin/model-cards/{model_name}/{version}/markdown` - returns raw Markdown
* `GET /admin/model-cards/{model_name}/{version}/pdf` - returns PDF (if enabled)

These endpoints require admin authentication (like `/admin/drift-reports`).

## Extensibility

The model card generation logic is in `detection/model_card.py`. Extend the `ModelCard` and `DatasheetSection` dataclasses to add new fields as needed.
