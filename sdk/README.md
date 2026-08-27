# @ledgerlens/sdk

TypeScript client for the [LedgerLens](https://github.com/Ledger-Lenz/Ledgerlens-core)
wash-trading detection API, with full type inference and
[Zod](https://zod.dev) runtime validation of every response.

Covers the same REST surface as the [Python](../packages/ledgerlens-sdk),
[Go](../go), and [Rust](../crates/ledgerlens-sdk) SDKs. Runs in Node.js
(≥ 18) and the browser; ships a dual ESM + CommonJS build with type
declarations.

## Installation

```bash
npm install @ledgerlens/sdk
# or: pnpm add @ledgerlens/sdk / yarn add @ledgerlens/sdk
```

> Not yet published to npm — see [Publishing](#publishing). Until then, consume
> it from a local checkout (`npm install /path/to/Ledgerlens-core/sdk`) or from
> a git dependency.

The only runtime dependency is `zod`.

## Usage

```ts
import { LedgerLensClient, LedgerLensError } from "@ledgerlens/sdk";

const client = new LedgerLensClient({
  baseUrl: "http://localhost:8000", // default; point at ledgerlens-api in prod
  // adminKey / complianceKey are optional, for gated endpoints:
  // adminKey: process.env.LEDGERLENS_ADMIN_KEY,
  timeout: 15_000, // ms, default 30_000
});

try {
  const health = await client.getHealth();
  //    ^? { status: string; db: string; models: string }

  const scores = await client.getScores({ limit: 5, sort_by: "score", order: "desc" });
  for (const s of scores) {
    console.log(s.wallet, s.asset_pair, s.score, s.confidence);
  }

  const one = await client.getScore(scores[0]!.wallet);
  console.log(one.benford_flag, one.ml_flag);
} catch (err) {
  if (err instanceof LedgerLensError) {
    // HTTP error, timeout, or response-validation failure
    console.error(err.statusCode, err.message, err.zodIssues);
  } else {
    throw err;
  }
}
```

A complete, runnable version is in
[`examples/basic-usage.ts`](examples/basic-usage.ts):

```bash
cd sdk
npm install
LEDGERLENS_BASE_URL=http://localhost:8000 npm run example
```

### Available methods

Every method returns data validated against a Zod schema; unknown response
fields are stripped.

| Method | Endpoint |
|--------|----------|
| `getHealth()` | `GET /health` |
| `getScores(params?)` | `GET /scores` |
| `getScore(wallet)` | `GET /score/{wallet}` |
| `getAlerts(params?)` | `GET /alerts` |
| `getLiquidityPoolTrades(wallet)` | `GET /liquidity-pool-trades/{wallet}` |
| `getAssetRiskRankings()` | `GET /assets/risk-ranking` |
| `getRings(params?)` | `GET /rings` |
| `getCorrelations()` | `GET /correlations` |
| `getCounterfactual(wallet)` | `GET /score/{wallet}/counterfactual` |
| `getWebhookSubscribers()` | `GET /admin/webhook/subscribers` |
| `getDriftReports()` | `GET /admin/drift` |

`params` for `getScores` accepts `{ wallet?, limit?, offset?, sort_by?, order? }`;
`getAlerts` accepts `{ alert_type?, wallet?, limit?, offset? }`; `getRings`
accepts `{ limit?, offset? }`.

### Error handling

All failures throw `LedgerLensError`:

- **HTTP errors** — `statusCode` is set; `message` is the API's `detail` string
  when present, otherwise `HTTP <status>`.
- **Timeouts** — `message` is `Request timed out after <ms>ms: <url>`.
- **Response validation failures** — `zodIssues` holds the `ZodIssue[]`.

Network errors from `fetch` (e.g. connection refused) propagate unchanged.

### Schemas and types

The Zod schemas and their inferred types are exported for custom validation:

```ts
import { RiskScoreSchema, type RiskScore } from "@ledgerlens/sdk";

const parsed = RiskScoreSchema.parse(payload);
```

Exported schemas: `StellarAddressSchema`, `RiskScoreSchema`, `AlertSchema`,
`AlertTypeSchema`, `LiquidityPoolTradeSchema`, `AssetRiskRankingSchema`,
`RingSchema`, `PairCorrelationSchema`, `CounterfactualSchema`,
`WebhookSubscriberSchema`, `HealthSchema`, `PaginatedScoresSchema`,
`ApiErrorSchema` (each with a matching exported type).

## Development

All commands run from `sdk/`.

```bash
npm install          # install dependencies
npm test             # run the vitest suite once (tests/)
npm run test:watch   # vitest in watch mode
npm run typecheck    # tsc --noEmit — type-check only
npm run build        # dual CJS + ESM + types build → dist/
```

> There is no real linter yet; `npm run typecheck` only checks types. ESLint +
> Prettier setup is tracked in
> [#774](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/774).

### Build targets

`npm run build` runs three `tsc` passes, each with its own config extending the
strict base `tsconfig.json`:

| Config | Output | Feeds `package.json` field |
|--------|--------|----------------------------|
| `tsconfig.esm.json` | `dist/esm/` | `module`, `exports.import` |
| `tsconfig.cjs.json` | `dist/cjs/` | `main`, `exports.require` |
| `tsconfig.types.json` | `dist/types/` | `types` |

### Tests

Tests live in [`tests/`](tests/) and run under [vitest](https://vitest.dev).
They mock `globalThis.fetch`, so no live API is required. `tests/` is excluded
from the build configs.

## Publishing

`package.json` is configured for publication to npm as `@ledgerlens/sdk`
(`publishConfig.access: "public"`), and `prepublishOnly` runs
`npm run build && npm run test`.

**TBD — needs investigation:** there is currently no npm-publish GitHub Actions
workflow and no documented release process. The actual `npm publish` (version
bump, tag, npm credentials) is a maintainer release action.

## License

MIT
