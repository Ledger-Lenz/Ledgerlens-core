"""Tests for the governance proposal engine — Issue #150."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from detection.governance import (
    GovernanceEngine,
    GovernanceError,
    GovernanceVoteError,
    Proposal,
    SettingsReloader,
    Vote,
    _connect,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_gov.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE governance_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            proposer TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            submitted_at TIMESTAMP NOT NULL,
            voting_ends_at TIMESTAMP NOT NULL,
            executed_at TIMESTAMP,
            execution_error TEXT
        );
        CREATE TABLE governance_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            voter TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('for','against','abstain')),
            cast_at TIMESTAMP NOT NULL,
            UNIQUE(proposal_id, voter)
        );
        CREATE TABLE governance_committee (
            member TEXT PRIMARY KEY,
            added_at TIMESTAMP NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return path


def _make_engine(db_path, now_fn=None, reloader=None):
    return GovernanceEngine(db_path=db_path, settings_reloader=reloader or SettingsReloader(), _now_fn=now_fn)


def _add_members(db_path, *members):
    conn = sqlite3.connect(db_path)
    for m in members:
        conn.execute(
            "INSERT OR IGNORE INTO governance_committee (member, added_at, active) VALUES (?,?,1)",
            (m, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    conn.close()


def _force_status(db_path, pid, status):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE governance_proposals SET status=? WHERE id=?", (status, pid))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Unit: submit_proposal
# ---------------------------------------------------------------------------

class TestSubmitProposal:
    def test_non_committee_proposer_raises(self, db_path):
        engine = _make_engine(db_path)
        with pytest.raises(GovernanceError, match="not an active committee member"):
            engine.submit_proposal("nobody", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"})

    def test_valid_proposer_returns_proposal_with_72h_window(self, db_path):
        _add_members(db_path, "alice")
        engine = _make_engine(db_path)
        p = engine.submit_proposal("alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"})
        assert isinstance(p, Proposal)
        assert p.status == "active"
        assert abs((p.voting_ends_at - p.submitted_at).total_seconds() - 72 * 3600) < 2

    def test_invalid_proposal_type_raises(self, db_path):
        _add_members(db_path, "alice")
        with pytest.raises(GovernanceError):
            _make_engine(db_path).submit_proposal("alice", "nuke_everything", {})

    def test_disallowed_config_key_raises(self, db_path):
        """Governance proposal to change LEDGERLENS_SERVICE_SECRET_KEY → GovernanceError before any write."""
        _add_members(db_path, "alice")
        with pytest.raises(GovernanceError, match="not modifiable via governance"):
            _make_engine(db_path).submit_proposal(
                "alice", "config_change", {"key": "LEDGERLENS_SERVICE_SECRET_KEY", "new_value": "x"}
            )


# ---------------------------------------------------------------------------
# Unit: cast_vote
# ---------------------------------------------------------------------------

class TestCastVote:
    def test_non_member_voter_raises(self, db_path):
        _add_members(db_path, "alice")
        engine = _make_engine(db_path)
        p = engine.submit_proposal("alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"})
        with pytest.raises(GovernanceVoteError, match="not an active committee member"):
            engine.cast_vote(p.id, "nobody", "for")

    def test_expired_proposal_vote_raises(self, db_path):
        _add_members(db_path, "alice", "bob")
        # Submit proposal far enough in the past that voting_ends_at (now+72h from then) is also past
        past = datetime.now(timezone.utc) - timedelta(hours=73)
        p = _make_engine(db_path, now_fn=lambda: past).submit_proposal(
            "alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"}
        )
        with pytest.raises(GovernanceVoteError, match="expired"):
            _make_engine(db_path).cast_vote(p.id, "bob", "for")

    def test_duplicate_vote_raises(self, db_path):
        _add_members(db_path, "alice", "bob")
        engine = _make_engine(db_path)
        p = engine.submit_proposal("alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"})
        engine.cast_vote(p.id, "bob", "for")
        with pytest.raises(GovernanceVoteError, match="already voted"):
            engine.cast_vote(p.id, "bob", "for")

    def test_valid_vote_returned(self, db_path):
        _add_members(db_path, "alice", "bob")
        engine = _make_engine(db_path)
        p = engine.submit_proposal("alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"})
        vote = engine.cast_vote(p.id, "bob", "for")
        assert isinstance(vote, Vote) and vote.decision == "for"


# ---------------------------------------------------------------------------
# Unit: tally_proposal quorum
# ---------------------------------------------------------------------------

class TestTallyProposal:
    def _setup(self, db_path, n_members, n_for):
        members = [f"m{i}" for i in range(n_members)]
        _add_members(db_path, *members)
        engine = _make_engine(db_path)
        p = engine.submit_proposal(members[0], "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"})
        for i in range(1, 1 + n_for):
            engine.cast_vote(p.id, members[i], "for")
        return p.id, engine

    def test_5_members_3_for_quorum_not_met(self, db_path):
        """5-member committee: quorum_required=floor(5/2)+1=3; 3 for → quorum met."""
        pid, engine = self._setup(db_path, 5, 3)
        tally = engine.tally_proposal(pid)
        assert tally.quorum_required == 3
        assert tally.quorum_met  # 3 >= 3

    def test_5_members_2_for_quorum_not_met(self, db_path):
        pid, engine = self._setup(db_path, 5, 2)
        tally = engine.tally_proposal(pid)
        assert not tally.quorum_met
        assert tally.outcome == "rejected"

    def test_tally_does_not_change_status(self, db_path):
        pid, engine = self._setup(db_path, 4, 3)
        engine.tally_proposal(pid)
        conn = sqlite3.connect(db_path)
        status = conn.execute("SELECT status FROM governance_proposals WHERE id=?", (pid,)).fetchone()[0]
        conn.close()
        assert status == "active"


# ---------------------------------------------------------------------------
# Unit: close_expired
# ---------------------------------------------------------------------------

class TestCloseExpired:
    def test_past_deadline_closed(self, db_path):
        _add_members(db_path, "alice")
        past = datetime.now(timezone.utc) - timedelta(hours=73)
        p = _make_engine(db_path, now_fn=lambda: past).submit_proposal(
            "alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"}
        )
        closed = _make_engine(db_path).close_expired()
        assert any(c.id == p.id for c in closed)
        assert all(c.status in ("passed", "rejected") for c in closed)

    def test_within_deadline_not_closed(self, db_path):
        _add_members(db_path, "alice")
        p = _make_engine(db_path).submit_proposal(
            "alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"}
        )
        closed = _make_engine(db_path).close_expired()
        assert not any(c.id == p.id for c in closed)

    def test_close_expired_idempotent(self, db_path):
        _add_members(db_path, "alice")
        past = datetime.now(timezone.utc) - timedelta(hours=73)
        _make_engine(db_path, now_fn=lambda: past).submit_proposal(
            "alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"}
        )
        engine = _make_engine(db_path)
        engine.close_expired()
        engine.close_expired()  # must not raise


# ---------------------------------------------------------------------------
# Unit: execute_proposal
# ---------------------------------------------------------------------------

class TestExecuteProposal:
    def _passed_config_proposal(self, db_path, key="RISK_SCORE_THRESHOLD", value="75"):
        _add_members(db_path, "alice", "bob", "carol")
        engine = _make_engine(db_path)
        p = engine.submit_proposal("alice", "config_change", {"key": key, "new_value": value})
        engine.cast_vote(p.id, "bob", "for")
        engine.cast_vote(p.id, "carol", "for")
        _force_status(db_path, p.id, "passed")
        return p.id

    def test_config_change_calls_reloader(self, db_path):
        """Mock SettingsReloader.apply; assert called with correct key/value."""
        pid = self._passed_config_proposal(db_path)
        mock_reloader = MagicMock(spec=SettingsReloader)
        engine = GovernanceEngine(db_path=db_path, settings_reloader=mock_reloader)
        p = engine.execute_proposal(pid)
        mock_reloader.apply.assert_called_once_with("RISK_SCORE_THRESHOLD", "75")
        assert p.status == "executed"

    def test_execute_failure_sets_failed(self, db_path):
        """Mock reloader raising ValueError → status='failed', execution_error populated."""
        pid = self._passed_config_proposal(db_path)
        mock_reloader = MagicMock(spec=SettingsReloader)
        mock_reloader.apply.side_effect = ValueError("bad value")
        p = GovernanceEngine(db_path=db_path, settings_reloader=mock_reloader).execute_proposal(pid)
        assert p.status == "failed"
        assert "bad value" in (p.execution_error or "")

    def test_execute_non_passed_raises(self, db_path):
        _add_members(db_path, "alice")
        engine = _make_engine(db_path)
        p = engine.submit_proposal("alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"})
        with pytest.raises(GovernanceError, match="cannot be executed"):
            engine.execute_proposal(p.id)


# ---------------------------------------------------------------------------
# Integration: full lifecycle
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    def test_submit_vote_tally_execute_sequence(self, db_path):
        """submit → cast 3 votes → tally → execute; status: active→passed→executed."""
        _add_members(db_path, "alice", "bob", "carol", "dave")
        mock_reloader = MagicMock(spec=SettingsReloader)
        engine = GovernanceEngine(db_path=db_path, settings_reloader=mock_reloader)

        p = engine.submit_proposal("alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "80"})
        assert p.status == "active"

        engine.cast_vote(p.id, "bob", "for")
        engine.cast_vote(p.id, "carol", "for")
        engine.cast_vote(p.id, "dave", "for")

        tally = engine.tally_proposal(p.id)
        assert tally.quorum_met

        p = engine.close_proposal(p.id)
        assert p.status == "passed"

        p = engine.execute_proposal(p.id)
        assert p.status == "executed"
        mock_reloader.apply.assert_called_once_with("RISK_SCORE_THRESHOLD", "80")


# ---------------------------------------------------------------------------
# SettingsReloader
# ---------------------------------------------------------------------------

class TestSettingsReloader:
    def test_secret_key_rejected(self):
        with pytest.raises(GovernanceError, match="not modifiable via governance"):
            SettingsReloader().apply("LEDGERLENS_SERVICE_SECRET_KEY", "x")

    def test_admin_key_rejected(self):
        with pytest.raises(GovernanceError, match="not modifiable via governance"):
            SettingsReloader().apply("LEDGERLENS_ADMIN_API_KEY", "x")

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            SettingsReloader().apply("RISK_SCORE_THRESHOLD", "not_a_number")

    def test_atomic_write(self, tmp_path):
        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("detection.governance._connect") as mc:
                mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mc.return_value.__exit__ = MagicMock(return_value=False)
                SettingsReloader().apply("RISK_SCORE_THRESHOLD", "85")
            content = (tmp_path / ".env").read_text()
            assert "RISK_SCORE_THRESHOLD=85" in content
        finally:
            os.chdir(orig)

    def test_existing_key_updated_not_duplicated(self, tmp_path):
        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            (tmp_path / ".env").write_text("RISK_SCORE_THRESHOLD=70\n")
            with patch("detection.governance._connect") as mc:
                mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mc.return_value.__exit__ = MagicMock(return_value=False)
                SettingsReloader().apply("RISK_SCORE_THRESHOLD", "90")
            content = (tmp_path / ".env").read_text()
            assert "RISK_SCORE_THRESHOLD=90" in content
            assert content.count("RISK_SCORE_THRESHOLD=") == 1
        finally:
            os.chdir(orig)
