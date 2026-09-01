/**
 * @ledgerlens/sdk — TypeScript SDK for the LedgerLens API
 *
 * Features:
 * - Full TypeScript type inference for all API responses
 * - Zod runtime validation (unknown fields stripped)
 * - Browser + Node.js (ESM + CJS dual build)
 * - Timeout and error handling
 *
 * @example
 * ```ts
 * import { LedgerLensClient } from "@ledgerlens/sdk";
 *
 * const client = new LedgerLensClient({ baseUrl: "http://localhost:8000" });
 * const health = await client.getHealth();
 * console.log(health);
 * ```
 */

/**
 * {@link LedgerLensClient} is the HTTP client; {@link LedgerLensError} is the
 * error type every client method rejects with.
 */
export { LedgerLensClient, LedgerLensError } from "./client";
/** Constructor options for {@link LedgerLensClient}. */
export type { LedgerLensClientOptions } from "./client";

/**
 * Zod schemas backing every API response. Exported so consumers can run their
 * own validation, derive partial schemas, or reuse them in tests. Each
 * `XxxSchema` parses the payload described by the matching `Xxx` type below.
 */
export {
  // Schemas (for custom validation)
  StellarAddressSchema,
  RiskScoreSchema,
  AlertSchema,
  AlertTypeSchema,
  LiquidityPoolTradeSchema,
  AssetRiskRankingSchema,
  RingSchema,
  PairCorrelationSchema,
  CounterfactualSchema,
  WebhookSubscriberSchema,
  HealthSchema,
  PaginatedScoresSchema,
  ApiErrorSchema,
} from "./schemas";

/**
 * Static types inferred from the Zod schemas above, describing the shape of
 * each parsed API response.
 */
export type {
  RiskScore,
  Alert,
  AlertType,
  LiquidityPoolTrade,
  AssetRiskRanking,
  Ring,
  PairCorrelation,
  Counterfactual,
  WebhookSubscriber,
  Health,
  PaginatedScores,
  ApiError,
} from "./schemas";
