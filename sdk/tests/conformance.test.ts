/**
 * Cross-SDK conformance runner (TypeScript side).
 *
 * Executes every case in tests/contract/conformance/cases.json against the
 * shared reference server. Set LEDGERLENS_CONFORMANCE_URL (e.g.
 * http://127.0.0.1:8787) to run; skipped otherwise. See
 * tests/contract/conformance/README.md.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { describe, it, expect } from "vitest";
import { LedgerLensClient, LedgerLensError } from "../src/client";

type Case = {
  id: string;
  operation: "health" | "list_scores";
  args: { asset_pair?: string };
  expect: Record<string, unknown>;
};

const baseUrl = process.env.LEDGERLENS_CONFORMANCE_URL;
const { cases } = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, "../../tests/contract/conformance/cases.json"),
    "utf8",
  ),
) as { cases: Case[] };

async function run(client: LedgerLensClient, c: Case): Promise<unknown> {
  try {
    if (c.operation === "health") {
      const h = await client.getHealth();
      return { ok: { status: h.status } };
    }
    const scores = await client.getScores(
      c.args.asset_pair ? { asset_pair: c.args.asset_pair } : undefined,
    );
    return {
      ok: {
        wallets: scores.map((s) => s.wallet),
        scores: scores.map((s) => s.score),
      },
    };
  } catch (err) {
    if (err instanceof LedgerLensError && err.statusCode !== undefined) {
      return { error: { status: err.statusCode } };
    }
    throw err;
  }
}

describe.skipIf(!baseUrl)("conformance", () => {
  const client = new LedgerLensClient({ baseUrl });
  for (const c of cases) {
    it(c.id, async () => {
      const sel = await fetch(`${baseUrl}/__conformance/select?case=${c.id}`, {
        method: "POST",
      });
      expect(sel.ok).toBe(true);
      expect(await run(client, c)).toEqual(c.expect);
    });
  }
});
