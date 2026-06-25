/**
 * Zod schemas for LedgerLens API response validation.
 *
 * All API responses are validated at runtime using these schemas.
 * Unknown fields are stripped by Zod's `.strip()` behaviour (default).
 * Use `.parse()` for strict validation or `.safeParse()` for graceful
 * error handling on the client side.
 */

import { z } from "zod";

// ---------------------------------------------------------------------------
// Core domain types
// ---------------------------------------------------------------------------

/**
 * A Stellar wallet address: 56 chars, starts with 'G', base32 alphabet.
 */
export const StellarAddressSchema = z
  .string()
  .regex(/^G[A-Z2-7]{55}$/, "Invalid Stellar wallet address");

/**
 * A risk score (0–100) computed by LedgerLens.
 */
export const RiskScoreSchema = z.object({
  wallet: z.string(),
  asset_pair: z.string(),
  score: z.number().int().min(0).max(100),
  benford_flag: z.boolean(),

// ---------------------------------------------------------------------------
// Alert types
// ---------------------------------------------------------------------------

export const AlertTypeSchema = z.enum([
  "WASH_TRADING",
  "CIRCULAR_ROUTE",
  "POOL_MANIPULATION",
  "SANDWICH_ATTACK",
  "PATH_PAYMENT_CYCLE",
]);

export const AlertSchema = z.object({
  id: z.number().int(),
  wallet: z.string(),
  alert_type: AlertTypeSchema,
  severity: z.string(),
  details: z.string(),
  detected_at: z.string().datetime(),
  acknowledged: z.boolean(),
});

export type Alert = z.infer<typeof AlertSchema>;


// ---------------------------------------------------------------------------
// Asset / pool types
// ---------------------------------------------------------------------------

export const LiquidityPoolTradeSchema = z.object({
  wallet: z.string(),
  pool_id: z.string(),
  asset_a: z.string(),
  asset_b: z.string(),
  volume_a: z.number(),
  volume_b: z.number(),
  timestamp: z.string().datetime(),
});

export type LiquidityPoolTrade = z.infer<typeof LiquidityPoolTradeSchema>;

export const AssetRiskRankingSchema = z.object({
  asset_pair: z.string(),
  avg_score: z.number().min(0).max(100),
  max_score: z.number().min(0).max(100),
  flagged_count: z.number().int().nonnegative(),
  total_count: z.number().int().nonnegative(),
  updated_at: z.string().datetime(),
});

export type AssetRiskRanking = z.infer<typeof AssetRiskRankingSchema>;

  ml_flag: z.boolean(),

// ---------------------------------------------------------------------------
// Wash-trading ring
// ---------------------------------------------------------------------------

export const RingSchema = z.object({
  id: z.number().int(),
  accounts: z.array(z.string()),
  total_volume: z.number(),
  cycle_volume: z.number(),
  avg_trade_count: z.number(),
  timing_tightness: z.number(),
  detected_at: z.string().datetime(),
});

// ---------------------------------------------------------------------------
// Pair correlation
// ---------------------------------------------------------------------------

export const PairCorrelationSchema = z.object({
  pair_a: z.string(),
  pair_b: z.string(),
  correlation_r: z.number(),
  method: z.string(),
  shared_wallet_count: z.number().int().nullable().optional(),
  computed_at: z.string().datetime(),
});

export type PairCorrelation = z.infer<typeof PairCorrelationSchema>;


// ---------------------------------------------------------------------------
// Counterfactual explanation
// ---------------------------------------------------------------------------

export const CounterfactualSchema = z.object({
  original_score: z.number().int(),
  counterfactual_score: z.number().int(),
  changed_features: z.record(z.string(), z.unknown()),
  explanation: z.string(),
});


// ---------------------------------------------------------------------------
// Webhook subscriber
// ---------------------------------------------------------------------------

export const WebhookSubscriberSchema = z.object({
  id: z.number().int(),
  url: z.string().url(),
  event_types: z.array(z.string()),
  active: z.boolean(),
  created_at: z.string().datetime(),

// ---------------------------------------------------------------------------
// Health, pagination, error
// ---------------------------------------------------------------------------

export const HealthSchema = z.object({
  status: z.string(),
  db: z.string(),
  models: z.string(),
});

export type Health = z.infer<typeof HealthSchema>;

export const PaginatedScoresSchema = z.object({
  scores: z.array(RiskScoreSchema),
  total: z.number().int().optional(),
});

export type PaginatedScores = z.infer<typeof PaginatedScoresSchema>;

export const ApiErrorSchema = z.object({
  detail: z.string(),
});

export type ApiError = z.infer<typeof ApiErrorSchema>;

});

export type WebhookSubscriber = z.infer<typeof WebhookSubscriberSchema>;

export type Counterfactual = z.infer<typeof CounterfactualSchema>;


export type Ring = z.infer<typeof RingSchema>;

  confidence: z.number().int().min(0).max(100),
  disputed: z.boolean().default(false),
  timestamp: z.string().datetime(),
  // Conformal prediction uncertainty fields (optional, v2+)
  score_lower: z.number().min(0).max(100).nullable().optional(),
  score_upper: z.number().min(0).max(100).nullable().optional(),
  prediction_set: z.array(z.number().int()).nullable().optional(),
  coverage_guarantee: z.number().min(0).max(1).nullable().optional(),
});

export type RiskScore = z.infer<typeof RiskScoreSchema>;
