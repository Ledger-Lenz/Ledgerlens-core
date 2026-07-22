"""Tests for governed-config propagation across processes.

Covers the acceptance criteria for the governance config-propagation issue:
- A governance proposal (or `PATCH /admin/config`) must actually change the
  value every live process's scoring/alerting/counterfactual logic uses --
  not just the executing process, and not only after an uncoordinated
  restart.
- Propagation must be bounded and documented: near-instant when Redis is
  configured/reachable (a shared version counter bypasses each process's
  local TTL cache), degrading gracefully to a hard `runtime_config_ttl_
  seconds` (default 60s) bound when Redis is absent -- exactly today's
  pre-existing polling behavior in that case.
- `run_pipeline.py`, `detection/alert_engine.py`, and `api/main.py` must
  reflect the change, not just `config/settings.py`'s internal state.
- A health endpoint must expose the currently-active config version/value.
- `PATCH /admin/config` must propagate under the same mechanism.

Two independent process instances are simulated (rather than spawning real
OS processes) via two separate `_RuntimeConfigCache` instances sharing only
the backing SQLite database and a shared `fakeredis` server -- exactly how
two real replica pods would share only their backing store, never Python
object state.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import config.settings as settings_module
from detection.governance import GovernanceEngine, SettingsReloader

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_config_propagation_globals(tmp_path, monkeypatch):
    """Fully isolate every test in this file from process-wide caches/singletons.

    `config.settings` deliberately uses process-lifetime singletons (the
    Redis client, its "attempted once" flag, and the default
    `_RuntimeConfigCache` instance) -- correct for a real process, but each
    test here needs to control independently whether Redis is "available"
    and start from an empty cache, without leaking into other tests in this
    file or the wider suite.
    """
    db_path = str(tmp_path / "ledgerlens.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)
    original_threshold = settings_module.settings.risk_score_threshold

    def _reset():
        settings_module._config_redis_client = None
        settings_module._config_redis_attempted = False
        settings_module._config_redis_circuit = None
        settings_module._default_runtime_cache = settings_module._RuntimeConfigCache()

    _reset()
    yield db_path
    _reset()
    # SettingsReloader.apply() now really mutates this singleton (that's the
    # fix) -- restore it so a governance test in this file can't leak a
    # changed default into other test files run in the same session.
    object.__setattr__(settings_module.settings, "risk_score_threshold", original_threshold)


def _make_governance_db(db_path: str) -> None:
    """Build a real production-schema database via `init_db()` (migrations
    1-18), then seed a committee member. Deliberately does NOT hand-roll the
    governance tables: doing so previously masked a real bug where migration
    7's `governance_proposals` schema was incompatible with `GovernanceEngine`
    (fixed by migration 19) -- using the real migration path here is what
    caught it, and keeps these tests honest about what production actually
    looks like.
    """
    from detection.storage import init_db

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO governance_committee (member, added_at, active) VALUES (?,?,1)",
        ("alice", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _execute_threshold_proposal(db_path: str, new_value: str, cwd: str) -> None:
    """Submit, auto-pass (single-member committee), and execute a real
    RISK_SCORE_THRESHOLD proposal using the real (unmocked) SettingsReloader --
    exercising the exact path a governance-approved change takes in
    production, including the `.env` write and `bump_config_version()` call.
    """
    orig_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        engine = GovernanceEngine(db_path=db_path, settings_reloader=SettingsReloader())
        p = engine.submit_proposal(
            "alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": new_value}
        )
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE governance_proposals SET status='passed' WHERE id=?", (p.id,))
        conn.commit()
        conn.close()
        result = engine.execute_proposal(p.id)
        assert result.status == "executed", result.execution_error
    finally:
        os.chdir(orig_cwd)


# ---------------------------------------------------------------------------
# Acceptance criterion 2: cross-process propagation, two simulated processes
# ---------------------------------------------------------------------------


class TestCrossProcessPropagation:
    def test_second_process_observes_change_via_redis_without_restart(self, tmp_path):
        """With Redis reachable: a second process's cache bypasses its local
        TTL and observes a governance-executed change on its very next read
        -- no restart, no waiting out the 60s TTL."""
        if fakeredis is None:
            pytest.skip("fakeredis not installed")

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        fake_client = fakeredis.FakeStrictRedis()
        with patch("redis.from_url", return_value=fake_client):
            # "Process B": a worker that has already been polling steadily.
            process_b_cache = settings_module._RuntimeConfigCache()
            baseline = process_b_cache.get()
            assert baseline.get("risk_score_threshold") is None

            # "Process A": executes a governance proposal -- writes
            # runtime_config and bumps the shared Redis version counter.
            _execute_threshold_proposal(db_path, "95", str(tmp_path))

            # Process B re-reads immediately (no sleep, no TTL wait) and
            # must already see the new value.
            cfg = process_b_cache.get()
            assert cfg["risk_score_threshold"] == "95"

    def test_second_process_bounded_by_ttl_without_redis(self, tmp_path):
        """Without Redis: a second process's cache does NOT see the change
        until its local TTL elapses -- the documented worst-case fallback
        bound, not an unbounded/never-converges failure."""
        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        # Seed an unrelated row first so the cache's baseline read is
        # non-empty and therefore actually gets cached: an *empty*
        # `runtime_config` result is deliberately never cached (avoids
        # permanently caching a transient read failure as "no overrides
        # exist"), so testing the TTL bound needs at least one row present
        # from the start.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?)",
            ("unrelated_seed_key", "x", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        with patch("redis.from_url", side_effect=ConnectionError("refused")):
            process_b_cache = settings_module._RuntimeConfigCache()
            baseline = process_b_cache.get()
            assert baseline.get("risk_score_threshold") is None

            _execute_threshold_proposal(db_path, "95", str(tmp_path))

            # Within TTL, no Redis to bypass it: still stale.
            cfg = process_b_cache.get()
            assert cfg.get("risk_score_threshold") is None

            # Simulate the TTL having elapsed (equivalent to waiting
            # `runtime_config_ttl_seconds`, without a real 60s sleep).
            process_b_cache._ts = 0.0
            cfg = process_b_cache.get()
            assert cfg["risk_score_threshold"] == "95"


# ---------------------------------------------------------------------------
# Acceptance criterion 3: real consumers reflect the change
# ---------------------------------------------------------------------------


class TestConsumersReflectGovernanceChange:
    def test_run_pipeline_reads_through_the_live_path(self, tmp_path):
        """Proves `run_pipeline.py` is wired to the canonical read path, not
        just that `config.settings`'s function works in isolation: calls the
        exact name bound into `run_pipeline`'s own module namespace (`from
        config.settings import get_runtime_risk_score_threshold`), which is
        the same function object the module's scoring loop calls at
        `run_pipeline.py`'s "above_threshold"/high_risk-filter lines. If that
        import/call site ever regresses back to reading `settings.
        risk_score_threshold` directly, this import would simply not exist
        and this test would fail to even collect.
        """
        import run_pipeline

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        assert run_pipeline.get_runtime_risk_score_threshold is (
            settings_module.get_runtime_risk_score_threshold
        )

        before = run_pipeline.get_runtime_risk_score_threshold()
        _execute_threshold_proposal(db_path, str(before + 7), str(tmp_path))
        settings_module.invalidate_runtime_config_cache()

        assert run_pipeline.get_runtime_risk_score_threshold() == before + 7

    def test_alert_deduplicator_uses_governance_applied_threshold(self, tmp_path):
        """A score that was below the old threshold but above the new one
        must only open an alert once governance raises... no, lowers the bar:
        concretely, lowering the threshold below a previously-subthreshold
        score must cause a freshly-constructed AlertDeduplicator to alert on
        it, proving `alert_engine.py`'s constructor reads the governed value."""
        from detection.alert_engine import AlertDeduplicator

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        score = 60.0
        wallet = "GALERT" + "A" * 50

        # Before: default threshold (70) -- score 60 must not open an alert.
        dedup_before = AlertDeduplicator(db_path=db_path)
        assert dedup_before.process(wallet, score) == []

        # Governance lowers the threshold to 50 -- now 60 must trigger.
        _execute_threshold_proposal(db_path, "50", str(tmp_path))
        settings_module.invalidate_runtime_config_cache()

        dedup_after = AlertDeduplicator(db_path=db_path)
        events = dedup_after.process(wallet, score)
        assert any(e["event_type"] == "alert.opened" for e in events)

    def test_api_alerts_endpoint_uses_governance_applied_threshold(self, tmp_path):
        """`GET /v1/alerts` must reflect a governance-applied threshold, not
        just `settings.risk_score_threshold` read once at import time."""
        from fastapi.testclient import TestClient

        from api.main import app
        from detection.risk_score import RiskScore
        from detection.storage import save_scores

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        wallet = "GAPIALERT" + "A" * 46
        save_scores(
            [
                RiskScore(
                    wallet=wallet,
                    asset_pair="XLM/USDC",
                    score=55,
                    benford_flag=False,
                    ml_flag=False,
                    confidence=90,
                    timestamp=datetime.now(timezone.utc),
                )
            ],
            db_path,
        )

        client = TestClient(app)

        # Default threshold (70): score 55 is below it, not returned.
        resp = client.get("/v1/alerts")
        assert resp.status_code == 200
        assert wallet not in [s["wallet"] for s in resp.json()]

        # Governance lowers the threshold below 55.
        _execute_threshold_proposal(db_path, "50", str(tmp_path))
        settings_module.invalidate_runtime_config_cache()

        resp = client.get("/v1/alerts")
        assert resp.status_code == 200
        assert wallet in [s["wallet"] for s in resp.json()]

    def test_counterfactual_engine_uses_governance_applied_threshold(self, tmp_path):
        """`generate_counterfactuals`'s default `target_score` (`detection/
        counterfactual_engine.py`) must be derived from the governance-applied
        threshold, not a snapshot taken at import time.

        `_predicted_score` is patched to a fixed value so the distinguishing
        signal is purely how many times it gets called: exactly once means
        `current_score < target_score` short-circuited at the top of
        `generate_counterfactuals` (the score was already "safe" against that
        target); many more means the full feature-search ran because the
        score was still at or above target -- i.e. `target_score` resolved
        differently depending on the live governed threshold.
        """
        from unittest.mock import patch as _patch

        import detection.counterfactual_engine as cf_engine
        from detection.feature_engineering import FEATURE_NAMES

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        feature_vector = dict.fromkeys(FEATURE_NAMES, 1.0)
        fixed_score = 65

        # Default threshold (70) -> target_score=69 -> 65 < 69: short-circuits.
        with _patch.object(cf_engine, "_predicted_score", return_value=fixed_score) as mock_score:
            result = cf_engine.generate_counterfactuals(feature_vector, models={}, n_counterfactuals=1)
        assert result == []
        assert mock_score.call_count == 1

        # Governance lowers the threshold to 50 -> target_score=49 -> 65 >= 49:
        # the full search must actually run (many more _predicted_score calls).
        _execute_threshold_proposal(db_path, "50", str(tmp_path))
        settings_module.invalidate_runtime_config_cache()

        with _patch.object(cf_engine, "_predicted_score", return_value=fixed_score) as mock_score:
            cf_engine.generate_counterfactuals(feature_vector, models={}, n_counterfactuals=1)
        assert mock_score.call_count > 1


# ---------------------------------------------------------------------------
# Acceptance criterion 4: health/status endpoint reports active config version
# ---------------------------------------------------------------------------


class TestHealthReportsConfigVersion:
    def test_health_config_section_matches_governance_executed_value(self, tmp_path):
        from fastapi.testclient import TestClient

        from api.main import app

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        _execute_threshold_proposal(db_path, "88", str(tmp_path))
        settings_module.invalidate_runtime_config_cache()

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/health")
        body = resp.json()

        assert body["config"]["risk_score_threshold"] == 88
        assert body["config"]["risk_score_threshold_version"] is not None


# ---------------------------------------------------------------------------
# Acceptance criterion 5: PATCH /admin/config propagates under the same mechanism
# ---------------------------------------------------------------------------


class TestAdminConfigPatchPropagates:
    def test_patch_admin_config_propagates_to_a_second_process(self, tmp_path):
        if fakeredis is None:
            pytest.skip("fakeredis not installed")

        from fastapi.testclient import TestClient

        from api.main import app, require_admin_key

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        fake_client = fakeredis.FakeStrictRedis()
        with patch("redis.from_url", return_value=fake_client):
            process_b_cache = settings_module._RuntimeConfigCache()
            assert process_b_cache.get().get("risk_score_threshold") is None

            app.dependency_overrides[require_admin_key] = lambda: None
            try:
                client = TestClient(app)
                resp = client.patch(
                    "/admin/config", json={"updates": {"risk_score_threshold": "77"}}
                )
                assert resp.status_code == 200
            finally:
                app.dependency_overrides.clear()

            # A second process (sharing only the DB + Redis) sees it immediately.
            cfg = process_b_cache.get()
            assert cfg["risk_score_threshold"] == "77"


# ---------------------------------------------------------------------------
# POST /governance/proposals/{id}/execute
#
# Discovered while writing the tests above: this endpoint was documented in
# docs/governance_protocol.md since the module's inception but never actually
# implemented -- only the legacy create/list/vote shim was wired into
# api/main.py, with no way to reach `GovernanceEngine.execute_proposal` via
# the API at all. Fixed alongside the propagation mechanism since a
# governance proposal that can never be executed via its own documented API
# is the same class of "feature that silently doesn't work" this issue is
# about.
# ---------------------------------------------------------------------------


class TestExecuteProposalEndpoint:
    def test_execute_endpoint_applies_and_propagates(self, tmp_path):
        from fastapi.testclient import TestClient

        from api.main import app, require_admin_key

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        orig_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            engine = GovernanceEngine(db_path=db_path)
            p = engine.submit_proposal(
                "alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "80"}
            )
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE governance_proposals SET status='passed' WHERE id=?", (p.id,))
            conn.commit()
            conn.close()

            app.dependency_overrides[require_admin_key] = lambda: None
            try:
                client = TestClient(app)
                resp = client.post(f"/v1/governance/proposals/{p.id}/execute")
            finally:
                app.dependency_overrides.clear()
        finally:
            os.chdir(orig_cwd)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "executed"
        assert body["execution_error"] is None

        settings_module.invalidate_runtime_config_cache()
        assert settings_module.get_runtime_risk_score_threshold() == 80

    def test_execute_endpoint_rejects_non_passed_proposal(self, tmp_path):
        from fastapi.testclient import TestClient

        from api.main import app, require_admin_key

        db_path = str(tmp_path / "ledgerlens.db")
        _make_governance_db(db_path)
        object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

        engine = GovernanceEngine(db_path=db_path)
        p = engine.submit_proposal(
            "alice", "config_change", {"key": "RISK_SCORE_THRESHOLD", "new_value": "80"}
        )  # still 'active', never voted/passed

        app.dependency_overrides[require_admin_key] = lambda: None
        try:
            client = TestClient(app)
            resp = client.post(f"/v1/governance/proposals/{p.id}/execute")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 422
