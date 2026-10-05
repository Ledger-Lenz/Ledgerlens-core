# Changelog — LedgerLens Go SDK

All notable changes to the LedgerLens Go SDK (`github.com/Ledger-Lenz/Ledgerlens-core/go`)
are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this module adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are tagged `go/vX.Y.Z` (the `go/` prefix is required for Go submodule
tags) and installed with `go get github.com/Ledger-Lenz/Ledgerlens-core/go@go/vX.Y.Z`.

This changelog is scoped to the `go/` directory only. Changes to the wider
`ledgerlens-core` repository are tracked in the [root CHANGELOG](../CHANGELOG.md).

## [0.2.0](https://github.com/Ledger-Lenz/Ledgerlens-core/compare/go/v0.1.0...go/v0.2.0) (2026-10-05)


### Features

* SDK conformance suite, TS WebSocket reconnect, Go webhook verify, proto breaking-change CI ([#1052](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1052)) ([e5ac40c](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/e5ac40c3bbbda5a2a6b32d849ea967ac1deca139)), closes [#987](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/987) [#988](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/988) [#989](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/989) [#990](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/990)
* SDK resilience, generated pagination types, semver contract, shard rebalancing ([#1071](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/1071)) ([56ade25](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/56ade25d9e5c4660de5010b3ad1fe5c12536f714)), closes [#983](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/983) [#984](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/984) [#985](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/985) [#986](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/986)


### Documentation

* **go:** document minimum supported Go version ([056f02e](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/056f02eeb8387694681119898d23c28bc3d0108c))
* **go:** document minimum supported Go version ([8434e55](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/8434e5503aa54a22766237ee24b72b453246491f))


### Code Refactoring

* **sdk:** dedupe reqwest client builder; docs(go): document minimum Go version ([6545377](https://github.com/Ledger-Lenz/Ledgerlens-core/commit/65453774dfbaadb870e79737deaa2ffd9c43cfa6)), closes [#784](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/784) [#783](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/783)

## [Unreleased]

### Added

- `WithRetryPolicy` / `RetryPolicy`: opt-in retry with exponential backoff and
  full jitter for idempotent requests (GET, HEAD, DELETE) on transport errors
  and HTTP 429/5xx. Context cancellation aborts pending backoff.

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
