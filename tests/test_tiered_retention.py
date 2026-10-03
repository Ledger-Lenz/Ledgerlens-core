"""Hot/warm/cold tiered retention with integrity checks (Issue #977)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from storage.retention import TieredRetentionEngine

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
AGES_DAYS = [1, 10, 29, 31, 100, 364, 366, 500, 800]


@pytest.fixture
def engine(tmp_path):
    db = str(tmp_path / "hot.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE risk_scores (id INTEGER PRIMARY KEY, wallet TEXT, score INTEGER, timestamp TEXT)"
    )
    conn.executemany(
        "INSERT INTO risk_scores VALUES (?, ?, ?, ?)",
        [
            (i, f"W{i}", age, (NOW - timedelta(days=age)).isoformat())
            for i, age in enumerate(AGES_DAYS)
        ],
    )
    conn.commit()
    conn.close()
    return TieredRetentionEngine(
        db_path=db,
        warm_db_path=str(tmp_path / "warm.db"),
        cold_root=str(tmp_path / "cold"),
        tables=["risk_scores"],
    )


def _count(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM risk_scores").fetchone()[0]
    finally:
        conn.close()


def test_migration_moves_rows_to_correct_tier(engine, tmp_path):
    before = engine.query("risk_scores", now=NOW)
    report = engine.migrate(now=NOW)["risk_scores"]

    assert report == {"to_warm": 6, "to_cold": 3, "total_rows": len(AGES_DAYS)}
    assert _count(tmp_path / "hot.db") == 3
    assert _count(tmp_path / "warm.db") == 3
    assert list((tmp_path / "cold").glob("*/risk_scores.parquet"))

    # Integrity: every row survives migration unchanged.
    after = engine.query("risk_scores", now=NOW)
    assert after == before


def test_migration_is_idempotent(engine):
    engine.migrate(now=NOW)
    assert engine.migrate(now=NOW)["risk_scores"]["to_warm"] == 0
    assert engine.migrate(now=NOW)["risk_scores"]["to_cold"] == 0
    assert len(engine.query("risk_scores", now=NOW)) == len(AGES_DAYS)


def test_query_routes_time_range_across_tiers(engine):
    engine.migrate(now=NOW)
    start = (NOW - timedelta(days=400)).isoformat()
    end = (NOW - timedelta(days=5)).isoformat()
    rows = engine.query("risk_scores", start=start, end=end, now=NOW)
    assert sorted(r["score"] for r in rows) == [10, 29, 31, 100, 364, 366]

    recent = engine.query("risk_scores", start=(NOW - timedelta(days=15)).isoformat(), now=NOW)
    assert sorted(r["score"] for r in recent) == [1, 10]


def test_invalid_tier_bounds_rejected(tmp_path):
    with pytest.raises(ValueError):
        TieredRetentionEngine(db_path=str(tmp_path / "x.db"), hot_days=30, warm_days=30)
