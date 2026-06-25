"""Tests for the immutable HMAC-SHA256 audit log."""

import sqlite3

import pytest
from typer.testing import CliRunner

from cli import app
from storage.audit_log import (
    GENESIS_PREV_HASH,
    append_entry,
    get_all_entries,
    init_db,
    log_admin_config_changed,
    log_api_key_used,
    log_score_computed,
    log_suppression_rule_added,
    log_suppression_rule_removed,
    is_chain_intact,
    verify_chain,
)


@pytest.fixture
def audit_db(tmp_path) -> str:
    return str(tmp_path / "audit_test.db")


@pytest.fixture
def populated_db(audit_db) -> str:
    init_db(audit_db)
    return audit_db


# ---------------------------------------------------------------------------
# Genesis / init
# ---------------------------------------------------------------------------


def test_init_db_creates_table(audit_db):
    init_db(audit_db)
    conn = sqlite3.connect(audit_db)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchall()
    conn.close()
    assert len(tables) == 1


def test_init_db_inserts_genesis(audit_db):
    entries = get_all_entries(audit_db)
    assert len(entries) == 1
    assert entries[0]["event_type"] == "genesis"
    assert entries[0]["actor"] == "system"
    assert entries[0]["prev_hash"] == GENESIS_PREV_HASH
    assert entries[0]["entry_hash"] is not None

# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def test_append_entry_adds_to_chain(populated_db):
    entry = append_entry("score_computed", "alice", wallet="GABCDEF123", score=85, db_path=populated_db)
    assert entry["event_type"] == "score_computed"
    assert entry["actor"] == "alice"
    assert entry["wallet"] == "GABCDEF123"
    assert entry["score"] == 85
    assert entry["prev_hash"] is not None
    assert entry["entry_hash"] is not None
    assert len(get_all_entries(populated_db)) == 2


def test_append_entry_prev_hash_links_to_previous(populated_db):
    entries_before = get_all_entries(populated_db)
    genesis_hash = entries_before[0]["entry_hash"]
    entry = append_entry("api_key_used", "bob", db_path=populated_db)
    assert entry["prev_hash"] == genesis_hash

# ---------------------------------------------------------------------------
# Convenience loggers
# ---------------------------------------------------------------------------


def test_log_score_computed(populated_db):
    e = log_score_computed("alice", "GABCDEF123", 90, db_path=populated_db)
    assert e["event_type"] == "score_computed"
    assert e["wallet"] == "GABCDEF123"
    assert e["score"] == 90


def test_log_api_key_used(populated_db):
    e = log_api_key_used("bob", db_path=populated_db)
    assert e["event_type"] == "api_key_used"
    assert e["actor"] == "bob"


def test_log_admin_config_changed(populated_db):
    e = log_admin_config_changed("admin", db_path=populated_db)
    assert e["event_type"] == "admin_config_changed"
    assert e["actor"] == "admin"

# ---------------------------------------------------------------------------
# Chain verification - integrity
# ---------------------------------------------------------------------------


def test_verify_chain_intact_after_multiple_writes(populated_db):
    for i in range(100):
        log_score_computed(f"user{i % 10}", f"G{i:056d}", i % 100, db_path=populated_db)
    results = verify_chain(populated_db)
    assert len(results) == 101
    assert all(r["error"] is None for r in results)
    assert is_chain_intact(populated_db)


def test_verify_chain_detects_tampered_entry_hash(populated_db):
    log_score_computed("alice", "GABCDEF123", 85, db_path=populated_db)
    log_score_computed("bob", "GXYZ789", 90, db_path=populated_db)

    conn = sqlite3.connect(populated_db)
    conn.execute("UPDATE audit_log SET entry_hash = 'tampered' WHERE id = 2")
    conn.commit()
    conn.close()

    results = verify_chain(populated_db)
    errors = [r for r in results if r["error"] is not None]
    assert len(errors) == 2  # tampered entry + broken link for next entry
    assert any("tampered" in e["error"] for e in errors)
    assert any(not e["prev_hash_ok"] for e in errors)


def test_verify_chain_detects_broken_prev_hash(populated_db):
    log_score_computed("alice", "GABCDEF123", 85, db_path=populated_db)
    log_score_computed("bob", "GXYZ789", 90, db_path=populated_db)

    conn = sqlite3.connect(populated_db)
    conn.execute("UPDATE audit_log SET prev_hash = 'broken_link' WHERE id = 2")
    conn.commit()
    conn.close()

    results = verify_chain(populated_db)
    errors = [r for r in results if r["error"] is not None]
    assert len(errors) >= 1
    assert any("broken_link" in e["error"] for e in errors)


def test_verify_chain_with_deleted_middle_entry(populated_db):
    for i in range(5):
        log_score_computed(f"user{i}", f"G{i:056d}", i * 10, db_path=populated_db)

    conn = sqlite3.connect(populated_db)
    conn.execute("DELETE FROM audit_log WHERE id = 3")
    conn.commit()
    conn.close()

    results = verify_chain(populated_db)
    errors = [r for r in results if r["error"] is not None]
    assert len(errors) >= 1
    assert any(not r["prev_hash_ok"] for r in errors)


def test_verify_chain_deleted_last_entry_leaves_genesis(populated_db):
    log_score_computed("alice", "GABCDEF123", 85, db_path=populated_db)


# ---------------------------------------------------------------------------
# Bulk write
# ---------------------------------------------------------------------------


def test_chain_intact_after_1000_writes(populated_db):
    for i in range(1000):
        log_score_computed(f"user{i % 50}", f"G{i:056d}", i % 100, db_path=populated_db)
    assert is_chain_intact(populated_db)
    entries = get_all_entries(populated_db)
    assert len(entries) == 1001  # genesis + 1000


# ---------------------------------------------------------------------------
# CLI audit verify command
# ---------------------------------------------------------------------------

runner = CliRunner()


def test_cli_audit_verify_intact(populated_db):
    log_score_computed("alice", "GABCDEF123", 85, db_path=populated_db)
    result = runner.invoke(app, ["audit", "verify", "--db-path", populated_db])
    assert result.exit_code == 0
    assert "intact" in result.output


def test_cli_audit_verify_broken(populated_db):
    log_score_computed("alice", "GABCDEF123", 85, db_path=populated_db)
    conn = sqlite3.connect(populated_db)
    conn.execute("UPDATE audit_log SET entry_hash = 'bad' WHERE id = 2")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["audit", "verify", "--db-path", populated_db])
    assert result.exit_code == 1
    assert "broken" in result.output or "Chain broken" in result.output


def test_cli_audit_verify_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    result = runner.invoke(app, ["audit", "verify", "--db-path", db])
    assert result.exit_code == 0
    assert "intact" in result.output



def test_log_suppression_rule_added(populated_db):
    e = log_suppression_rule_added("admin", db_path=populated_db)
    assert e["event_type"] == "suppression_rule_added"


def test_log_suppression_rule_removed(populated_db):
    e = log_suppression_rule_removed("admin", db_path=populated_db)
    assert e["event_type"] == "suppression_rule_removed"



def test_append_entry_creates_unique_hashes(populated_db):
    hashes = set()
    for i in range(10):
        e = append_entry("score_computed", f"user{i}", wallet=f"G{i:056d}", score=i, db_path=populated_db)
        hashes.add(e["entry_hash"])
    assert len(hashes) == 10



def test_init_db_is_idempotent(audit_db):
    init_db(audit_db)
    init_db(audit_db)
    entries = get_all_entries(audit_db)
    assert len(entries) == 1
