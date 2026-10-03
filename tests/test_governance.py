"""Tests for the governance proposal engine — Issue #150."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config.settings as settings_module
from detection.governance import (
    GovernanceApprovalError,
    GovernanceEngine,
    GovernanceError,
    GovernanceVoteError,
    Proposal,
    SettingsReloader,
    Vote,
    _connect,
)


@pytest.fixture(autouse=True)
def _restore_risk_score_threshold():
    """Save/restore governance-mutable global state around every test.

    Before the config-propagation fix, `SettingsReloader.apply()`'s in-process
    live-apply silently no-op'd (the `_default_risk_score_threshold` property
    had no setter), so `TestSettingsReloader`'s real (unmocked) `.apply()`
    calls below never actually touched the global `settings` singleton. Now
    that the setter works, those calls durably mutate `settings.risk_score_
    threshold` for the rest of the pytest session unless restored.

    Also invalidates `config.settings`'s module-level `_default_runtime_cache`
    on both sides: it's a wall-clock-TTL cache keyed by nothing but time, not
    `db_path`, so a `runtime_config` write in one test (this file writes to a
    fresh `tmp_path` db per test) can otherwise leak a stale cached value into
    an unrelated test elsewhere in the suite that happens to run within the
    same ~60s TTL window and also calls `get_runtime_risk_score_threshold()`.
    """
    settings_module.invalidate_runtime_config_cache()
    original = settings_module.settings.risk_score_threshold
    yield
    object.__setattr__(settings_module.settings, "risk_score_threshold", original)
    settings_module.invalidate_runtime_config_cache()


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


def _sign_off_config(engine, pid, risk_owner="bob", compliance="carol"):
    engine.record_signoff(pid, risk_owner, "risk_owner", {"impact_analysis": "reports/backtest.json"})
    engine.record_signoff(pid, compliance, "compliance_officer")


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
        _sign_off_config(engine, p.id)
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

        _sign_off_config(engine, p.id)
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

    def test_live_apply_actually_changes_in_process_settings(self, tmp_path):
        """Direct repro/confirmation of the object.__setattr__ crash fix.

        `_default_risk_score_threshold` (config/settings.py) used to be a
        read-only `@property` with no setter -- `object.__setattr__(settings,
        "_default_risk_score_threshold", parsed)` raised `AttributeError`,
        silently swallowed by `apply()`'s `except (AttributeError, TypeError):
        pass`, so the executing process's own live settings value never
        changed. This asserts the live in-process value actually changes
        immediately after `apply()` runs -- no restart, no other process
        involved.
        """
        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            original = settings_module.settings.risk_score_threshold
            assert original != 92, "test fixture assumption: default isn't already 92"

            with patch("detection.governance._connect") as mc:
                mc.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mc.return_value.__exit__ = MagicMock(return_value=False)
                SettingsReloader().apply("RISK_SCORE_THRESHOLD", "92")

            assert settings_module.settings.risk_score_threshold == 92
            assert settings_module.settings._default_risk_score_threshold == 92
        finally:
            os.chdir(orig)


# ---------------------------------------------------------------------------
# Change approval workflow (required sign-offs + evidence)
# ---------------------------------------------------------------------------

class TestApprovalWorkflow:
    def _passed(self, db_path, proposal_type="config_change", payload=None):
        _add_members(db_path, "alice", "bob", "carol", "dave")
        engine = GovernanceEngine(db_path=db_path, settings_reloader=MagicMock(spec=SettingsReloader))
        payload = payload or {"key": "RISK_SCORE_THRESHOLD", "new_value": "75"}
        p = engine.submit_proposal("alice", proposal_type, payload)
        engine.cast_vote(p.id, "dave", "for")
        _force_status(db_path, p.id, "passed")
        return engine, p.id

    def test_passed_vote_without_signoffs_is_blocked(self, db_path):
        engine, pid = self._passed(db_path)
        with pytest.raises(GovernanceApprovalError, match="risk_owner"):
            engine.execute_proposal(pid)
        # Blocked, not failed: the change can proceed once approvals land.
        assert engine.approval_chain(pid)["status"] == "passed"
        engine._reloader.apply.assert_not_called()

    def test_missing_evidence_is_blocked(self, db_path):
        engine, pid = self._passed(db_path)
        engine.record_signoff(pid, "bob", "risk_owner")
        engine.record_signoff(pid, "carol", "compliance_officer")
        with pytest.raises(GovernanceApprovalError, match="impact_analysis"):
            engine.execute_proposal(pid)

    def test_partial_signoffs_are_blocked(self, db_path):
        engine, pid = self._passed(db_path)
        engine.record_signoff(pid, "bob", "risk_owner", {"impact_analysis": "r.json"})
        with pytest.raises(GovernanceApprovalError, match="compliance_officer"):
            engine.execute_proposal(pid)

    def test_proposer_cannot_sign_off_own_change(self, db_path):
        engine, pid = self._passed(db_path)
        with pytest.raises(GovernanceApprovalError, match="proposer"):
            engine.record_signoff(pid, "alice", "risk_owner", {"impact_analysis": "r.json"})

    def test_one_signer_cannot_fill_two_roles(self, db_path):
        engine, pid = self._passed(db_path)
        engine.record_signoff(pid, "bob", "risk_owner", {"impact_analysis": "r.json"})
        with pytest.raises(GovernanceApprovalError):
            engine.record_signoff(pid, "bob", "compliance_officer")

    def test_non_member_and_unknown_role_rejected(self, db_path):
        engine, pid = self._passed(db_path)
        with pytest.raises(GovernanceApprovalError, match="committee"):
            engine.record_signoff(pid, "mallory", "risk_owner")
        with pytest.raises(GovernanceApprovalError, match="approval policy"):
            engine.record_signoff(pid, "bob", "model_owner")

    def test_executed_change_is_traceable_to_recorded_chain(self, db_path):
        engine, pid = self._passed(db_path)
        _sign_off_config(engine, pid)
        assert engine.execute_proposal(pid).status == "executed"

        record = engine.live_change_approval("config_change", "RISK_SCORE_THRESHOLD")
        assert record is not None
        chain = record["approval_chain"]
        assert chain["proposal_id"] == pid
        assert chain["proposer"] == "alice"
        assert chain["status"] == "executed"
        assert {so["role"]: so["signer"] for so in chain["signoffs"]} == {
            "risk_owner": "bob",
            "compliance_officer": "carol",
        }
        assert chain["missing_roles"] == [] and chain["missing_evidence"] == []
        assert [v["voter"] for v in chain["votes"]] == ["dave"]
        assert len(record["chain_sha256"]) == 64

        with pytest.raises(GovernanceApprovalError, match="closed"):
            engine.record_signoff(pid, "dave", "risk_owner")

    def test_model_promotion_requires_robustness_and_red_team_evidence(self, db_path, tmp_path, monkeypatch):
        import config.settings as settings_module

        (tmp_path / "xgboost_v202606010000.joblib").write_bytes(b"")
        monkeypatch.setattr(settings_module.settings, "model_dir", str(tmp_path), raising=False)
        engine, pid = self._passed(
            db_path, "model_promotion", {"model_name": "xgboost", "version": "202606010000"}
        )
        engine.record_signoff(pid, "bob", "model_owner", {"backtest_report": "bt.json"})
        engine.record_signoff(pid, "carol", "independent_validator", {"robustness_report": "rb.json"})
        engine.record_signoff(pid, "dave", "compliance_officer")
        with pytest.raises(GovernanceApprovalError, match="red_team_report"):
            engine.execute_proposal(pid)

    def test_model_promotion_executes_with_full_approval(self, db_path, tmp_path, monkeypatch):
        import config.settings as settings_module

        (tmp_path / "xgboost_v202606010000.joblib").write_bytes(b"")
        monkeypatch.setattr(settings_module.settings, "model_dir", str(tmp_path), raising=False)
        engine, pid = self._passed(
            db_path, "model_promotion", {"model_name": "xgboost", "version": "202606010000"}
        )
        engine.record_signoff(pid, "bob", "model_owner", {"backtest_report": "bt.json"})
        engine.record_signoff(
            pid, "carol", "independent_validator", {"robustness_report": "rb.json", "red_team_report": "rt.json"}
        )
        engine.record_signoff(pid, "dave", "compliance_officer")
        assert engine.execute_proposal(pid).status == "executed"
        assert (tmp_path / "xgboost_latest.txt").read_text() == "202606010000"
        assert engine.live_change_approval("model_promotion", "xgboost")["approval_chain"]["proposal_id"] == pid

    def test_model_promotion_rejects_unsafe_model_name(self, db_path):
        _add_members(db_path, "alice")
        engine = _make_engine(db_path)
        with pytest.raises(GovernanceError):
            engine.submit_proposal("alice", "model_promotion", {"model_name": "../etc", "version": "1"})
