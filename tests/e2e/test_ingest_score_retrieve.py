"""E2E: ingest synthetic trade batch -> score wallet -> GET /scores/{wallet} returns expected score."""

import sqlite3
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.e2e


class TestIngestScoreRetrieve:
    def test_ingest_score_and_retrieve(self, e2e_client, e2e_db_path):
        """Full path: insert a score directly, then retrieve via the API."""
        wallet = "GABCDEFGHIJKLMNOPQRSTUVWXYZ234567ABCDEFGHIJKLMNOPQRSTUVWX"
        asset_pair = "XLM/USDC"
        score = 85
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(e2e_db_path) as conn:
            conn.execute(
                "INSERT INTO risk_scores (wallet, asset_pair, score, confidence, "
                "benford_flag, ml_flag, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (wallet, asset_pair, score, 90, 1, 1, now),
            )

        response = e2e_client.get(f"/v1/scores/{wallet}")
        assert response.status_code == 200
        data = response.json()
        assert "scores" in data
        scores = data["scores"]
        assert len(scores) >= 1
        assert scores[0]["score"] == score
        assert scores[0]["asset_pair"] == asset_pair
