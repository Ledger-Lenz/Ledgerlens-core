"""E2E: high-risk score -> GET /v1/alerts returns the score; low-risk score is absent.

Previously this file used raw ``sqlite3.connect`` calls to insert test data,
bypassing the storage abstraction layer (``detection.storage.save_scores``).
That pattern created hidden coupling: the raw DDL could diverge from the live
schema maintained by ``init_db``/``_MIGRATIONS``, and rows inserted this way
were not cleaned up between test cases, causing state to leak across the class.

Changes in this revision
------------------------
* Data insertion uses ``detection.storage.save_scores`` — the same function
  that the production pipeline uses — so the schema never drifts.
* Each test method uses a unique wallet address (prefixed with the test name)
  to avoid cross-test contamination even when the session-scoped DB is shared.
* Teardown deletes only the rows that the current test method inserted, keeping
  cleanup local and explicit rather than wiping shared session state.
* ``GET /v1/alerts`` returns a list of ``RiskScore`` JSON objects; each object
  has a ``wallet`` key. The extraction is now explicit and documented.
* The ``pytestmark`` at module level correctly marks every test in this module
  with the ``e2e`` marker (unlike a conftest-level mark, which pytest ignores).
"""

from datetime import datetime, timezone

import pytest

from detection.risk_score import RiskScore
from detection.storage import save_scores

pytestmark = pytest.mark.e2e

# Score threshold above which /v1/alerts returns a record (default 70).
_ALERT_THRESHOLD = 70


def _make_score(wallet: str, asset_pair: str, score: int) -> RiskScore:
    """Construct a minimal RiskScore for insertion via the storage layer."""
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score >= _ALERT_THRESHOLD,
        ml_flag=score >= _ALERT_THRESHOLD,
        confidence=90,
        timestamp=datetime.now(timezone.utc),
    )


class TestAlertFlow:
    def test_high_score_triggers_alert(self, e2e_client, e2e_db_path):
        """A score at or above the alert threshold must appear in GET /v1/alerts."""
        # Use a wallet address that is unique to this test method so that
        # residual rows from other tests cannot produce false positives.
        wallet = "GALRTHI1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ2345ABCDEFGHIJ"
        asset_pair = "XLM/USDC"
        high_score = 95

        save_scores([_make_score(wallet, asset_pair, high_score)], e2e_db_path)
        try:
            response = e2e_client.get("/v1/alerts")
            assert response.status_code == 200

            # /v1/alerts returns a list of RiskScore-shaped JSON objects; each
            # has a "wallet" key.
            alerts = response.json()
            assert isinstance(alerts, list), (
                f"Expected list from /v1/alerts, got {type(alerts).__name__}"
            )
            wallets_in_alerts = [a["wallet"] for a in alerts]
            assert wallet in wallets_in_alerts, (
                f"Expected wallet {wallet!r} in alerts (score={high_score}), "
                f"but got wallets: {wallets_in_alerts}"
            )
        finally:
            # Remove the rows this test inserted to avoid leaking state into
            # subsequent test cases that share the session-scoped database.
            import sqlite3

            with sqlite3.connect(e2e_db_path) as conn:
                conn.execute(
                    "DELETE FROM risk_scores WHERE wallet = ?", (wallet,)
                )

    def test_low_score_not_in_alerts(self, e2e_client, e2e_db_path):
        """A score below the alert threshold must not appear in GET /v1/alerts."""
        wallet = "GLOWSC1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ2345ABCDEFGHIJK"
        asset_pair = "XLM/USDC"
        low_score = 30

        save_scores([_make_score(wallet, asset_pair, low_score)], e2e_db_path)
        try:
            response = e2e_client.get("/v1/alerts")
            assert response.status_code == 200

            alerts = response.json()
            assert isinstance(alerts, list)
            wallets_in_alerts = [a["wallet"] for a in alerts]
            assert wallet not in wallets_in_alerts, (
                f"Wallet {wallet!r} (score={low_score}) should not appear in "
                f"alerts (threshold={_ALERT_THRESHOLD}), but was found."
            )
        finally:
            import sqlite3

            with sqlite3.connect(e2e_db_path) as conn:
                conn.execute(
                    "DELETE FROM risk_scores WHERE wallet = ?", (wallet,)
                )
