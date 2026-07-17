# LedgerLens Go SDK

Idiomatic Go client for the [LedgerLens](https://github.com/Ledger-Lenz/Ledgerlens-core) fraud-detection REST API.

Covers the same REST surface as the Python SDK (`packages/ledgerlens-sdk`) and the TypeScript SDK (`sdk/`), with context-aware methods, typed error handling, and webhook HMAC verification helpers.

## Installation

```bash
go get github.com/Ledger-Lenz/Ledgerlens-core/go@latest
```

Requires Go 1.22+. The module path is `github.com/Ledger-Lenz/Ledgerlens-core/go`.

## Quick Start

```go
package main

import (
    "context"
    "fmt"
    "log"

    ledgerlens "github.com/Ledger-Lenz/Ledgerlens-core/go"
)

func main() {
    client := ledgerlens.NewClient(
        "https://api.ledgerlens.io",
        ledgerlens.WithAPIKey("your-api-key"),
    )

    ctx := context.Background()
    resp, err := client.GetScore(ctx, "GABCDEF...")
    if err != nil {
        log.Fatal(err)
    }
    for _, s := range resp.Scores {
        fmt.Printf("wallet=%s pair=%s score=%d benford=%v ml=%v\n",
            s.Wallet, s.AssetPair, s.Score, s.BenfordFlag, s.MLFlag)
    }
}
```

## Withdrawal Gating Example

A common exchange-backend pattern: block a withdrawal when the wallet's risk
score is at or above a threshold and the ML classifier has flagged it.

```go
func checkWithdrawalAllowed(ctx context.Context, client *ledgerlens.Client, wallet string) error {
    resp, err := client.GetScore(ctx, wallet)
    if err != nil {
        return fmt.Errorf("ledgerlens score lookup: %w", err)
    }
    for _, s := range resp.Scores {
        if s.Score >= 70 && s.MLFlag {
            return fmt.Errorf("withdrawal blocked: LedgerLens risk score %d for %s/%s",
                s.Score, s.Wallet, s.AssetPair)
        }
    }
    return nil
}
```

## Client Options

| Option | Description |
|--------|-------------|
| `WithAPIKey(key)` | Sets `X-LedgerLens-Admin-Key` on every request |
| `WithHTTPClient(hc)` | Replaces the default `*http.Client` |
| `WithTimeout(d)` | Sets the per-request timeout (default: 30 s) |
| `WithInsecureSkipVerify()` | Disables TLS verification — **test servers only** |

## Methods

```go
// Health
client.Health(ctx) (*HealthStatus, error)

// Scores
client.GetScore(ctx, wallet)                  (*WalletScoresResponse, error)
client.GetScores(ctx, assetPair)              ([]RiskScore, error)
client.ExplainScore(ctx, wallet, assetPair)   ([]ShapContribution, error)

// Rings
client.GetRings(ctx)                          ([]Ring, error)

// Webhooks
client.RegisterWebhook(ctx, req)              (*WebhookCreated, error)
client.ListWebhooks(ctx)                      ([]WebhookSubscriber, error)
client.DeleteWebhook(ctx, subscriberID)       error
```

## Error Handling

All methods return a `*LedgerLensAPIError` on non-2xx responses:

```go
resp, err := client.GetScore(ctx, wallet)
if err != nil {
    var apiErr *ledgerlens.LedgerLensAPIError
    if errors.As(err, &apiErr) {
        switch apiErr.StatusCode {
        case 401:
            log.Fatal("invalid API key")
        case 404:
            log.Printf("wallet not found")
        case 429:
            log.Printf("rate limited, retry after %s", apiErr.RetryAfter)
        }
    }
    return err
}
```

## Webhook Verification

Go-based exchange backends receiving webhook alerts should verify the
`X-LedgerLens-Signature` and `X-LedgerLens-Timestamp` headers on every
delivery. The SDK provides constant-time helpers matching the Python reference
in [docs/webhook_security_model.md](../docs/webhook_security_model.md):

```go
func webhookHandler(w http.ResponseWriter, r *http.Request) {
    body, err := io.ReadAll(r.Body)
    if err != nil {
        http.Error(w, "read error", http.StatusBadRequest)
        return
    }

    sig := r.Header.Get("X-LedgerLens-Signature")
    if !ledgerlens.VerifyWebhookSignature(body, webhookSecret, sig) {
        http.Error(w, "invalid signature", http.StatusUnauthorized)
        return
    }

    ts := r.Header.Get("X-LedgerLens-Timestamp")
    if !ledgerlens.VerifyWebhookTimestamp(ts, ledgerlens.DefaultWebhookMaxAge) {
        http.Error(w, "timestamp too old (replay?)", http.StatusUnauthorized)
        return
    }

    // safe to process the payload
    w.WriteHeader(http.StatusOK)
}
```

`VerifyWebhookSignature` uses `hmac.Equal` (constant-time comparison). Never
compare webhook signatures with `==` or `bytes.Equal` — those are vulnerable to
timing side-channel attacks.

## Struct Reference

### RiskScore

| Field | Type | JSON key | Notes |
|-------|------|----------|-------|
| `Wallet` | `string` | `wallet` | Stellar wallet address |
| `AssetPair` | `string` | `asset_pair` | e.g. `XLM/USDC` |
| `Score` | `int` | `score` | 0–100, higher = more suspicious |
| `BenfordFlag` | `bool` | `benford_flag` | Benford anomaly detected |
| `MLFlag` | `bool` | `ml_flag` | ML classifier flagged |
| `Confidence` | `int` | `confidence` | 0–100 |
| `Disputed` | `bool` | `disputed` | Score is under dispute |
| `Timestamp` | `time.Time` | `timestamp` | Score computation time |
| `ScoreLower` | `*float64` | `score_lower` | Conformal interval lower bound (v2+) |
| `ScoreUpper` | `*float64` | `score_upper` | Conformal interval upper bound (v2+) |
| `PredictionSet` | `[]int` | `prediction_set` | Conformal prediction set (v2+) |
| `CoverageGuarantee` | `*float64` | `coverage_guarantee` | Target coverage level (v2+) |

### Ring

| Field | Type | JSON key |
|-------|------|----------|
| `ID` | `int` | `id` |
| `Accounts` | `[]string` | `accounts` |
| `TotalVolume` | `float64` | `total_volume` |
| `CycleVolume` | `float64` | `cycle_volume` |
| `AvgTradeCount` | `float64` | `avg_trade_count` |
| `TimingTightness` | `float64` | `timing_tightness` |
| `DetectedAt` | `string` | `detected_at` |

## Security

- The client defaults to a TLS-verified `http.Client`. `WithInsecureSkipVerify()` is provided explicitly for local test servers only and is never enabled by default.
- `apiKey` is never included in `String()`, `GoString()`, log output, or error messages.
- `VerifyWebhookSignature` uses `hmac.Equal` (constant-time); the implementation mirrors the Python `hmac.compare_digest` reference exactly.
- `VerifyWebhookTimestamp` rejects future timestamps (`delta < 0`) to guard against clock-skew attacks in addition to replays.

## Running Tests

```bash
cd go/
go test ./...
go vet ./...
```

## Versioning

The module is tagged `go/vX.Y.Z` for `go get`:

```bash
go get github.com/Ledger-Lenz/Ledgerlens-core/go@go/v0.1.0
```
