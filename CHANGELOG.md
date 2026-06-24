# Changelog

All notable changes to `ledgerlens-core` are documented in this file.

## Unreleased

### Added
- Compliance audit report generator (`detection/compliance_report.py`) producing
  self-contained HTML/PDF reports with SHAP attributions, Benford analysis,
  trade timeline, and model provenance.
- `cli.py report generate --wallet G... --date YYYY-MM-DD` command.
- Synthetic SDEX trade generator (`ingestion/synthetic_data.py`) with
  labelled wash-trading rings for local training and testing.
- Labelled training dataset builder (`detection/dataset.py`).
- SQLite-backed local `RiskScore` store (`detection/storage.py`).
- Local read-only FastAPI app (`api/main.py`) serving `/scores`, `/alerts`,
  and `/assets/risk-ranking`.
- `ledgerlens` CLI (`cli.py`): `generate-data`, `train`, `score`, `serve`.
- Retrying HTTP client for Horizon API calls (`ingestion/http_client.py`).
- Dockerfile, docker-compose, and GitHub Actions CI workflow.

### Fixed
- `detection/shap_explainer.py` updated for the current SHAP `TreeExplainer`
  output shape.

## 0.1.0

- Initial scaffold: Horizon ingestion, Benford's Law engine, ML feature
  engineering, ensemble model training/inference, `RiskScore` schema.
