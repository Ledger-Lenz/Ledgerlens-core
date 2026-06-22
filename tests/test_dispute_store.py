import sqlite3
from datetime import datetime, timezone

import pytest

from config.settings import settings
from detection import storage
from detection.dispute_store import submit_dispute, cast_vote


def _use_tmp_db(tmp_path):
    db = tmp_path / "test.db"
    object.__setattr__(settings, "db_path", str(db))
    storage.init_db()
    return str(db)


def test_submit_dispute_creates_pending(tmp_path):
    db = _use_tmp_db(tmp_path)
    # insert on_chain_submission
    conn = sqlite3.connect(db)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO on_chain_submissions (wallet, asset_pair, score, tx_hash, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?)", ("GABC", "XLM/USDC", 80, "tx123", "submitted", ts))
    conn.commit()
    conn.close()

    d = submit_dispute("GABC", "XLM/USDC", None)
    assert d.status == "pending"


def test_second_dispute_within_7_days_raises(tmp_path):
    db = _use_tmp_db(tmp_path)
    conn = sqlite3.connect(db)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO on_chain_submissions (wallet, asset_pair, score, tx_hash, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?)", ("GXYZ", "XLM/USDC", 50, "tx456", "submitted", ts))
    conn.commit()
    conn.close()

    submit_dispute("GXYZ", "XLM/USDC", None)
    with pytest.raises(ValueError):
        submit_dispute("GXYZ", "XLM/USDC", None)


def test_cast_vote_and_duplicate_vote(tmp_path):
    db = _use_tmp_db(tmp_path)
    conn = sqlite3.connect(db)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO on_chain_submissions (wallet, asset_pair, score, tx_hash, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?)", ("GDEF", "XLM/USDC", 60, "tx789", "submitted", ts))
    conn.commit()
    conn.close()

    d = submit_dispute("GDEF", "XLM/USDC", None)
    # first vote
    updated = cast_vote(d.dispute_id, "a" * 64, "approve")
    assert len(updated.committee_votes) == 1
    # duplicate vote
    with pytest.raises(ValueError):
        cast_vote(d.dispute_id, "a" * 64, "approve")


def test_quorum_triggers_resolution(tmp_path):
    db = _use_tmp_db(tmp_path)
    conn = sqlite3.connect(db)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO on_chain_submissions (wallet, asset_pair, score, tx_hash, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?)", ("GHIJ", "XLM/USDC", 90, "tx000", "submitted", ts))
    # add committee members
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO committee_members (public_key_hex, key_hash, added_at) VALUES (?, ?, ?)", ("pk1", "a" * 64, now))
    conn.execute("INSERT INTO committee_members (public_key_hex, key_hash, added_at) VALUES (?, ?, ?)", ("pk2", "b" * 64, now))
    conn.execute("INSERT INTO committee_members (public_key_hex, key_hash, added_at) VALUES (?, ?, ?)", ("pk3", "c" * 64, now))
    conn.commit()
    conn.close()

    d = submit_dispute("GHIJ", "XLM/USDC", None)
    # cast approve votes from three members
    cast_vote(d.dispute_id, "a" * 64, "approve")
    cast_vote(d.dispute_id, "b" * 64, "approve")
    # after third vote it should be resolved (supermajority)
    res2 = cast_vote(d.dispute_id, "c" * 64, "approve")
    assert res2.status == "approved"
    # resolved disputes are immutable
    with pytest.raises(ValueError):
        cast_vote(d.dispute_id, "d" * 64, "reject")