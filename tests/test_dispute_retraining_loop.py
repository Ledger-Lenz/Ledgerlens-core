"""Confirmed false-positive disputes feed retraining data (Issue #1033)."""

import sqlite3
from datetime import datetime, timezone

from config.settings import settings
from detection import storage
from detection.dispute_store import CONFIRMED_FALSE_POSITIVE, cast_vote, submit_dispute
from detection.feedback_store import AnalystFeedbackStore


def test_confirmed_dispute_appears_in_next_training_snapshot(tmp_path):
    db = str(tmp_path / "test.db")
    object.__setattr__(settings, "db_path", db)
    storage.init_db()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO on_chain_submissions (wallet, asset_pair, score, tx_hash, status, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("GFP", "XLM/USDC", 88, "txfp", "submitted", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    store = AnalystFeedbackStore(db_path=db)
    _, hash_before = store.training_snapshot()

    dispute = submit_dispute("GFP", "XLM/USDC", None)
    for voter in ("a", "b", "c"):
        result = cast_vote(dispute.dispute_id, voter * 64, "approve")
    assert result.resolution == CONFIRMED_FALSE_POSITIVE

    snapshot, hash_after = store.training_snapshot()
    assert hash_after != hash_before
    [correction] = [r for r in snapshot if r["source_dispute_id"] == dispute.dispute_id]
    assert correction["wallet"] == "GFP"
    assert correction["analyst_label"] == 0
    assert correction["original_score"] == dispute.disputed_score
    assert correction["confidence"] == 1.0
    # Snapshot hashing is deterministic for reproducibility.
    assert store.training_snapshot()[1] == hash_after


def test_rejected_dispute_produces_no_correction(tmp_path):
    db = str(tmp_path / "test.db")
    object.__setattr__(settings, "db_path", db)
    storage.init_db()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO on_chain_submissions (wallet, asset_pair, score, tx_hash, status, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("GTP", "XLM/USDC", 90, "txtp", "submitted", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    dispute = submit_dispute("GTP", "XLM/USDC", None)
    for voter in ("a", "b", "c"):
        cast_vote(dispute.dispute_id, voter * 64, "reject")
    assert AnalystFeedbackStore(db_path=db).training_snapshot()[0] == []
