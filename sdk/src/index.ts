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

export { LedgerLensClient, LedgerLensError } from "./client";
export type { LedgerLensClientOptions } from "./client";

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

// Types
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
