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

/**
 * Error thrown for every failure surfaced by {@link LedgerLensClient}.
 *
 * This covers three cases:
 * - a non-2xx HTTP response (`statusCode` is set, `zodIssues` is undefined);
 * - a response whose body fails Zod validation (`statusCode` and `zodIssues`
 *   are both set);
 * - a transport-level failure such as a timeout (`statusCode` is undefined).
 */
export class LedgerLensError extends Error {
  /**
   * @param message   Human-readable description of the failure.
   * @param statusCode HTTP status code of the response, when the failure
   *                   originated from an HTTP response. Undefined for
   *                   transport-level failures (e.g. timeouts).
   * @param zodIssues  Zod validation issues, present only when the response
   *                   body failed schema validation.
   */
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

/**
 * Configuration accepted by the {@link LedgerLensClient} constructor.
 * Every field is optional.
 */
export interface LedgerLensClientOptions {
  /**
   * Base URL of the LedgerLens API, scheme + host with no trailing path.
   * @defaultValue `"http://localhost:8000"`
   */
  baseUrl?: string;
  /** Admin API key, sent as the `X-LedgerLens-Admin-Key` request header. */
  adminKey?: string;
  /** Compliance API key, sent as the `X-LedgerLens-Compliance-Key` request header. */
  complianceKey?: string;
  /**
   * Per-request timeout in milliseconds. When exceeded the request is aborted
   * and a {@link LedgerLensError} is thrown.
   * @defaultValue `30000`
   */
  timeout?: number;
  /**
   * Extra `fetch` init options merged into every request (e.g. `credentials`,
   * `mode`, custom `headers`). Explicit SDK headers take precedence.
   */
  fetchInit?: RequestInit;
}

// ---------------------------------------------------------------------------
// Internal response parser
// ---------------------------------------------------------------------------

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
  context: string,
): Promise<T> {
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      const parsed = ApiErrorSchema.safeParse(body);
      if (parsed.success) detail = parsed.data.detail;
    } catch {
      // Ignore malformed error bodies and retain the HTTP status message.
    }
    throw new LedgerLensError(detail, response.status);
  }

  const result = schema.safeParse(await response.json());
  if (!result.success) {
    throw new LedgerLensError(
      `Response validation failed for ${context}`,
      response.status,
      result.error.issues,
    );
  }
  return result.data;
}

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

  /**
   * Creates a new client.
   *
   * @param options Client configuration. See {@link LedgerLensClientOptions}.
   *                Defaults to an empty object, which targets
   *                `http://localhost:8000` with a 30s timeout and no auth.
   */
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

  /**
   * Fetches API liveness/readiness information via `GET /health`.
   *
   * @returns The parsed {@link Health} payload (`status`, `db`, `models`).
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
  async getHealth(): Promise<Health> {
    const res = await this._fetch("/health");
    return parseResponse(res, HealthSchema, "getHealth");
  }

  // -----------------------------------------------------------------------
  // Scores
  // -----------------------------------------------------------------------

  /**
   * Lists risk scores via `GET /scores`, optionally filtered and paginated.
   *
   * @param params Optional query parameters:
   *   - `wallet`  — restrict to a single wallet address;
   *   - `limit`   — maximum number of records to return;
   *   - `offset`  — number of records to skip (pagination);
   *   - `sort_by` — field name to sort by;
   *   - `order`   — sort direction, `"asc"` or `"desc"`.
   * @returns An array of {@link RiskScore} records.
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
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

  /**
   * Fetches the risk score for a single wallet via `GET /score/{wallet}`.
   *
   * @param wallet The wallet address to look up. It is URL-encoded before use.
   * @returns The parsed {@link RiskScore} for the wallet.
   * @throws {LedgerLensError} On a non-2xx response (e.g. 404 unknown wallet),
   *   validation failure, or timeout.
   */
  async getScore(wallet: string): Promise<RiskScore> {
    const res = await this._fetch(`/score/${encodeURIComponent(wallet)}`);
    return parseResponse(res, RiskScoreSchema, `getScore(${wallet})`);
  }

  /**
   * Lists alerts via `GET /alerts`, optionally filtered and paginated.
   *
   * @param params Optional query parameters:
   *   - `alert_type` — filter by alert type (see {@link AlertType});
   *   - `wallet`     — filter to a single wallet address;
   *   - `limit`      — maximum number of records to return;
   *   - `offset`     — number of records to skip (pagination).
   * @returns An array of {@link Alert} records.
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
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

  /**
   * Fetches liquidity-pool trades for a wallet via
   * `GET /liquidity-pool-trades/{wallet}`.
   *
   * @param wallet The wallet address to look up. It is URL-encoded before use.
   * @returns An array of {@link LiquidityPoolTrade} records for the wallet.
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
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

  // -----------------------------------------------------------------------
  // Asset risk rankings
  // -----------------------------------------------------------------------

  /**
   * Fetches per-asset-pair risk rankings via `GET /assets/risk-ranking`.
   *
   * @returns An array of {@link AssetRiskRanking} records.
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
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

  /**
   * Lists detected wash-trading rings via `GET /rings`.
   *
   * @param params Optional pagination parameters:
   *   - `limit`  — maximum number of records to return;
   *   - `offset` — number of records to skip.
   * @returns An array of {@link Ring} records.
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
  async getRings(params?: { limit?: number; offset?: number }): Promise<Ring[]> {
    const qs = this._buildQuery(params);
    const res = await this._fetch(`/rings${qs}`);
    return parseResponse(res, z.array(RingSchema), "getRings");
  }

  // -----------------------------------------------------------------------
  // Pair correlations
  // -----------------------------------------------------------------------

  /**
   * Fetches asset-pair correlations via `GET /correlations`.
   *
   * @returns An array of {@link PairCorrelation} records.
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
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

  /**
   * Fetches the counterfactual explanation for a wallet's risk score via
   * `GET /score/{wallet}/counterfactual`.
   *
   * @param wallet The wallet address to explain. It is URL-encoded before use.
   * @returns The parsed {@link Counterfactual} explanation.
   * @throws {LedgerLensError} On a non-2xx response, validation failure, or timeout.
   */
  async getCounterfactual(wallet: string): Promise<Counterfactual> {
    const res = await this._fetch(
      `/score/${encodeURIComponent(wallet)}/counterfactual`,
    );
    return parseResponse(res, CounterfactualSchema, "getCounterfactual");
  }

  // -----------------------------------------------------------------------
  // Webhook subscribers (admin)
  // -----------------------------------------------------------------------

  /**
   * Lists webhook subscribers via `GET /admin/webhook/subscribers`.
   * Requires an admin key (see {@link LedgerLensClientOptions.adminKey}).
   *
   * @returns An array of {@link WebhookSubscriber} records.
   * @throws {LedgerLensError} On a non-2xx response (e.g. 401/403 without an
   *   admin key), validation failure, or timeout.
   */
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

  /**
   * Fetches model drift reports via `GET /admin/drift`.
   * Requires an admin key (see {@link LedgerLensClientOptions.adminKey}).
   *
   * The response shape is not currently modelled by a schema, so the raw
   * parsed JSON is returned untyped.
   *
   * @returns The raw JSON body of the drift report response.
   * @throws {LedgerLensError} On a timeout. Note: unlike the other methods this
   *   call does not check the HTTP status code.
   */
  async getDriftReports(): Promise<unknown> {
    const res = await this._fetch("/admin/drift");
    return res.json();
  }

  // -----------------------------------------------------------------------
  // Private helpers
  // -----------------------------------------------------------------------

  /**
   * Issues a `fetch` to `baseUrl + path` with the configured headers and an
   * abort-based timeout.
   *
   * @param path Request path beginning with `/` (query string included).
   * @returns The raw {@link Response}; status checking happens in `parseResponse`.
   * @throws {LedgerLensError} When the request exceeds the configured timeout.
   */
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

  /**
   * Serialises a params object into a query string.
   *
   * `undefined` and `null` values are omitted; all other values are coerced
   * with `String()`.
   *
   * @param params Key/value pairs to encode.
   * @returns A query string beginning with `?`, or an empty string when there
   *   are no parameters to encode.
   */
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
