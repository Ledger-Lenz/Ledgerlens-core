"""Tests for detection/alert_engine.py — alert deduplication logic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from detection.alert_engine import AlertDeduplicator, _ensure_tables


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "ledgerlens.db")


class TestEnsureTables:
    def test_creates_both_tables(self, db_path: str):
        conn = sqlite3.connect(db_path)
        _ensure_tables(conn)
        conn.commit()

        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "alert_states" in table_names
        assert "alert_events" in table_names

    def test_idempotent(self, db_path: str):
        conn = sqlite3.connect(db_path)
        _ensure_tables(conn)
        conn.commit()
        _ensure_tables(conn)  # must not raise
        conn.commit()
        conn.close()


class TestAlertDeduplicator:
    def test_opens_alert_when_score_crosses_threshold(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        events = dedup.process("GOPEN" + "A" * 51, 75.0)
        assert len(events) == 1
        assert events[0]["event_type"] == "alert.opened"
        assert events[0]["score"] == 75.0

    def test_no_event_when_score_below_threshold(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        events = dedup.process("GSAFE" + "A" * 51, 60.0)
        assert events == []

    def test_resolves_after_hysteresis(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        wallet = "GRESOLVE" + "A" * 48

        # Open
        events = dedup.process(wallet, 80.0)
        assert events[0]["event_type"] == "alert.opened"

        # Three consecutive below-threshold observations
        for _ in range(2):
            events = dedup.process(wallet, 50.0)
            assert events == []
        events = dedup.process(wallet, 50.0)
        assert len(events) == 1
        assert events[0]["event_type"] == "alert.resolved"

    def test_escalates_on_large_score_jump(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        wallet = "GESCAL" + "A" * 50

        # Open alert
        dedup.process(wallet, 75.0)

        # Jump by 11 (> ESCALATION_DELTA of 10)
        events = dedup.process(wallet, 86.0)
        assert len(events) == 1
        assert events[0]["event_type"] == "alert.escalated"

    def test_no_escalation_for_small_jump(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        wallet = "GNOESC" + "A" * 50

        dedup.process(wallet, 75.0)
        events = dedup.process(wallet, 84.0)  # jump of 9, below delta of 10
        assert all(e["event_type"] != "alert.escalated" for e in events)

    def test_below_threshold_streak_resets_on_above(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        wallet = "GRESET" + "A" * 50

        dedup.process(wallet, 80.0)  # open

        # Two below-threshold observations
        dedup.process(wallet, 50.0)
        dedup.process(wallet, 50.0)
        # One above — should reset the resolution streak
        events = dedup.process(wallet, 80.0)
        assert all(e["event_type"] != "alert.resolved" for e in events)

    def test_get_state_returns_none_for_unknown_wallet(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path)
        assert dedup.get_state("GUNKNOWN" + "A" * 48) is None

    def test_get_state_returns_active_after_open(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        wallet = "GSTATE" + "A" * 50
        dedup.process(wallet, 80.0)
        state = dedup.get_state(wallet)
        assert state is not None
        assert state["alert_active"] == 1

    def test_get_events_returns_history(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        wallet = "GEVENTS" + "A" * 49
        dedup.process(wallet, 80.0)
        dedup.process(wallet, 60.0)
        dedup.process(wallet, 60.0)
        dedup.process(wallet, 60.0)

        events = dedup.get_events(wallet)
        assert len(events) >= 2  # opened + resolved

    def test_multiple_wallets_independent(self, db_path: str):
        dedup = AlertDeduplicator(db_path=db_path, threshold=70)
        w1 = "GW1" + "A" * 53
        w2 = "GW2" + "A" * 53

        e1 = dedup.process(w1, 80.0)
        e2 = dedup.process(w2, 60.0)

        assert e1[0]["event_type"] == "alert.opened"
        assert e2 == []

    def test_constructs_with_default_threshold_from_settings(self, db_path: str):
        # When threshold is not explicitly passed, it reads from runtime config
        dedup = AlertDeduplicator(db_path=db_path)
        assert dedup._threshold == 70  # default from settings
