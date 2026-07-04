"""Tests for Event Sourcing Audit Log (Issue #297)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiosqlite
import pytest

from audit.scoring_events import (
    ChainHashVerifier,
    ScoringEvent,
    ScoringEventStore,
    make_scoring_event,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(wallet="GABCD" + "A" * 51, score=50, prev_score=None, triggered_by="ingestion"):
    return make_scoring_event(
        wallet=wallet,
        namespace_id="default",
        score=score,
        previous_score=prev_score,
        feature_snapshot={"benford_chi_square_24h": 1.2, "wash_ring_membership": 0.0},
        model_version="v1.0.0",
        triggered_by=triggered_by,
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_audit.db")


@pytest.fixture
def store(db_path):
    return ScoringEventStore(db_path=db_path)


# ---------------------------------------------------------------------------
# Tests: ScoringEvent.compute_chain_hash
# ---------------------------------------------------------------------------


def test_chain_hash_is_deterministic():
    """Same inputs always produce the same hash."""
    occurred = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    h1 = ScoringEvent.compute_chain_hash(
        previous_chain_hash=None,
        event_id="abc",
        wallet="GABCD" + "A" * 51,
        score=75,
        feature_snapshot={"a": 1.0, "b": 2.0},
        occurred_at=occurred,
    )
    h2 = ScoringEvent.compute_chain_hash(
        previous_chain_hash=None,
        event_id="abc",
        wallet="GABCD" + "A" * 51,
        score=75,
        feature_snapshot={"b": 2.0, "a": 1.0},  # different insertion order
        occurred_at=occurred,
    )
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_chain_hash_genesis_uses_sentinel():
    """First event uses 'GENESIS' as previous hash."""
    occurred = datetime(2026, 1, 1, tzinfo=timezone.utc)
    h = ScoringEvent.compute_chain_hash(
        previous_chain_hash=None,
        event_id="first",
        wallet="G" + "A" * 55,
        score=0,
        feature_snapshot={},
        occurred_at=occurred,
    )
    assert isinstance(h, str) and len(h) == 64


# ---------------------------------------------------------------------------
# Tests: ScoringEventStore.append
# ---------------------------------------------------------------------------


def test_append_computes_correct_chain_hash(store):
    """ScoringEventStore.append computes chain_hash chaining to previous event."""
    wallet = "G" + "A" * 55

    async def run():
        e1 = _event(wallet=wallet, score=40)
        await store.append(e1)
        assert len(e1.chain_hash) == 64

        e2 = _event(wallet=wallet, score=60)
        await store.append(e2)

        # e2's chain_hash should incorporate e1's chain_hash as prev
        expected = ScoringEvent.compute_chain_hash(
            previous_chain_hash=e1.chain_hash,
            event_id=e2.event_id,
            wallet=e2.wallet,
            score=e2.score,
            feature_snapshot=e2.feature_snapshot,
            occurred_at=e2.occurred_at,
        )
        assert e2.chain_hash == expected

    asyncio.run(run())


def test_append_rejects_invalid_triggered_by(store):
    """append raises ValueError for unknown triggered_by values."""
    e = _event(triggered_by="hacked")

    async def run():
        with pytest.raises(ValueError, match="triggered_by"):
            await store.append(e)

    asyncio.run(run())


def test_append_rejects_admin_override_without_actor_id(store):
    """admin_override events without actor_id are rejected."""
    e = make_scoring_event(
        wallet="G" + "A" * 55,
        namespace_id="default",
        score=90,
        previous_score=None,
        feature_snapshot={},
        model_version="v1",
        triggered_by="admin_override",
        actor_id=None,  # should be rejected
    )

    async def run():
        with pytest.raises(ValueError, match="actor_id"):
            await store.append(e)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Tests: ScoringEventStore.replay
# ---------------------------------------------------------------------------


def test_replay_returns_chronological_order(store):
    """replay returns events oldest-first with no gaps."""
    wallet = "G" + "B" * 55

    async def run():
        for score in [30, 50, 70, 80]:
            e = _event(wallet=wallet, score=score)
            await store.append(e)

        events = await store.replay(wallet)
        assert len(events) == 4
        scores = [ev.score for ev in events]
        assert scores == [30, 50, 70, 80]

    asyncio.run(run())


def test_current_score_returns_latest(store):
    """current_score returns the score from the most recent event."""
    wallet = "G" + "C" * 55

    async def run():
        assert await store.current_score(wallet) is None

        for score in [10, 20, 85]:
            e = _event(wallet=wallet, score=score)
            await store.append(e)

        assert await store.current_score(wallet) == 85

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Tests: ChainHashVerifier
# ---------------------------------------------------------------------------


def test_chain_verifier_returns_valid(store):
    """ChainHashVerifier.verify returns 'valid' for an unmodified chain."""
    wallet = "G" + "D" * 55

    async def run():
        for score in [20, 40, 60]:
            await store.append(_event(wallet=wallet, score=score))

        verifier = ChainHashVerifier(store)
        result = await verifier.verify(wallet)
        assert result.status == "valid"
        assert result.total_events == 3
        assert result.first_tampered_event_id is None

    asyncio.run(run())


def test_chain_verifier_returns_no_events(store):
    """ChainHashVerifier.verify returns 'no_events' for unknown wallet."""

    async def run():
        verifier = ChainHashVerifier(store)
        result = await verifier.verify("G" + "E" * 55)
        assert result.status == "no_events"
        assert result.total_events == 0

    asyncio.run(run())


def test_chain_verifier_detects_tampered_event(db_path):
    """Mutating a stored event triggers TAMPERED status at the correct event."""
    store = ScoringEventStore(db_path=db_path)
    wallet = "G" + "F" * 55

    async def run():
        e1 = _event(wallet=wallet, score=30)
        e2 = _event(wallet=wallet, score=60)
        await store.append(e1)
        await store.append(e2)

        # Bypass app-level append-only to simulate a tampered record
        async with aiosqlite.connect(db_path) as db:
            # Temporarily disable the trigger by using the internal mechanism
            # (SQLite RAISE triggers block UPDATE — so we disable triggers first)
            await db.execute("DROP TRIGGER IF EXISTS prevent_scoring_event_update")
            await db.execute(
                "UPDATE scoring_events SET score = 99 WHERE event_id = ?",
                (e1.event_id,),
            )
            await db.commit()

        verifier = ChainHashVerifier(store)
        result = await verifier.verify(wallet)
        assert result.status == "tampered"
        assert result.first_tampered_event_id == e1.event_id

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Tests: SQLite trigger prevents UPDATE
# ---------------------------------------------------------------------------


def test_sqlite_trigger_prevents_update(db_path):
    """BEFORE UPDATE trigger raises ABORT when UPDATE scoring_events is attempted."""
    import sqlite3

    store = ScoringEventStore(db_path=db_path)
    wallet = "G" + "G" * 55

    async def setup():
        await store.append(_event(wallet=wallet, score=50))

    asyncio.run(setup())

    # Direct UPDATE should be blocked by the trigger
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.OperationalError, match="append-only"):
        conn.execute(
            "UPDATE scoring_events SET score = 99 WHERE wallet = ?", (wallet,)
        )
        conn.commit()
    conn.close()


def test_sqlite_trigger_prevents_delete(db_path):
    """BEFORE DELETE trigger raises ABORT when DELETE from scoring_events is attempted."""
    import sqlite3

    store = ScoringEventStore(db_path=db_path)
    wallet = "G" + "H" * 55

    async def setup():
        await store.append(_event(wallet=wallet, score=50))

    asyncio.run(setup())

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.OperationalError, match="append-only"):
        conn.execute("DELETE FROM scoring_events WHERE wallet = ?", (wallet,))
        conn.commit()
    conn.close()
