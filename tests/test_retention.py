"""Tests for storage/retention.py's count-invariant guarantees."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from storage.retention import RetentionEngine


def _make_db(tmp_path, rows):
    """Create a SQLite DB with a `risk_scores` table and the given rows.

    `rows` is a list of (id, timestamp) tuples.
    """
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE risk_scores (id INTEGER PRIMARY KEY, timestamp TEXT)")
    conn.executemany("INSERT INTO risk_scores (id, timestamp) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


def _count(db_path, table="risk_scores"):
    conn = sqlite3.connect(db_path)
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    conn.close()
    return count


def test_archival_preserves_row_count_invariant(tmp_path):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=400)).isoformat()
    recent_ts = (now - timedelta(days=1)).isoformat()

    rows = [(1, old_ts), (2, old_ts), (3, recent_ts)]
    db_path = _make_db(tmp_path, rows)
    pre_count = _count(db_path)

    engine = RetentionEngine(
        db_path=db_path,
        archive_root=str(tmp_path / "archive"),
        ttl_days={"risk_scores": 365},
    )
    report = engine.run()

    result = report["risk_scores"]
    assert result["rows_archived"] == 2

    post_sqlite_count = _count(db_path)
    assert post_sqlite_count == 1

    import pandas as pd

    archived_df = pd.read_parquet(result["archive_path"])
    assert len(archived_df) == result["rows_archived"]
    assert len(archived_df) + post_sqlite_count == pre_count


def test_no_rows_eligible_for_archival(tmp_path):
    now = datetime.now(timezone.utc)
    recent_ts = (now - timedelta(days=1)).isoformat()

    db_path = _make_db(tmp_path, [(1, recent_ts)])
    engine = RetentionEngine(
        db_path=db_path,
        archive_root=str(tmp_path / "archive"),
        ttl_days={"risk_scores": 365},
    )
    report = engine.run()

    result = report["risk_scores"]
    assert result["rows_archived"] == 0
    assert result["archive_path"] is None
    assert _count(db_path) == 1


def test_empty_dataset(tmp_path):
    db_path = _make_db(tmp_path, [])
    engine = RetentionEngine(
        db_path=db_path,
        archive_root=str(tmp_path / "archive"),
        ttl_days={"risk_scores": 365},
    )
    report = engine.run()

    result = report["risk_scores"]
    assert result["rows_archived"] == 0
    assert _count(db_path) == 0


def test_dry_run_does_not_modify_database(tmp_path):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=400)).isoformat()

    db_path = _make_db(tmp_path, [(1, old_ts), (2, old_ts)])
    pre_count = _count(db_path)

    engine = RetentionEngine(
        db_path=db_path,
        archive_root=str(tmp_path / "archive"),
        ttl_days={"risk_scores": 365},
    )
    report = engine.run(dry_run=True)

    result = report["risk_scores"]
    assert result["rows_archived"] == 2
    assert result["archive_path"] is None
    assert _count(db_path) == pre_count


def test_missing_table_is_skipped(tmp_path):
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.close()

    engine = RetentionEngine(
        db_path=db_path,
        archive_root=str(tmp_path / "archive"),
        ttl_days={"risk_scores": 365},
    )
    report = engine.run()

    result = report["risk_scores"]
    assert result["rows_archived"] == 0
    assert result.get("skipped") is True
