/**
 * LedgerLensClient — the main entry point for consuming the LedgerLens API.
 *
 * Every method wraps an HTTP call to the LedgerLens API and validates the
 * response with the corresponding Zod schema.  Unknown fields are silently
 * stripped.  On validation failure a `LedgerLensError` is thrown with the
 * Zod issue details.
 */

import { z } from "zod";
import type {
  Alert,
  AssetRiskRanking,
  Counterfactual,
  Health,
  LiquidityPoolTrade,
  PairCorrelation,
  Ring,
  RiskScore,
  WebhookSubscriber,
} from "./schemas";
import {
  AlertSchema,
  ApiErrorSchema,
  AssetRiskRankingSchema,
  CounterfactualSchema,
  HealthSchema,
  LiquidityPoolTradeSchema,
  PairCorrelationSchema,
  RingSchema,
  RiskScoreSchema,
  WebhookSubscriberSchema,
} from "./schemas";

// ---------------------------------------------------------------------------
// Error
// ---------------------------------------------------------------------------

export class LedgerLensError extends Error {
  constructor(
    message: string,
    public readonly statusCode?: number,
    public readonly zodIssues?: z.ZodIssue[],
  ) {
    super(message);
    this.name = "LedgerLensError";
  }
}

// ---------------------------------------------------------------------------
// Client options
// ---------------------------------------------------------------------------

export interface LedgerLensClientOptions {
  baseUrl?: string;
  adminKey?: string;
  complianceKey?: string;
  timeout?: number;
  fetchInit?: RequestInit;
}

// ---------------------------------------------------------------------------
// Internal response parser
// ---------------------------------------------------------------------------

async function parseResponse<T>(

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

/**
 * LedgerLens API client with full TypeScript inference and Zod runtime validation.
 *
 * @example
 * ```ts
 * const client = new LedgerLensClient({ baseUrl: "http://localhost:8000" });
 * const scores = await client.getScores();
 * const { score } = await client.getScore("G...");
 * ```
 */
export class LedgerLensClient {
  private readonly baseUrl: string;
  private readonly timeout: number;
  private readonly fetchInit: RequestInit;

  constructor(options: LedgerLensClientOptions = {}) {
    this.baseUrl = options.baseUrl ?? "http://localhost:8000";
    this.timeout = options.timeout ?? 30_000;
    this.fetchInit = options.fetchInit ?? {};

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.fetchInit.headers as Record<string, string>),
    };

    if (options.adminKey) {
      headers["X-LedgerLens-Admin-Key"] = options.adminKey;
    }
    if (options.complianceKey) {
      headers["X-LedgerLens-Compliance-Key"] = options.complianceKey;
    }

    this.fetchInit = { ...this.fetchInit, headers };
  }

  // -----------------------------------------------------------------------
  // Health
  // -----------------------------------------------------------------------

  async getHealth(): Promise<Health> {
    const res = await this._fetch("/health");
    return parseResponse(res, HealthSchema, "getHealth");
  }

  // -----------------------------------------------------------------------
  // Scores
  // -----------------------------------------------------------------------

  async getScores(
    params?: {
      wallet?: string;
      limit?: number;
      offset?: number;
      sort_by?: string;
      order?: "asc" | "desc";
    },
  ): Promise<RiskScore[]> {
    const qs = this._buildQuery(params);
    const res = await this._fetch(`/scores${qs}`);
    return parseResponse(res, z.array(RiskScoreSchema), "getScores");
  }

  async getScore(wallet: string): Promise<RiskScore> {

  // -----------------------------------------------------------------------
  // Asset risk rankings
  // -----------------------------------------------------------------------

  async getAssetRiskRankings(): Promise<AssetRiskRanking[]> {
    const res = await this._fetch("/assets/risk-ranking");
    return parseResponse(
      res,
      z.array(AssetRiskRankingSchema),
      "getAssetRiskRankings",
    );
  }

  // -----------------------------------------------------------------------
  // Wash-trading rings
  // -----------------------------------------------------------------------

  async getRings(params?: { limit?: number; offset?: number }): Promise<Ring[]> {
    const qs = this._buildQuery(params);
    const res = await this._fetch(`/rings${qs}`);
    return parseResponse(res, z.array(RingSchema), "getRings");
  }

  // -----------------------------------------------------------------------
  // Pair correlations
  // -----------------------------------------------------------------------

  async getCorrelations(): Promise<PairCorrelation[]> {
    const res = await this._fetch("/correlations");
    return parseResponse(
      res,
      z.array(PairCorrelationSchema),
      "getCorrelations",
    );
  }

  // -----------------------------------------------------------------------
  // Counterfactual explanations
  // -----------------------------------------------------------------------

  async getCounterfactual(wallet: string): Promise<Counterfactual> {
    const res = await this._fetch(
      `/score/${encodeURIComponent(wallet)}/counterfactual`,
    );
    return parseResponse(res, CounterfactualSchema, "getCounterfactual");
  }

  // -----------------------------------------------------------------------
  // Webhook subscribers (admin)
  // -----------------------------------------------------------------------

  async getWebhookSubscribers(): Promise<WebhookSubscriber[]> {
    const res = await this._fetch("/admin/webhook/subscribers");
    return parseResponse(
      res,
      z.array(WebhookSubscriberSchema),
      "getWebhookSubscribers",
    );
  }

  // -----------------------------------------------------------------------
  // Admin / observability endpoints
  // -----------------------------------------------------------------------

  async getDriftReports(): Promise<unknown> {
    const res = await this._fetch("/admin/drift");
    return res.json();
  }

  // -----------------------------------------------------------------------
  // Private helpers
  // -----------------------------------------------------------------------

  private async _fetch(path: string): Promise<Response> {
    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await fetch(url, {
        ...this.fetchInit,
        signal: controller.signal,
      });
      return response;
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        throw new LedgerLensError(
          `Request timed out after ${this.timeout}ms: ${url}`,
        );
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  private _buildQuery(
    params?: Record<string, unknown>,
  ): string {
    if (!params || Object.keys(params).length === 0) return "";
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        qs.set(key, String(value));
      }
    }
    const str = qs.toString();
    return str ? `?${str}` : "";
  }
}

    const res = await this._fetch(`/score/${encodeURIComponent(wallet)}`);
    return parseResponse(res, RiskScoreSchema, `getScore(${wallet})`);
  }

  // -----------------------------------------------------------------------
  // Alerts
  // -----------------------------------------------------------------------

  async getAlerts(
    params?: {
      alert_type?: string;
      wallet?: string;
      limit?: number;
      offset?: number;
    },
  ): Promise<Alert[]> {
    const qs = this._buildQuery(params);
    const res = await this._fetch(`/alerts${qs}`);
    return parseResponse(res, z.array(AlertSchema), "getAlerts");
  }

  // -----------------------------------------------------------------------
  // Liquidity pool trades
  // -----------------------------------------------------------------------

  async getLiquidityPoolTrades(wallet: string): Promise<LiquidityPoolTrade[]> {
    const res = await this._fetch(
      `/liquidity-pool-trades/${encodeURIComponent(wallet)}`,
    );
    return parseResponse(
      res,
      z.array(LiquidityPoolTradeSchema),
      "getLiquidityPoolTrades",
    );
  }
}

  response: Response,
  schema: z.ZodType<T>,
  context: string,
): Promise<T> {
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      const parsed = ApiErrorSchema.safeParse(body);
      if (parsed.success) {
        detail = parsed.data.detail;
      }
    } catch {
      // ignore
    }
    throw new LedgerLensError(detail, response.status);
  }

  const json: unknown = await response.json();
  const result = schema.safeParse(json);

  if (!result.success) {
    throw new LedgerLensError(
      `Response validation failed for ${context}`,
      response.status,
      result.error.issues,
    );
  }

  return result.data;
}
