# Changelog — LedgerLens Go SDK

All notable changes to the LedgerLens Go SDK (`github.com/Ledger-Lenz/Ledgerlens-core/go`)
are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this module adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are tagged `go/vX.Y.Z` (the `go/` prefix is required for Go submodule
tags) and installed with `go get github.com/Ledger-Lenz/Ledgerlens-core/go@go/vX.Y.Z`.

This changelog is scoped to the `go/` directory only. Changes to the wider
`ledgerlens-core` repository are tracked in the [root CHANGELOG](../CHANGELOG.md).

## [Unreleased]

Nothing yet.

## [0.1.0] — Unreleased

Initial release of the Go SDK. Not yet tagged; the items below reconstruct the
module's history from `git log -- go/` (introduced in
[#340](https://github.com/Ledger-Lenz/Ledgerlens-core/pull/340)) and will ship
as `go/v0.1.0`.

### Added

- `Client` — context-aware HTTP client for the LedgerLens REST API, constructed
  with `NewClient(baseURL string, opts ...Option)`.
- API methods, each taking a `context.Context`:
  - `Health` — `GET /health`
  - `GetScore` — `GET /scores/{wallet}`
  - `GetScores` — `GET /scores` (optional `asset_pair` filter)
  - `ExplainScore` — `GET /scores/{wallet}/explain` (SHAP contributions)
  - `GetRings` — `GET /rings`
  - `RegisterWebhook` — `POST /webhooks`
  - `ListWebhooks` — `GET /webhooks`
  - `DeleteWebhook` — `DELETE /webhooks/{subscriberID}`
- Functional options: `WithAPIKey`, `WithHTTPClient`, `WithTimeout`,
  `WithInsecureSkipVerify` (test servers only).
- Typed error handling via `LedgerLensAPIError` (exposes `StatusCode`, `Detail`,
  and a parsed `RetryAfter` on HTTP 429).
- Webhook verification helpers: `VerifyWebhookSignature` (constant-time
  HMAC-SHA256, matching the Python `hmac.compare_digest` reference),
  `VerifyWebhookTimestamp`, and the `DefaultWebhookMaxAge` constant (5 minutes).
- API key redaction: the key never appears in `String()`, `GoString()`, logs,
  or error messages.
- Response model types: `RiskScore` (including the v2+ conformal-prediction
  fields), `WalletScoresResponse`, `CrossChainLink`, `ShapContribution`, `Ring`,
  `HealthStatus`, `WebhookSubscriber`, `WebhookRegisterRequest`,
  `WebhookCreated`.
- Package documentation (`doc.go`).

[Unreleased]: https://github.com/Ledger-Lenz/Ledgerlens-core/commits/main/go
[0.1.0]: https://github.com/Ledger-Lenz/Ledgerlens-core/commits/main/go
