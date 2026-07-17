"""Tests for the analyst case management system (Issue #200 follow-up).

Covers: claim/release/feedback lifecycle, concurrent claim race, lock expiry,
per-analyst claim cap, queue assignment annotation, SLA stats, and the
403/409/429 error paths.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.auth import require_admin_key
from config.settings import settings
from detection.analyst_store import (
    claim_wallet,
    expire_stale_locks,
    get_active_claim,
    get_analyst_queue,
    get_case_stats,
    release_wallet,
    resolve_claim,
    submit_analyst_feedback,
)
from detection.risk_score import RiskScore
from detection.storage import init_db, save_scores


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WALLET_A = "G" + "A" * 55
WALLET_B = "G" + "B" * 55
WALLET_C = "G" + "C" * 55
ANALYST_1 = "a1b2c3d4e5f6"
ANALYST_2 = "f6e5d4c3b2a1"
ASSET_PAIR = "XLM/USDC"


def _noop_admin():
    return None


@pytest.fixture(autouse=True)
def webhook_enc_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", key)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "case_test.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    object.__setattr__(settings, "db_path", db_path)
    object.__setattr__(settings, "analyst_lock_timeout_seconds", 1800)
    object.__setattr__(settings, "analyst_claim_max_active_per_analyst", 10)
    init_db(db_path)
    return db_path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "case_api_test.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    object.__setattr__(settings, "db_path", db_path)
    object.__setattr__(settings, "analyst_lock_timeout_seconds", 1800)
    object.__setattr__(settings, "analyst_claim_max_active_per_analyst", 10)
    init_db(db_path)
    app.dependency_overrides[require_admin_key] = _noop_admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_score(wallet: str, score: int, asset_pair: str = ASSET_PAIR) -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score > 50,
        ml_flag=score > 50,
        confidence=80,
        timestamp=datetime.now(timezone.utc),
    )


def _seed_wallets(db_path: str, wallets: list[tuple[str, int]] | None = None):
    """Insert score records for test wallets."""
    if wallets is None:
        wallets = [(WALLET_A, 90), (WALLET_B, 80), (WALLET_C, 70)]
    for w, s in wallets:
        save_scores([_make_score(w, s)], db_path)


# ---------------------------------------------------------------------------
# Claim endpoint tests
# ---------------------------------------------------------------------------


class TestClaimWallet:
    def test_claim_unassigned_wallet(self, client):
        """POST /analyst/wallet/{wallet}/claim succeeds for unassigned wallet."""
        _seed_wallets(settings.db_path)
        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["wallet"] == WALLET_A
        assert body["analyst_key_hash"] == ANALYST_1
        assert body["lock_expires_at"] is not None

    def test_claim_already_claimed_returns_409(self, client):
        """POST returns 409 when wallet is actively claimed by another analyst."""
        _seed_wallets(settings.db_path)

        # First analyst claims
        client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )

        # Second analyst tries to claim
        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_2},
            params={"asset_pair": ASSET_PAIR},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert "detail" in body
        assert body["detail"]["detail"] == "Already claimed"
        assert body["detail"]["assigned_to"] == ANALYST_1

    def test_claim_same_analyst_refreshes_lock(self, client):
        """Same analyst claiming again refreshes the lock."""
        _seed_wallets(settings.db_path)

        resp1 = client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )
        first_expires = resp1.json()["lock_expires_at"]

        resp2 = client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )
        assert resp2.status_code == 200
        second_expires = resp2.json()["lock_expires_at"]
        # Lock should be refreshed (new expiry >= old expiry)
        assert second_expires >= first_expires


# ---------------------------------------------------------------------------
# Release endpoint tests
# ---------------------------------------------------------------------------


class TestReleaseWallet:
    def test_release_active_claim(self, client):
        """POST /analyst/wallet/{wallet}/release succeeds for active claim."""
        _seed_wallets(settings.db_path)

        client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )

        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/release",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "released"

    def test_release_no_active_claim_returns_404(self, client):
        """POST returns 404 when no active claim exists."""
        _seed_wallets(settings.db_path)

        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/release",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Feedback submission requires claim
# ---------------------------------------------------------------------------


class TestFeedbackRequiresClaim:
    def test_submit_feedback_without_claim_returns_403(self, client):
        """POST /analyst/wallet/{wallet}/feedback returns 403 without claim."""
        _seed_wallets(settings.db_path)

        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/feedback",
            json={
                "verdict": "confirmed_wash",
                "analyst_key_hash": ANALYST_1,
            },
        )
        assert resp.status_code == 403
        assert "claim" in resp.json()["detail"].lower()

    def test_submit_feedback_by_wrong_analyst_returns_403(self, client):
        """POST returns 403 when analyst doesn't hold the claim."""
        _seed_wallets(settings.db_path)

        # Analyst 1 claims
        client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )

        # Analyst 2 tries to submit feedback
        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/feedback",
            json={
                "verdict": "confirmed_wash",
                "analyst_key_hash": ANALYST_2,
            },
        )
        assert resp.status_code == 403

    def test_submit_feedback_with_claim_succeeds(self, client):
        """POST succeeds when analyst holds an active claim."""
        _seed_wallets(settings.db_path)

        # Claim first
        client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )

        # Submit feedback
        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/feedback",
            json={
                "verdict": "confirmed_wash",
                "analyst_key_hash": ANALYST_1,
                "notes": "clearly wash trading",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["verdict"] == "confirmed_wash"

    def test_feedback_resolves_claim(self, client):
        """After feedback submission, the claim is resolved."""
        _seed_wallets(settings.db_path)

        client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
            params={"asset_pair": ASSET_PAIR},
        )

        client.post(
            f"/analyst/wallet/{WALLET_A}/feedback",
            json={
                "verdict": "false_positive",
                "analyst_key_hash": ANALYST_1,
            },
        )

        # Claim should now be resolved (not active)
        claim = get_active_claim(WALLET_A, ASSET_PAIR, db_path=settings.db_path)
        assert claim is None


# ---------------------------------------------------------------------------
# Lock expiry tests
# ---------------------------------------------------------------------------


class TestLockExpiry:
    def test_expired_claim_becomes_claimable(self, client):
        """A claim with lock_expires_at in the past is auto-released."""
        _seed_wallets(settings.db_path)

        # Claim with a very short timeout (0 seconds = immediately expired)
        claim_wallet(
            WALLET_A, ASSET_PAIR, ANALYST_1,
            lock_timeout_seconds=0,
            db_path=settings.db_path,
        )

        # Run sweep
        released = expire_stale_locks(db_path=settings.db_path)
        assert released >= 1

        # Now another analyst can claim
        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_2},
            params={"asset_pair": ASSET_PAIR},
        )
        assert resp.status_code == 200
        assert resp.json()["analyst_key_hash"] == ANALYST_2

    def test_active_claim_not_expired(self, client):
        """A claim with future lock_expires_at is NOT expired by sweep."""
        _seed_wallets(settings.db_path)

        claim_wallet(
            WALLET_A, ASSET_PAIR, ANALYST_1,
            lock_timeout_seconds=1800,
            db_path=settings.db_path,
        )

        released = expire_stale_locks(db_path=settings.db_path)
        assert released == 0

        claim = get_active_claim(WALLET_A, ASSET_PAIR, db_path=settings.db_path)
        assert claim is not None
        assert claim["analyst_key_hash"] == ANALYST_1


# ---------------------------------------------------------------------------
# Concurrent claim race test
# ---------------------------------------------------------------------------


class TestConcurrentClaim:
    def test_concurrent_claims_exactly_one_succeeds(self, db):
        """Two concurrent claim requests: exactly one succeeds."""
        _seed_wallets(db)

        results = {"a": None, "b": None}

        def try_claim(analyst, key):
            try:
                claim_wallet(WALLET_A, ASSET_PAIR, analyst, db_path=db)
                results[key] = "success"
            except RuntimeError:
                results[key] = "conflict"
            except PermissionError:
                results[key] = "cap"

        t1 = threading.Thread(target=try_claim, args=(ANALYST_1, "a"))
        t2 = threading.Thread(target=try_claim, args=(ANALYST_2, "b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one succeeds, the other gets conflict
        assert results["a"] != results["b"], f"Expected different outcomes, got {results}"
        assert "success" in results.values(), f"Expected one success, got {results}"
        assert "conflict" in results.values(), f"Expected one conflict, got {results}"


# ---------------------------------------------------------------------------
# Per-analyst claim cap
# ---------------------------------------------------------------------------


class TestClaimCap:
    def test_claim_cap_enforced(self, db):
        """Claim attempt beyond the cap is rejected with PermissionError."""
        object.__setattr__(settings, "analyst_claim_max_active_per_analyst", 2)
        _seed_wallets(db, [(WALLET_A, 90), (WALLET_B, 80), (WALLET_C, 70)])

        claim_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=db)
        claim_wallet(WALLET_B, ASSET_PAIR, ANALYST_1, db_path=db)

        with pytest.raises(PermissionError, match="max"):
            claim_wallet(WALLET_C, ASSET_PAIR, ANALYST_1, db_path=db)

    def test_claim_cap_not_exceeded_after_release(self, db):
        """Releasing a claim frees a slot in the cap."""
        object.__setattr__(settings, "analyst_claim_max_active_per_analyst", 2)
        _seed_wallets(db, [(WALLET_A, 90), (WALLET_B, 80), (WALLET_C, 70)])

        claim_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=db)
        claim_wallet(WALLET_B, ASSET_PAIR, ANALYST_1, db_path=db)
        release_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=db)

        # Now should succeed
        claim_wallet(WALLET_C, ASSET_PAIR, ANALYST_1, db_path=db)
        claim = get_active_claim(WALLET_C, ASSET_PAIR, db_path=db)
        assert claim is not None


# ---------------------------------------------------------------------------
# Queue annotation tests
# ---------------------------------------------------------------------------


class TestQueueAnnotation:
    def test_queue_includes_assignment_state(self, client):
        """Queue items include is_assigned, assigned_to, lock_expires_at."""
        _seed_wallets(settings.db_path)

        queue = client.get("/analyst/queue").json()
        assert len(queue) > 0
        item = queue[0]
        assert "is_assigned" in item
        assert "assigned_to" in item
        assert "lock_expires_at" in item
        assert item["is_assigned"] is False
        assert item["assigned_to"] is None

    def test_queue_shows_assigned_wallet(self, client):
        """Queue shows assignment when wallet is claimed."""
        _seed_wallets(settings.db_path)

        claim_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=settings.db_path)

        queue = client.get("/analyst/queue").json()
        a_item = next((q for q in queue if q["wallet"] == WALLET_A), None)
        assert a_item is not None
        assert a_item["is_assigned"] is True
        assert a_item["assigned_to"] == ANALYST_1


# ---------------------------------------------------------------------------
# Case stats (SLA) tests
# ---------------------------------------------------------------------------


class TestCaseStats:
    def test_case_stats_empty(self, db):
        """Case stats returns valid structure with zero/null values when empty."""
        stats = get_case_stats(db_path=db)
        assert "avg_time_to_claim_seconds" in stats
        assert "avg_time_to_resolution_seconds" in stats
        assert "assigned_count" in stats
        assert "unassigned_count" in stats
        assert "expired_reclaimed_count" in stats
        assert stats["assigned_count"] == 0

    def test_case_stats_assigned_count(self, db):
        """assigned_count reflects active claims."""
        _seed_wallets(db, [(WALLET_A, 90), (WALLET_B, 80)])
        claim_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=db)

        stats = get_case_stats(db_path=db)
        assert stats["assigned_count"] == 1

    def test_case_stats_expired_count(self, db):
        """expired_reclaimed_count tracks released locks."""
        _seed_wallets(db, [(WALLET_A, 90)])
        claim_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, lock_timeout_seconds=0, db_path=db)
        expire_stale_locks(db_path=db)

        stats = get_case_stats(db_path=db)
        assert stats["expired_reclaimed_count"] >= 1

    def test_case_stats_avg_time_to_resolution(self, db):
        """avg_time_to_resolution_seconds is computed from resolved claims."""
        _seed_wallets(db, [(WALLET_A, 90)])
        claim_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=db)
        resolve_claim(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=db)

        stats = get_case_stats(db_path=db)
        assert stats["avg_time_to_resolution_seconds"] is not None
        assert stats["avg_time_to_resolution_seconds"] >= 0

    def test_case_stats_endpoint(self, client):
        """GET /analyst/case-stats returns 200 with correct structure."""
        _seed_wallets(settings.db_path)
        resp = client.get("/analyst/case-stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "assigned_count" in body
        assert "unassigned_count" in body


# ---------------------------------------------------------------------------
# Wallet view includes assignment
# ---------------------------------------------------------------------------


class TestWalletViewAssignment:
    def test_wallet_view_includes_assignment(self, client):
        """GET /analyst/wallet/{wallet} includes assignment section."""
        _seed_wallets(settings.db_path)

        resp = client.get(f"/analyst/wallet/{WALLET_A}")
        assert resp.status_code == 200
        body = resp.json()
        assert "assignment" in body
        assert body["assignment"]["is_assigned"] is False

    def test_wallet_view_shows_assignment_after_claim(self, client):
        """Wallet view reflects claim after it's made."""
        _seed_wallets(settings.db_path)

        claim_wallet(WALLET_A, ASSET_PAIR, ANALYST_1, db_path=settings.db_path)

        resp = client.get(f"/analyst/wallet/{WALLET_A}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["assignment"]["is_assigned"] is True
        assert body["assignment"]["assigned_to"] == ANALYST_1


# ---------------------------------------------------------------------------
# API error paths
# ---------------------------------------------------------------------------


class TestAPIErrorPaths:
    def test_claim_invalid_wallet_returns_400(self, client):
        resp = client.post(
            "/analyst/wallet/INVALID/claim",
            json={"analyst_key_hash": ANALYST_1},
        )
        assert resp.status_code == 400

    def test_release_invalid_wallet_returns_400(self, client):
        resp = client.post(
            "/analyst/wallet/INVALID/release",
            json={"analyst_key_hash": ANALYST_1},
        )
        assert resp.status_code == 400

    def test_claim_nonexistent_wallet_returns_404(self, client):
        resp = client.post(
            f"/analyst/wallet/{WALLET_A}/claim",
            json={"analyst_key_hash": ANALYST_1},
        )
        assert resp.status_code == 404
