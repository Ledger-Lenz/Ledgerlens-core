"""Dual approval, expiry and audit trail for overrides and suppressions (Issue #995)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from config.settings import settings as _settings
from detection.override_approval import MAX_TTL_DAYS, ApprovalError

WALLET = "GA" + "A" * 54


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "overrides.db")
    with patch.object(_settings, "ledgerlens_db_path", path):
        from detection.wallet_override_store import init_override_table

        init_override_table()
        yield path


@pytest.fixture
def store(db_path):
    from detection.suppressions import SuppressionsStore

    return SuppressionsStore(db_path)


@pytest.mark.parametrize("approvers", [[], ["alice"], ["alice", "ALICE"], ["ops", "alice"], ["alice", " "]])
def test_override_without_two_distinct_approvers_is_rejected(db_path, approvers):
    from detection.wallet_override_store import add_override, get_active_override

    with pytest.raises(ApprovalError):
        add_override(WALLET, "denylist", "wash trading", "ops", approvers=approvers)
    assert get_active_override(WALLET) is None


@pytest.mark.parametrize("approvers", [[], ["alice"], ["ops", "alice"]])
def test_suppression_without_two_distinct_approvers_is_rejected(store, approvers):
    with pytest.raises(ApprovalError):
        store.add(WALLET, "known arb bot", requested_by="ops", approvers=approvers)
    assert store.is_suppressed(WALLET) is None


def test_override_requires_justification(db_path):
    from detection.wallet_override_store import add_override

    with pytest.raises(ApprovalError):
        add_override(WALLET, "allowlist", "  ", "ops", approvers=["alice", "bob"])


def test_dual_approved_override_is_applied_and_audited(db_path):
    from detection.wallet_override_store import (
        add_override,
        get_active_override,
        list_override_audit,
    )

    add_override(WALLET, "allowlist", "partner", "ops", approvers=["alice", "bob"])
    active = get_active_override(WALLET)
    assert active["approvers"] == "alice,bob"
    assert active["expires_at"] > datetime.now(timezone.utc).isoformat()

    [record] = list_override_audit(WALLET)
    assert record["action"] == "add"
    assert record["requester"] == "ops"
    assert record["approvers"] == ["alice", "bob"]
    assert record["justification"] == "partner"
    assert record["before_state"] is None
    assert record["after_state"]["list_type"] == "allowlist"


def test_override_expires_unless_renewed(db_path):
    from detection.wallet_override_store import (
        add_override,
        get_active_override,
        list_override_audit,
        renew_override,
    )

    now = datetime.now(timezone.utc)
    add_override(WALLET, "denylist", "wash", "ops", approvers=["alice", "bob"],
                 expires_at=(now + timedelta(days=1)).isoformat())
    renewed_to = (now + timedelta(days=30)).isoformat()
    with pytest.raises(ApprovalError):
        renew_override(WALLET, "denylist", "ops", ["alice"], "still active", renewed_to)
    renew_override(WALLET, "denylist", "ops", ["alice", "carol"], "still active", renewed_to)

    actions = [r["action"] for r in list_override_audit(WALLET)]
    assert actions == ["add", "renew"]
    renew = list_override_audit(WALLET)[1]
    assert renew["before_state"]["expires_at"] < renew["after_state"]["expires_at"]

    with patch("detection.wallet_override_store.datetime") as mock_dt:
        mock_dt.now.return_value = now + timedelta(days=31)
        assert get_active_override(WALLET) is None


def test_expiry_is_capped(db_path):
    from detection.wallet_override_store import add_override

    too_far = (datetime.now(timezone.utc) + timedelta(days=MAX_TTL_DAYS + 1)).isoformat()
    with pytest.raises(ApprovalError):
        add_override(WALLET, "allowlist", "partner", "ops", approvers=["alice", "bob"], expires_at=too_far)


def test_suppression_audit_and_expiry(store):
    rule = store.add(WALLET, "known arb bot", requested_by="ops", approvers=["alice", "bob"])
    assert rule["expires_at"] is not None
    assert store.is_suppressed(WALLET)["id"] == rule["id"]

    store.renew(rule["id"], "ops", ["alice", "carol"], "still an arb bot")
    store.delete(rule["id"], deleted_by="ops")

    records = store.list_audit(WALLET)
    assert [r["action"] for r in records] == ["add", "renew", "delete"]
    assert records[0]["approvers"] == ["alice", "bob"]
    assert records[2]["before_state"]["wallet"] == WALLET
    assert records[2]["after_state"] is None


def test_legacy_permanent_suppression_gets_expiry(db_path):
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE alert_suppressions (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT NOT NULL, "
            "reason TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT)"
        )
        conn.execute("INSERT INTO alert_suppressions (wallet, reason, created_at) VALUES (?, 'old', '2025-01-01')",
                     (WALLET,))

    from detection.suppressions import SuppressionsStore

    [rule] = SuppressionsStore(db_path).list_active()
    assert rule["expires_at"] is not None
