/**
 * Basic usage example for @ledgerlens/sdk.
 *
 * Instantiates the client, checks API health, lists a page of risk scores,
 * and fetches the detail for a single wallet. Errors from the API are
 * surfaced as `LedgerLensError`.
 *
 * Run it against a local API:
 *
 *   cd sdk
 *   npm install
 *   LEDGERLENS_BASE_URL=http://localhost:8000 npm run example
 *
 * (`npm run example` uses `tsx` to execute this file directly — no build step.)
 *
 * When consuming the published package instead of running it from this repo,
 * change the import below to:
 *
 *   import { LedgerLensClient, LedgerLensError } from "@ledgerlens/sdk";
 */

import { LedgerLensClient, LedgerLensError } from "../src/index";

async function main(): Promise<void> {
  const client = new LedgerLensClient({
    baseUrl: process.env.LEDGERLENS_BASE_URL ?? "http://localhost:8000",
    // adminKey / complianceKey can be passed here for gated endpoints:
    // adminKey: process.env.LEDGERLENS_ADMIN_KEY,
    timeout: 15_000,
  });

  try {
    // 1. Liveness check.
    const health = await client.getHealth();
    console.log("API health:", health);

    // 2. List the 5 highest-scoring wallets.
    const scores = await client.getScores({
      limit: 5,
      sort_by: "score",
      order: "desc",
    });
    console.log(`\nFetched ${scores.length} risk score(s):`);
    for (const s of scores) {
      const flags = [s.benford_flag && "benford", s.ml_flag && "ml"]
        .filter(Boolean)
        .join(",");
      console.log(
        `  ${s.wallet}  ${s.asset_pair}  score=${s.score} ` +
          `confidence=${s.confidence}${flags ? `  flags=[${flags}]` : ""}`,
      );
    }

    // 3. Fetch the full record for the first wallet, if any.
    if (scores.length > 0) {
      const detail = await client.getScore(scores[0].wallet);
      console.log(`\nDetail for ${detail.wallet}:`, detail);
    }
  } catch (err) {
    if (err instanceof LedgerLensError) {
      console.error(
        `LedgerLens API error (HTTP ${err.statusCode ?? "n/a"}): ${err.message}`,
      );
      if (err.zodIssues) console.error("Validation issues:", err.zodIssues);
      process.exitCode = 1;
      return;
    }
    throw err;
  }
}

main();
