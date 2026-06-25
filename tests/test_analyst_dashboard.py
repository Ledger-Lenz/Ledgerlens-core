"""Tests for the Analyst Review Dashboard API (Issue #200).

Covers: empty queue, queue ordering, feedback submission, stats computation,
and the active learning feedback export endpoint.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.auth import require_admin_key
from detection.risk_score import RiskScore
from detection.storage import save_scores, init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _noop_admin():
    """Bypass admin key check in tests."""
    return None


@pytest.fixture(autouse=True)
def webhook_enc_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ledgerlens_analyst_test.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)

    import config.settings as settings_module
    object.__setattr__(settings_module.settings, "db_path", db_path)

    init_db(db_path)
    app.dependency_overrides[require_admin_key] = _noop_admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_score(wallet: str, score: int, asset_pair: str = "XLM/USDC") -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score > 50,
        ml_flag=score > 50,
        confidence=80,
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Queue tests
# ---------------------------------------------------------------------------


def test_analyst_queue_empty(client):
    """Empty queue returns an empty list, not an error."""
    resp = client.get("/analyst/queue")
    assert resp.status_code == 200
    assert resp.json() == []


def test_analyst_queue_ordering(client):
    """Queue is ordered by score descending."""
    import detection.storage as store
    wallets = [
        ("G" + "A" * 55, 90),
        ("G" + "B" * 55, 40),
        ("G" + "C" * 55, 75),
    ]
    save_scores([_make_score(w, s) for w, s in wallets], store.settings.db_path)

    resp = client.get("/analyst/queue")
    assert resp.status_code == 200
    body = resp.json()
    scores_returned = [r["score"] for r in body]
    assert scores_returned == sorted(scores_returned, reverse=True), (
        f"Queue not sorted by score descending: {scores_returned}"
    )


def test_analyst_queue_respects_limit(client):
    """Queue respects the `limit` parameter."""
    import detection.storage as store
    for i in range(5):
        save_scores([_make_score("G" + chr(65 + i) * 55, 50 + i)], store.settings.db_path)

    resp = client.get("/analyst/queue?limit=3")
    assert resp.status_code == 200
    assert len(resp.json()) <= 3


def test_analyst_queue_reviewed_wallet_excluded(client):
    """Wallet reviewed today does not appear in the queue again."""
    import detection.storage as store

    wallet = "G" + "D" * 55
    save_scores([_make_score(wallet, 85)], store.settings.db_path)

    # Submit feedback → wallet is now reviewed today
    client.post(
        f"/analyst/wallet/{wallet}/feedback",
        json={"verdict": "confirmed_wash", "analyst_key_hash": "abc123"},
    )

    queue = client.get("/analyst/queue").json()
    wallets_in_queue = [r["wallet"] for r in queue]
    assert wallet not in wallets_in_queue, "Reviewed wallet must not appear in queue"


# ---------------------------------------------------------------------------
# Wallet view tests
# ---------------------------------------------------------------------------


def test_analyst_wallet_view_not_found(client):
    """Returns 404 when no scores exist for the wallet."""
    resp = client.get(f"/analyst/wallet/{'G' + 'Z' * 55}")
    assert resp.status_code == 404


def test_analyst_wallet_view_all_sections(client):
    """GET /analyst/wallet/{wallet} returns all six required data sections."""
    import detection.storage as store

    wallet = "G" + "E" * 55
    save_scores([_make_score(wallet, 80)], store.settings.db_path)

    resp = client.get(f"/analyst/wallet/{wallet}")
    assert resp.status_code == 200
    body = resp.json()

    required_sections = {
        "current_score", "shap_top_10", "trade_timeline",
        "ring_membership", "score_trend", "open_alerts",
    }
    for section in required_sections:
        assert section in body, f"Missing section: {section}"

    assert body["wallet"] == wallet
    assert body["current_score"]["score"] == 80


def test_analyst_wallet_view_invalid_address(client):
    """Returns 400 for invalid Stellar address."""
    resp = client.get("/analyst/wallet/INVALID")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Feedback submission tests
# ---------------------------------------------------------------------------


def test_submit_feedback_confirmed_wash(client):
    """POST /analyst/wallet/{wallet}/feedback accepts confirmed_wash verdict."""
    import detection.storage as store

    wallet = "G" + "F" * 55
    save_scores([_make_score(wallet, 90)], store.settings.db_path)

    resp = client.post(
        f"/analyst/wallet/{wallet}/feedback",
        json={"verdict": "confirmed_wash", "notes": "clearly wash trading", "analyst_key_hash": "abc"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["verdict"] == "confirmed_wash"
    assert body["wallet"] == wallet


def test_submit_feedback_false_positive(client):
    """POST accepts false_positive verdict."""
    import detection.storage as store

    wallet = "G" + "G" * 55
    save_scores([_make_score(wallet, 72)], store.settings.db_path)

    resp = client.post(
        f"/analyst/wallet/{wallet}/feedback",
        json={"verdict": "false_positive", "analyst_key_hash": "xyz"},
    )
    assert resp.status_code == 201
    assert resp.json()["verdict"] == "false_positive"


def test_submit_feedback_needs_review(client):
    """POST accepts needs_review verdict."""
    import detection.storage as store

    wallet = "G" + "H" * 55
    save_scores([_make_score(wallet, 65)], store.settings.db_path)

    resp = client.post(
        f"/analyst/wallet/{wallet}/feedback",
        json={"verdict": "needs_review", "analyst_key_hash": "xyz"},
    )
    assert resp.status_code == 201


def test_submit_feedback_invalid_verdict(client):
    """POST rejects unknown verdict values with 422."""
    import detection.storage as store

    wallet = "G" + "I" * 55
    save_scores([_make_score(wallet, 70)], store.settings.db_path)

    resp = client.post(
        f"/analyst/wallet/{wallet}/feedback",
        json={"verdict": "UNKNOWN_VERDICT", "analyst_key_hash": "xyz"},
    )
    assert resp.status_code == 422


def test_submit_feedback_with_review_started_at(client):
    """POST accepts review_started_at for average review time computation."""
    import detection.storage as store

    wallet = "G" + "J" * 55
    save_scores([_make_score(wallet, 80)], store.settings.db_path)

    started = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    resp = client.post(
        f"/analyst/wallet/{wallet}/feedback",
        json={
            "verdict": "confirmed_wash",
            "analyst_key_hash": "abc",
            "review_started_at": started,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["review_started_at"] is not None


# ---------------------------------------------------------------------------
# Stats tests
# ---------------------------------------------------------------------------


def test_analyst_stats_empty(client):
    """Stats endpoint returns valid structure with zero values when no data."""
    resp = client.get("/analyst/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "cases_reviewed_today" in body
    assert "false_positive_rate_30d" in body
    assert "avg_review_time_seconds" in body
    assert body["cases_reviewed_today"] == 0
    assert body["false_positive_rate_30d"] == 0.0


def test_analyst_stats_cases_reviewed_today(client):
    """cases_reviewed_today increments with each feedback submission today."""
    import detection.storage as store

    for letter in ["K", "L", "M"]:
        w = "G" + letter * 55
        save_scores([_make_score(w, 80)], store.settings.db_path)
        client.post(
            f"/analyst/wallet/{w}/feedback",
            json={"verdict": "confirmed_wash", "analyst_key_hash": "abc"},
        )

    resp = client.get("/analyst/stats")
    assert resp.status_code == 200
    assert resp.json()["cases_reviewed_today"] >= 3


def test_analyst_stats_false_positive_rate(client):
    """false_positive_rate_30d is computed correctly."""
    import detection.storage as store

    for letter, verdict in [("N", "false_positive"), ("O", "confirmed_wash"), ("P", "false_positive")]:
        w = "G" + letter * 55
        save_scores([_make_score(w, 80)], store.settings.db_path)
        client.post(
            f"/analyst/wallet/{w}/feedback",
            json={"verdict": verdict, "analyst_key_hash": "abc"},
        )

    resp = client.get("/analyst/stats")
    # 2 fp / 3 total = ~0.667
    rate = resp.json()["false_positive_rate_30d"]
    assert abs(rate - 2 / 3) < 0.01, f"Expected ~0.667, got {rate}"


# ---------------------------------------------------------------------------
# Active learning feedback export tests
# ---------------------------------------------------------------------------


def test_feedback_export_requires_since_param(client):
    """GET /analyst/feedback requires the `since` query parameter."""
    resp = client.get("/analyst/feedback")
    assert resp.status_code == 422


def test_feedback_export_empty_before_any_feedback(client):
    """GET /analyst/feedback returns empty list when no feedback exists."""
    since = "2020-01-01T00:00:00Z"
    resp = client.get(f"/analyst/feedback?since={since}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_feedback_export_returns_records_since_timestamp(client):
    """GET /analyst/feedback returns only records submitted after `since`."""
    import detection.storage as store
    from detection.analyst_store import submit_analyst_feedback

    wallet = "G" + "Q" * 55
    save_scores([_make_score(wallet, 90)], store.settings.db_path)

    submit_analyst_feedback(
        wallet=wallet,
        asset_pair="XLM/USDC",
        verdict="confirmed_wash",
        notes=None,
        analyst_key_hash="abc",
    )

    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    resp = client.get(f"/analyst/feedback?since={since}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    assert any(r["wallet"] == wallet for r in body)


def test_feedback_export_invalid_since_returns_422(client):
    """GET /analyst/feedback with bad timestamp returns 422."""
    resp = client.get("/analyst/feedback?since=not-a-date")
    assert resp.status_code == 422
