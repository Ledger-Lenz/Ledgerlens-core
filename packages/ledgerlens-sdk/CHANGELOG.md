# Changelog

All notable changes to `ledgerlens-sdk` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No unreleased changes yet._

## [0.1.0] — 2026-06-24

Initial release of the standalone Python SDK for the LedgerLens wash-trading
detection API. Depends only on `httpx` and `pydantic`; does **not** require
the `ledgerlens-core` detection engine.

### Added

- **`LedgerLensClient`** — synchronous client backed by `httpx.Client`.
- **`AsyncLedgerLensClient`** — asynchronous client backed by
  `httpx.AsyncClient`; safe for use with `asyncio.gather`.
- **Typed Pydantic v2 response models** for every covered endpoint
  (`RiskScore`, `ScoreExplanation`, `Alert`, `Ring`, `Webhook`,
  `Dispute`, …).
- **`LedgerLensAPIError`** — raised on every non-2xx HTTP response,
  carrying the parsed `status_code` and `detail` from the API body.
- **Endpoint coverage** (primary read surface + most common write operations):
  - `health()`
  - `list_scores(...)`, `get_score(wallet)`, `explain_score(wallet,
    asset_pair)`, `get_counterfactual(wallet, asset_pair, ...)`
  - `list_alerts(...)`, `asset_risk_ranking()`, `list_rings()`,
    `list_correlations()`, `pool_risk(pool_id)`,
    `circular_path_payments(...)`
  - `create_webhook(...)`, `list_webhooks()`,
    `delete_webhook(subscriber_id)`
  - `create_dispute(...)`, `get_dispute(dispute_id)`
  - `submit_feedback(...)` (admin-key gated)
- **`api_key` constructor parameter** — sent as
  `X-LedgerLens-Admin-Key` on every request; harmless on public
  endpoints, required for admin-gated ones (`submit_feedback`).
- **Bring-your-own-client support** — both constructors accept an
  optional pre-configured `httpx.Client` / `httpx.AsyncClient`
  (useful for custom timeouts, proxies, or test mocks).
- **`pytest`-based test suite** using `httpx.MockTransport` for unit
  tests (no network required) and an `http.server`-based integration
  test for the sync client.
- **`pyproject.toml`** with `[build-system]` (hatchling), `[project]`,
  and `[project.optional-dependencies]` sections; publishable to PyPI
  as `ledgerlens-sdk` via `python -m build && twine upload dist/*`.

[Unreleased]: https://github.com/Ledger-Lenz/Ledgerlens-core/compare/sdk-v0.1.0...HEAD
[0.1.0]: https://github.com/Ledger-Lenz/Ledgerlens-core/releases/tag/sdk-v0.1.0
