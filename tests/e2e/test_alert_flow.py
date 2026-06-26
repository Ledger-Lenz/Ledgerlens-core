"""E2E: high-risk score -> alert fires -> GET /alerts returns the alert."""

import sqlite3
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.e2e


class TestAlertFlow:
    def test_high_score_triggers_alert(self, e2e_client, e2e_db_path):
        """A score above threshold should appear in the alerts endpoint."""
        wallet = "GALERTFLOW1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ2345ABCDEFG"
        asset_pair = "XLM/USDC"
        high_score = 95
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(e2e_db_path) as conn:
            conn.execute(
                "INSERT INTO risk_scores (wallet, asset_pair, score, confidence, "
                "benford_flag, ml_flag, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (wallet, asset_pair, high_score, 90, 1, 1, now),
            )

        response = e2e_client.get("/v1/alerts")
        assert response.status_code == 200
        alerts = response.json()
        wallets_in_alerts = [a["wallet"] for a in alerts]
        assert wallet in wallets_in_alerts

    def test_low_score_not_in_alerts(self, e2e_client, e2e_db_path):
        """A score below threshold should not appear in alerts."""
        wallet = "GLOWSCORE1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ2345ABCDEFG"
        asset_pair = "XLM/USDC"
        low_score = 30
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(e2e_db_path) as conn:
            conn.execute(
                "INSERT INTO risk_scores (wallet, asset_pair, score, confidence, "
                "benford_flag, ml_flag, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (wallet, asset_pair, low_score, 90, 0, 0, now),
            )

        response = e2e_client.get("/v1/alerts")
        assert response.status_code == 200
        alerts = response.json()
        wallets_in_alerts = [a["wallet"] for a in alerts]
        assert wallet not in wallets_in_alerts
