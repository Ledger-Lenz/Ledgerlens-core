import { describe, it, expect, vi, beforeEach } from "vitest";
import { LedgerLensClient, LedgerLensError } from "../src/client";
import { RiskScoreSchema } from "../src/schemas";

function mockFetch(status: number, body: unknown): void {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
  });
}

describe("LedgerLensClient", () => {
  let client: LedgerLensClient;

  beforeEach(() => {
    client = new LedgerLensClient({ baseUrl: "http://localhost:8000" });
    vi.resetAllMocks();
  });

  describe("getHealth", () => {
    it("returns the health status", async () => {
      const healthBody = { status: "ok", db: "ok", models: "ok" };
      mockFetch(200, healthBody);

      const result = await client.getHealth();
      expect(result).toEqual(healthBody);
      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:8000/health",
        expect.objectContaining({
          headers: expect.objectContaining({ "Content-Type": "application/json" }),
        }),
      );
    });

    it("throws LedgerLensError on non-ok response", async () => {
      mockFetch(503, { detail: "Service unavailable" });
      await expect(client.getHealth()).rejects.toThrow(LedgerLensError);
    });
  });

  describe("getScores", () => {
    it("returns a list of risk scores", async () => {
      const scores = [
        {
          wallet: "GABCDEF1234567890123456789012345678901234567890123456",
          asset_pair: "XLM/USDC",
          score: 85,
          benford_flag: true,
          ml_flag: true,
          confidence: 90,
          disputed: false,
          timestamp: "2025-06-25T12:00:00Z",
        },
      ];
      mockFetch(200, scores);

      const result = await client.getScores();
      expect(result).toHaveLength(1);
      expect(result[0].wallet).toBe(
        "GABCDEF1234567890123456789012345678901234567890123456",
      );
      expect(result[0].score).toBe(85);
    });

    it("strips unknown fields from response", async () => {

  describe("getScore", () => {
    it("returns a single risk score", async () => {
      const score = {
        wallet: "GABCDEF1234567890123456789012345678901234567890123456",
        asset_pair: "XLM/USDC",
        score: 92,
        benford_flag: true,
        ml_flag: true,
        confidence: 95,
        disputed: false,
        timestamp: "2025-06-25T12:00:00Z",
      };
      mockFetch(200, score);

      const result = await client.getScore(
        "GABCDEF1234567890123456789012345678901234567890123456",
      );
      expect(result.score).toBe(92);
      // Full TypeScript inference: destructure
      const { score: s } = result;
      expect(s).toBe(92);
    });

    it("throws on invalid wallet format response", async () => {
      mockFetch(400, { detail: "Invalid wallet address" });
      await expect(
        client.getScore("invalid"),
      ).rejects.toThrow(LedgerLensError);
    });
  });

  describe("getAlerts", () => {
    it("returns alerts", async () => {
      const alerts = [
        {
          id: 1,
          wallet: "GABCDEF1234567890123456789012345678901234567890123456",
          alert_type: "WASH_TRADING",
          severity: "high",
          details: "Wash trading detected",
          detected_at: "2025-06-25T12:00:00Z",
          acknowledged: false,
        },
      ];
      mockFetch(200, alerts);

      const result = await client.getAlerts();
      expect(result).toHaveLength(1);
      expect(result[0].alert_type).toBe("WASH_TRADING");
    });

    it("applies query parameters", async () => {
      mockFetch(200, []);
      await client.getAlerts({ alert_type: "WASH_TRADING", limit: 10 });

      const url = (fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(url).toContain("alert_type=WASH_TRADING");
      expect(url).toContain("limit=10");
    });
  });

  describe("API key headers", () => {
    it("includes admin key header when configured", () => {
      const adminClient = new LedgerLensClient({
        baseUrl: "http://localhost:8000",
        adminKey: "admin-secret",
      });

      mockFetch(200, { status: "ok", db: "ok", models: "ok" });
      adminClient.getHealth();

      const headers = (fetch as ReturnType<typeof vi.fn>).mock.calls[0][1]
        .headers;
      expect(headers["X-LedgerLens-Admin-Key"]).toBe("admin-secret");
    });

    it("includes compliance key header when configured", () => {
      const compClient = new LedgerLensClient({
        baseUrl: "http://localhost:8000",
        complianceKey: "compliance-secret",
      });

      mockFetch(200, { status: "ok", db: "ok", models: "ok" });
      compClient.getHealth();

      const headers = (fetch as ReturnType<typeof vi.fn>).mock.calls[0][1]
        .headers;
      expect(headers["X-LedgerLens-Compliance-Key"]).toBe("compliance-secret");
    });
  });

  describe("Zod schema validation", () => {
    it("RiskScoreSchema strips unknown fields", () => {
      const raw = {
        wallet: "GABCDEF1234567890123456789012345678901234567890123456",
        asset_pair: "XLM/USDC",
        score: 75,
        benford_flag: true,
        ml_flag: false,
        confidence: 80,
        timestamp: "2025-06-25T12:00:00Z",
        extra: "should be stripped",
      };
      const parsed = RiskScoreSchema.parse(raw);
      expect(parsed).not.toHaveProperty("extra");
      expect(parsed.score).toBe(75);
    });

    it("RiskScoreSchema rejects out-of-range score", () => {
      const raw = {
        wallet: "GABCDEF1234567890123456789012345678901234567890123456",
        asset_pair: "XLM/USDC",
        score: 999,
        benford_flag: true,
        ml_flag: false,
        confidence: 80,
        timestamp: "2025-06-25T12:00:00Z",
      };
      expect(() => RiskScoreSchema.parse(raw)).toThrow();
    });
  });
});


  describe("getRings", () => {
    it("returns wash-trading rings", async () => {
      const rings = [
        {
          id: 1,
          accounts: ["A", "B", "C"],
          total_volume: 1000,
          cycle_volume: 500,
          avg_trade_count: 5,
          timing_tightness: 0.1,
          detected_at: "2025-06-25T12:00:00Z",
        },
      ];
      mockFetch(200, rings);

      const result = await client.getRings();
      expect(result).toHaveLength(1);
      expect(result[0].accounts).toEqual(["A", "B", "C"]);
    });
  });

  describe("error handling", () => {
    it("throws LedgerLensError on HTTP error with detail", async () => {
      mockFetch(401, { detail: "Unauthorized" });

      await expect(client.getHealth()).rejects.toThrow(LedgerLensError);
      await expect(client.getHealth()).rejects.toMatchObject({
        statusCode: 401,
        message: "Unauthorized",
      });
    });

    it("throws LedgerLensError on response validation failure", async () => {
      mockFetch(200, { invalid: "data" });

      await expect(
        client.getScore("GABCDEF1234567890123456789012345678901234567890123456"),
      ).rejects.toThrow(LedgerLensError);
    });

    it("handles network errors gracefully", async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error("Network error"));
      await expect(client.getHealth()).rejects.toThrow("Network error");
    });
  });

      const raw = {
        wallet: "GABCDEF1234567890123456789012345678901234567890123456",
        asset_pair: "XLM/USDC",
        score: 50,
        benford_flag: false,
        ml_flag: false,
        confidence: 75,
        disputed: false,
        timestamp: "2025-06-25T12:00:00Z",
        unknown_field: "should be stripped",
      };
      mockFetch(200, [raw]);

      const result = await client.getScores();
      expect(result[0]).not.toHaveProperty("unknown_field");
    });
  });
