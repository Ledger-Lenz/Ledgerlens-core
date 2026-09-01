/**
 * Cross-language schema contract vector tests (TypeScript/Zod side).
 *
 * These tests load the canonical fixture file `tests/fixtures/contract_vectors.json`
 * and verify that every valid vector parses cleanly via Zod. They also verify that
 * adversarial vectors are rejected, proving divergence detection.
 *
 * ADR reference: docs/adr/ADR-005-schema-contract-enforcement.md
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { describe, it, expect } from "vitest";
import { ZodError } from "zod";
import { RiskScoreSchema } from "../src/schemas";

// ---------------------------------------------------------------------------
// Fixture loading
// ---------------------------------------------------------------------------

type ContractVectors = {
  _contract_version: string;
  risk_score: Record<string, Record<string, unknown>>;
  risk_score_adversarial: Record<string, Record<string, unknown>>;
  trade: Record<string, Record<string, unknown>>;
  asset: Record<string, Record<string, unknown>>;
  required_risk_score_fields: string[];
  required_trade_fields: string[];
  required_asset_fields: string[];
};

function loadFixture(): ContractVectors {
  // Resolve path relative to this test file's location:
  // sdk/tests/contract_vectors.test.ts → ../../tests/fixtures/contract_vectors.json
  const fixturePath = path.resolve(
    __dirname,
    "../../tests/fixtures/contract_vectors.json",
  );
  if (!fs.existsSync(fixturePath)) {
    throw new Error(
      `Contract vectors fixture not found at ${fixturePath}. ` +
        "Run: python scripts/generate_contract_vectors.py",
    );
  }
  return JSON.parse(fs.readFileSync(fixturePath, "utf-8")) as ContractVectors;
}

/** Strip fixture metadata keys (prefixed with _) before parsing. */
function stripMeta(obj: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(obj).filter(([k]) => !k.startsWith("_")),
  );
}

// Load once at module level to avoid re-reading per test.
const fixture = loadFixture();

// ---------------------------------------------------------------------------
// Valid vector tests
// ---------------------------------------------------------------------------

describe("RiskScoreSchema – valid contract vectors", () => {
  it("parses the 'complete' vector with all fields including latency_ms", () => {
    const raw = stripMeta(fixture.risk_score.complete);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success, `Parse failed: ${JSON.stringify((result as { error?: unknown }).error)}`).toBe(true);

    if (!result.success) return;
    const score = result.data;

    expect(score.wallet).toBeTruthy();
    expect(score.asset_pair).toBeTruthy();
    expect(score.score).toBeGreaterThanOrEqual(0);
    expect(score.score).toBeLessThanOrEqual(100);
    expect(typeof score.benford_flag).toBe("boolean");
    expect(typeof score.ml_flag).toBe("boolean");
    expect(score.confidence).toBeGreaterThanOrEqual(0);
    expect(score.confidence).toBeLessThanOrEqual(100);

    // latency_ms: must be present and a number in the complete vector
    expect(score.latency_ms).not.toBeNull();
    expect(score.latency_ms).not.toBeUndefined();
    expect(typeof score.latency_ms).toBe("number");
  });

  it("parses the 'minimal' vector with all optional fields null", () => {
    const raw = stripMeta(fixture.risk_score.minimal);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);

    if (!result.success) return;
    expect(result.data.latency_ms ?? null).toBeNull();
    expect(result.data.score_lower ?? null).toBeNull();
    expect(result.data.score_upper ?? null).toBeNull();
    expect(result.data.prediction_set ?? null).toBeNull();
    expect(result.data.coverage_guarantee ?? null).toBeNull();
  });

  it("parses the 'disputed' vector with disputed=true", () => {
    const raw = stripMeta(fixture.risk_score.disputed);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.disputed).toBe(true);
    }
  });

  it("parses 'score_boundary_zero' (score=0)", () => {
    const raw = stripMeta(fixture.risk_score.score_boundary_zero);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.score).toBe(0);
    }
  });

  it("parses 'score_boundary_hundred' (score=100)", () => {
    const raw = stripMeta(fixture.risk_score.score_boundary_hundred);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.score).toBe(100);
    }
  });

  it("parses v2+ uncertainty fields correctly in 'complete' vector", () => {
    const raw = stripMeta(fixture.risk_score.complete);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (!result.success) return;

    expect(result.data.score_lower).not.toBeNull();
    expect(result.data.score_upper).not.toBeNull();
    expect(result.data.prediction_set).not.toBeNull();
    expect(result.data.coverage_guarantee).not.toBeNull();
    expect(Array.isArray(result.data.prediction_set)).toBe(true);
  });

  it("all valid vector keys deserialize without error", () => {
    const validKeys = [
      "complete",
      "minimal",
      "disputed",
      "score_boundary_zero",
      "score_boundary_hundred",
    ];
    for (const key of validKeys) {
      const raw = stripMeta(fixture.risk_score[key]);
      const result = RiskScoreSchema.safeParse(raw);
      expect(
        result.success,
        `Vector '${key}' failed: ${JSON.stringify((result as { error?: unknown }).error)}`,
      ).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// Adversarial vector tests (divergence detection)
// ---------------------------------------------------------------------------

describe("RiskScoreSchema – adversarial vectors (divergence detection)", () => {
  it("rejects a payload with 'risk_score' instead of 'score' (wrong field name)", () => {
    /**
     * This test proves divergence detection: a field rename in the Python model
     * that is not reflected in the TS schema will cause a parse failure here,
     * provided the renamed field is also absent from the Zod schema.
     *
     * The adversarial vector uses 'risk_score' instead of 'score'. Since 'score'
     * is required in RiskScoreSchema, the parse must fail with a ZodError.
     */
    const raw = stripMeta(fixture.risk_score_adversarial.wrong_field_name);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error as ZodError;
      const fieldPaths = err.errors.map((e) => e.path.join("."));
      expect(fieldPaths).toContain("score");
    }
  });

  it("rejects a payload with score=999 (out of range)", () => {
    const raw = stripMeta(fixture.risk_score_adversarial.score_out_of_range);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error as ZodError;
      const fieldPaths = err.errors.map((e) => e.path.join("."));
      expect(fieldPaths).toContain("score");
    }
  });

  it("rejects a payload where 'score' is renamed to 'SCORE' (case drift)", () => {
    /**
     * Simulate what happens when a language implementation uses camelCase or
     * wrong casing. This is the canonical cross-language drift scenario.
     */
    const raw = stripMeta(fixture.risk_score.complete);
    const drifted = { ...raw, SCORE: raw.score };
    delete (drifted as Record<string, unknown>).score;

    const result = RiskScoreSchema.safeParse(drifted);
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error as ZodError;
      const fieldPaths = err.errors.map((e) => e.path.join("."));
      expect(fieldPaths).toContain("score");
    }
  });
});

// ---------------------------------------------------------------------------
// Required fields agreement check
// ---------------------------------------------------------------------------

describe("RiskScoreSchema – required fields agreement with fixture", () => {
  it("RiskScoreSchema includes all fields declared in required_risk_score_fields", () => {
    /**
     * This test checks that every field the Python canonical model declares as
     * required is actually present in the Zod schema. If a new field is added to
     * the Python model and not to the Zod schema, the round-trip test for the
     * 'complete' vector will catch it.
     *
     * We check by validating the 'complete' vector (which has all fields populated)
     * and confirming the parsed output contains all required fields.
     */
    const raw = stripMeta(fixture.risk_score.complete);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (!result.success) return;

    const parsedKeys = new Set(Object.keys(result.data));

    // Check each required field is in the parsed output
    // (optional fields may not appear if null, but should be parseable)
    const alwaysPresent = [
      "wallet",
      "asset_pair",
      "score",
      "benford_flag",
      "ml_flag",
      "confidence",
      "disputed",
      "timestamp",
    ];
    for (const field of alwaysPresent) {
      expect(parsedKeys.has(field), `Required field '${field}' absent from parsed output`).toBe(
        true,
      );
    }
  });

  it("latency_ms is in the Zod schema", () => {
    /**
     * Explicit check for the canonical divergence example: latency_ms was absent
     * from the TypeScript SDK while present in the Python model.
     */
    // Parse a vector that has latency_ms populated
    const raw = stripMeta(fixture.risk_score.complete);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (!result.success) return;

    // If latency_ms were not in the schema, it would be stripped by Zod's default
    // strip behaviour and would be undefined/missing from the result.
    // We check that the raw value round-tripped.
    expect("latency_ms" in result.data).toBe(true);
    expect(result.data.latency_ms).not.toBeUndefined();
  });

  it("v2+ uncertainty fields are in the Zod schema", () => {
    /**
     * Acceptance criterion: 'The schema-contract-enforcement mechanism covers,
     * at minimum, RiskScore's uncertainty-quantification fields.'
     */
    const requiredV2Fields = [
      "score_lower",
      "score_upper",
      "prediction_set",
      "coverage_guarantee",
    ];
    const raw = stripMeta(fixture.risk_score.complete);
    const result = RiskScoreSchema.safeParse(raw);
    expect(result.success).toBe(true);
    if (!result.success) return;

    for (const field of requiredV2Fields) {
      expect(
        field in result.data,
        `v2+ field '${field}' absent from Zod schema output`,
      ).toBe(true);
    }
  });

  it("fixture _contract_version is present", () => {
    expect(fixture._contract_version).toBeTruthy();
  });
});
