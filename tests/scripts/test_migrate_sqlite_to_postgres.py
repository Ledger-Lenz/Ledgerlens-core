"""Tests for scripts/migrate_sqlite_to_postgres.py (SQLite stands in for Postgres)."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

_SPEC = importlib.util.spec_from_file_location(
    "migrate_sqlite_to_postgres",
    Path(__file__).resolve().parents[2] / "scripts" / "migrate_sqlite_to_postgres.py",
)
mig = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mig)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "src.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE risk_scores (id INTEGER, wallet TEXT, score REAL, note TEXT)")
    conn.executemany(
        "INSERT INTO risk_scores VALUES (?, ?, ?, ?)",
        [
            (1, "GA1", 0.5, None),
            (2, None, None, "x" * 200_000),
            (3, "Gä€", 1.0, "unicode ✓"),
        ],
    )
    conn.execute("CREATE TABLE alerts (id INTEGER, msg TEXT)")
    conn.commit()
    conn.close()
    return str(path)


def _dest_count(url: str, table: str) -> int:
    engine = create_engine(url)
    if not inspect(engine).has_table(table):
        return 0
    with engine.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def test_dry_run_reports_without_writing(source, tmp_path):
    url = f"sqlite:///{tmp_path / 'dst.db'}"
    plan = mig.migrate(source, url, dry_run=True)
    assert plan == {"risk_scores": 3, "alerts": 0}
    assert _dest_count(url, "risk_scores") == 0


def test_migration_verifies_edge_cases(source, tmp_path):
    url = f"sqlite:///{tmp_path / 'dst.db'}"
    mig.migrate(source, url, chunksize=2)
    assert _dest_count(url, "risk_scores") == 3
    mig.migrate(source, url, verify_only=True)


def test_mismatch_rolls_back(source, tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'dst.db'}"
    real_write = mig._write_chunk

    def corrupt(chunk, table, conn):
        chunk = chunk.copy()
        chunk.loc[chunk.index[0], "wallet"] = "TAMPERED"
        real_write(chunk, table, conn)

    monkeypatch.setattr(mig, "_write_chunk", corrupt)
    with pytest.raises(mig.VerificationError, match="checksum mismatch"):
        mig.migrate(source, url)
    assert _dest_count(url, "risk_scores") == 0


def test_verify_only_detects_post_commit_drift(source, tmp_path):
    url = f"sqlite:///{tmp_path / 'dst.db'}"
    mig.migrate(source, url)
    with create_engine(url).begin() as c:
        c.execute(text("DELETE FROM risk_scores WHERE id = 1"))
    with pytest.raises(mig.VerificationError, match="row count"):
        mig.migrate(source, url, verify_only=True)
