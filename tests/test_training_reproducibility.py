"""Regression tests for deterministic standalone training inputs."""

import random
import sqlite3
from datetime import datetime, timezone

import numpy as np

from scripts.train_gnn import _load_labels
from scripts.train_lstm_autoencoder import load_clean_wallet_series


def test_gnn_label_selection_is_stable_for_fixed_as_of_and_seed(tmp_path):
    db_path = tmp_path / "labels.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE ring_members (wallet TEXT, confirmed INTEGER)")
        conn.execute("CREATE TABLE wallet_scores (wallet TEXT, score REAL, scored_at TEXT)")
        conn.execute("CREATE TABLE alerts (wallet TEXT, created_at TEXT)")
        conn.executemany(
            "INSERT INTO ring_members VALUES (?, 1)",
            [("GPOS1",), ("GPOS2",)],
        )
        conn.executemany(
            "INSERT INTO wallet_scores VALUES (?, 10, ?)",
            [
                (f"GNEG{i}", "2025-01-20T00:00:00+00:00")
                for i in range(10)
            ] + [("GPOS1", "2025-01-20T00:00:00+00:00")],
        )
        conn.execute(
            "INSERT INTO wallet_scores VALUES ('GFUTURE', 10, '2025-02-01T00:00:00+00:00')"
        )

    as_of = datetime(2025, 1, 31, tzinfo=timezone.utc)
    random.seed(17)
    first = _load_labels(str(db_path), neg_sample_ratio=2, as_of=as_of)
    random.seed(17)
    second = _load_labels(str(db_path), neg_sample_ratio=2, as_of=as_of)

    assert first == second
    assert first[0] == ["GPOS1", "GPOS2"]
    assert len(first[1]) == 4
    assert "GPOS1" not in first[1]
    assert "GFUTURE" not in first[1]


def test_lstm_synthetic_fallback_uses_the_requested_seed(tmp_path):
    db_path = str(tmp_path / "missing-snapshots.db")
    first = load_clean_wallet_series(db_path, sequence_length=8, seed=9)
    second = load_clean_wallet_series(db_path, sequence_length=8, seed=9)
    other_seed = load_clean_wallet_series(db_path, sequence_length=8, seed=10)

    assert len(first) == 500
    for left, right in zip(first, second):
        np.testing.assert_array_equal(left, right)
    assert any(not np.array_equal(left, right) for left, right in zip(first, other_seed))
