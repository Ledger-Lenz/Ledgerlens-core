"""E2E: persist a risk score via the storage layer -> retrieve it via GET /v1/scores/{wallet}.

Previously this test used raw ``sqlite3.connect`` calls to write rows directly
into the database, bypassing the storage abstraction.  That pattern creates
hidden schema coupling (the raw INSERT must mirror ``_MIGRATIONS`` exactly),
produces no cleanup on test failure, and will silently break whenever a
migration adds a NOT NULL column.

Changes in this revision
------------------------
* Data insertion uses ``detection.storage.save_scores`` — the same function
  used by the production pipeline — so the schema is always consistent.
* Each test method inserts rows under a unique wallet address to prevent
  cross-test contamination on the shared session-scoped database.
* A ``try/finally`` block removes the inserted row after each test, keeping
  the database clean for subsequent tests regardless of assertion failures.
* ``GET /v1/scores/{wallet}`` is protected by
  ``Depends(require_scope("read:scores"))``; the ``e2e_client`` fixture
  now includes the ``X-LedgerLens-Api-Key`` header for a provisioned key, so
  this endpoint returns 200 rather than 401.
* The expected response structure ``{"scores": [...]}`` is asserted
  explicitly with a descriptive failure message.
"""

from datetime import datetime, timezone

import pytest

from detection.risk_score import RiskScore
from detection.storage import save_scores

pytestmark = pytest.mark.e2e


def _make_score(wallet: str, asset_pair: str, score: int) -> RiskScore:
    """Construct a minimal ``RiskScore`` for insertion via the storage layer."""
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score >= 70,
        ml_flag=score >= 70,
        confidence=90,
        timestamp=datetime.now(timezone.utc),
    )


class TestIngestScoreRetrieve:
    def test_ingest_score_and_retrieve(self, e2e_client, e2e_db_path):
        """Score inserted via save_scores is returned by GET /v1/scores/{wallet}.

        Covers the path: storage write -> API read.  The test does not
        exercise the full detection pipeline (that belongs in a dedicated
        pipeline smoke test); it verifies that the storage↔API contract is
        intact end-to-end.
        """
        # Use a wallet address that is unique to this test to avoid collisions
        # with other tests running against the same session-scoped database.
        wallet = "GABCDE1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ2345ABCDEFGHIJK"
        asset_pair = "XLM/USDC"
        score = 85

        save_scores([_make_score(wallet, asset_pair, score)], e2e_db_path)
        try:
            response = e2e_client.get(f"/v1/scores/{wallet}")
            assert response.status_code == 200, (
                f"Expected 200 from GET /v1/scores/{wallet}, "
                f"got {response.status_code}: {response.text}"
            )

            data = response.json()
            assert "scores" in data, (
                f"Response body must contain 'scores' key; got keys: {list(data.keys())}"
            )

            scores_list = data["scores"]
            assert len(scores_list) >= 1, (
                f"Expected at least one score for wallet {wallet!r}, got empty list"
            )

            # Locate the score for the asset pair inserted by this test.
            matching = [
                s for s in scores_list if s.get("asset_pair") == asset_pair
            ]
            assert matching, (
                f"No score for asset_pair={asset_pair!r} in response: {scores_list}"
            )
            assert matching[0]["score"] == score, (
                f"Expected score={score}, got {matching[0]['score']}"
            )
        finally:
            # Clean up only the rows this test inserted.
            import sqlite3

            with sqlite3.connect(e2e_db_path) as conn:
                conn.execute(
                    "DELETE FROM risk_scores WHERE wallet = ?", (wallet,)
                )
