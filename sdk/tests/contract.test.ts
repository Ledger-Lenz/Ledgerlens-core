import { readFileSync } from "fs";
import { resolve } from "path";
import { describe, it, expect } from "vitest";
import { RiskScoreSchema } from "../src/schemas";

describe("Cross-repo schema contracts", () => {
  it("RiskScore schema matches fixture", () => {
    const fixturePath = resolve(__dirname, "../../tests/fixtures/schemas/risk_score_v1.json");
    const data = JSON.parse(readFileSync(fixturePath, "utf-8"));
    
    // Parse from JSON fixture
    const score = RiskScoreSchema.parse(data);
    
    expect(score.wallet).toBe("GBXGQJWVN5C3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3");
    expect(score.score).toBe(85);
    expect(score.benford_flag).toBe(true);
    
    // Check that output matches the fixture
    // Zod strip doesn't strip defined keys, but might not match exactly if fields are missing.
    // We just ensure all keys from fixture are present in output.
    for (const key of Object.keys(data)) {
      expect((score as any)[key]).toEqual(data[key]);
    }
  });
});
