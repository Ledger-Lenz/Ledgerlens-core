# @ledgerlens/sdk

TypeScript SDK for the [LedgerLens](https://github.com/Ledger-Lenz/Ledgerlens-core) fraud-detection API.

Covers the same REST surface as the [Go SDK](../go/README.md), the [Python SDK](../packages/ledgerlens-sdk/README.md), and the [Rust SDK](../crates/ledgerlens-sdk/README.md), with full TypeScript inference and Zod runtime response validation.

- **Full TypeScript types** — every response field is statically inferred; no `any` casts required.
- **Zod runtime validation** — responses are validated on arrival; unknown fields are silently stripped so future API additions never break existing consumers.
- **Browser + Node.js** — dual ESM / CJS build; uses the native `fetch` API (Node 18+, all modern browsers).
- **Configurable timeout and auth headers** — pass `adminKey`, `complianceKey`, or a custom `fetchInit` to tailor every request.
- **Single dependency** — only `zod` at runtime; no axios, no polyfills.

## Installation

```bash
npm install @ledgerlens/sdk
# or
yarn add @ledgerlens/sdk
# or
pnpm add @ledgerlens/sdk
```

Requires Node.js ≥ 18 (for native `fetch`). Works in any modern browser without bundler configuration.

## Quick Start

```ts
import { LedgerLensClient } from "@ledgerlens/sdk";

const client = new LedgerLensClient({
  baseUrl: "http://localhost:8000", // your LedgerLens API instance
});

// Check API health
const health = await client.getHealth();
console.log(health.status); // "ok"

// Query risk scores
const scores = await client.getScores({ limit: 20 });
for (const s of scores) {
  console.log(`${s.wallet}  ${s.asset_pair}  score=${s.score}`);
}

// Look up a specific wallet
const score = await client.getScore("GABCDEF...");
if (score.score >= 70) {
  console.warn("High-risk wallet detected:", score.wallet);
}
```

## Authentication

Admin and compliance endpoints require API keys passed as HTTP headers. Supply them at construction time:

```ts
const client = new LedgerLensClient({
  baseUrl: "https://api.ledgerlens.io",
  adminKey: process.env.LEDGERLENS_ADMIN_KEY,       // X-LedgerLens-Admin-Key
  complianceKey: process.env.LEDGERLENS_COMPLIANCE_KEY, // X-LedgerLens-Compliance-Key
});
```

Standard user endpoints (scores, alerts, rings) do not require a key unless your deployment enforces one via the API gateway.

## API Reference

### `new LedgerLensClient(options?)`

| Option          | Type          | Default                    | Description                                     |
|-----------------|---------------|----------------------------|-------------------------------------------------|
| `baseUrl`       | `string`      | `"http://localhost:8000"`  | Base URL of your LedgerLens API instance        |
| `adminKey`      | `string`      | —                          | Admin API key (`X-LedgerLens-Admin-Key` header) |
| `complianceKey` | `string`      | —                          | Compliance key (`X-LedgerLens-Compliance-Key`)  |
| `timeout`       | `number`      | `30000`                    | Request timeout in milliseconds                 |
| `fetchInit`     | `RequestInit` | —                          | Extra options merged into every `fetch` call    |

### Health

```ts
client.getHealth(): Promise<Health>
```

Returns `{ status, db, models }`. Performs live checks on the database connection and model files — see the [health endpoint docs](../docs/api_reference.md).

### Scores

```ts
// List scores (paginated, sortable)
client.getScores(params?: {
  wallet?: string;
  limit?: number;
  offset?: number;
  sort_by?: string;
  order?: "asc" | "desc";
}): Promise<RiskScore[]>

// Single wallet lookup
client.getScore(wallet: string): Promise<RiskScore>
```

`RiskScore` fields:

| Field                | Type             | Description                                          |
|----------------------|------------------|------------------------------------------------------|
| `wallet`             | `string`         | Stellar wallet address                               |
| `asset_pair`         | `string`         | Trading pair, e.g. `"XLM/USDC"`                     |
| `score`             | `number` 0–100   | Composite risk score; higher = more suspicious       |
| `benford_flag`       | `boolean`        | Benford's Law anomaly detected                       |
| `ml_flag`            | `boolean`        | ML ensemble flagged this wallet                      |
| `confidence`         | `number` 0–100   | Model confidence in the score                        |
| `disputed`           | `boolean`        | Score has an open governance dispute                 |
| `timestamp`          | `string`         | ISO 8601 datetime of last update                     |
| `score_lower`        | `number \| null` | Lower bound of 90% conformal prediction interval     |
| `score_upper`        | `number \| null` | Upper bound of 90% conformal prediction interval     |

### Alerts

```ts
client.getAlerts(params?: {
  alert_type?: "WASH_TRADING" | "CIRCULAR_ROUTE" | "POOL_MANIPULATION"
              | "SANDWICH_ATTACK" | "PATH_PAYMENT_CYCLE";
  wallet?: string;
  limit?: number;
  offset?: number;
}): Promise<Alert[]>
```

### Asset Risk Rankings

```ts
client.getAssetRiskRankings(): Promise<AssetRiskRanking[]>
```

Returns trading pairs ranked by aggregate risk, including `avg_score`, `max_score`, and `flagged_count`.

### Wash-Trading Rings

```ts
client.getRings(params?: { limit?: number; offset?: number }): Promise<Ring[]>
```

Each `Ring` contains the `accounts` array, `total_volume`, `cycle_volume`, and `timing_tightness` detected by the graph SCC engine.

### Pair Correlations

```ts
client.getCorrelations(): Promise<PairCorrelation[]>
```

### Counterfactual Explanations

```ts
client.getCounterfactual(wallet: string): Promise<Counterfactual>
```

Returns `original_score`, `counterfactual_score`, `changed_features`, and a human-readable `explanation` — useful for showing users what would need to change for a wallet to no longer be flagged.

### Liquidity Pool Trades

```ts
client.getLiquidityPoolTrades(wallet: string): Promise<LiquidityPoolTrade[]>
```

### Admin endpoints

```ts
client.getWebhookSubscribers(): Promise<WebhookSubscriber[]>
client.getDriftReports(): Promise<unknown>
```

Both require `adminKey` to be set.

## Error Handling

All methods throw `LedgerLensError` on non-2xx responses or Zod validation failures:

```ts
import { LedgerLensClient, LedgerLensError } from "@ledgerlens/sdk";

try {
  const score = await client.getScore("G...");
} catch (err) {
  if (err instanceof LedgerLensError) {
    console.error("API error:", err.message);      // human-readable detail from the API
    console.error("HTTP status:", err.statusCode); // e.g. 404, 401, 503
    console.error("Zod issues:", err.zodIssues);   // set when response validation fails
  }
}
```

Network failures (DNS, connection refused, `AbortError`) propagate as standard `Error` or `DOMException` instances so they can be caught separately.

## Using Schemas Directly

All Zod schemas are exported for consumers who want to validate API data independently:

```ts
import { RiskScoreSchema, AlertSchema } from "@ledgerlens/sdk";

// Parse data from a webhook payload, a cache, etc.
const score = RiskScoreSchema.parse(rawData);

// Graceful validation — no throw on failure
const result = RiskScoreSchema.safeParse(rawData);
if (!result.success) {
  console.error(result.error.issues);
}
```

## Building from Source

```bash
# Install dependencies
npm install

# Build all targets (ESM + CJS + type declarations)
npm run build

# Run tests (Vitest)
npm test

# Type-check without emitting
npm run lint
```

The build emits three artefacts under `dist/`:

| Path             | Format | Purpose                        |
|------------------|--------|--------------------------------|
| `dist/esm/`      | ESM    | Modern bundlers and `import`   |
| `dist/cjs/`      | CJS    | Node.js `require`              |
| `dist/types/`    | `.d.ts`| TypeScript declarations        |

## File Structure

```
sdk/
├── src/
│   ├── index.ts       — public re-exports (client + all schemas + types)
│   ├── client.ts      — LedgerLensClient, LedgerLensError, LedgerLensClientOptions
│   └── schemas.ts     — Zod schemas and inferred TypeScript types for every API response
├── tests/
│   └── client.test.ts — Vitest unit tests (fetch-mocked, no network required)
├── package.json       — npm package metadata, build scripts, dual ESM/CJS exports
├── tsconfig.json      — base TypeScript config
├── tsconfig.esm.json  — ESM build
├── tsconfig.cjs.json  — CJS build
└── tsconfig.types.json — declaration-only build
```

## Further Reading

- [LedgerLens API reference](../docs/api_reference.md)
- [OpenAPI spec](../docs/openapi.json)
- [Webhook security model](../docs/webhook_security_model.md) — HMAC verification for webhook deliveries
- [Go SDK](../go/README.md) — idiomatic Go client covering the same API surface
- [Python SDK](../packages/ledgerlens-sdk/README.md) — Python client
- [Rust SDK](../crates/ledgerlens-sdk/README.md) — Rust client

## License

MIT — see [LICENSE](../LICENSE).
