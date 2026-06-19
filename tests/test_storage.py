from datetime import datetime, timedelta, timezone
import time

import pytest
import sqlite3

from detection.risk_score import RiskScore
from detection.storage import (
    SchemaMigrationError,
    _MIGRATIONS,
    _connect,
    get_latest_scores,
    get_schema_version,
    init_db,
    migrate_db,
    save_scores,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "ledgerlens.db")


def _score(wallet="GABC", asset_pair="XLM/USDC", score=80, timestamp=None) -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=score > 50,
        ml_flag=score > 50,
        confidence=90,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Existing tests (unchanged)
# ---------------------------------------------------------------------------

def test_init_db_creates_table(db_path):
    init_db(db_path)
    assert get_latest_scores(db_path=db_path) == []


def test_save_and_get_latest_scores(db_path):
    save_scores([_score()], db_path)
    scores = get_latest_scores(db_path=db_path)
    assert len(scores) == 1
    assert scores[0].wallet == "GABC"
    assert scores[0].score == 80


def test_get_latest_scores_returns_most_recent_per_wallet_asset_pair(db_path):
    older = _score(score=30, timestamp=datetime.now(timezone.utc) - timedelta(hours=1))
    newer = _score(score=90, timestamp=datetime.now(timezone.utc))
    save_scores([older, newer], db_path)

    scores = get_latest_scores(db_path=db_path)
    assert len(scores) == 1
    assert scores[0].score == 90


def test_get_latest_scores_filters_by_wallet(db_path):
    save_scores([_score(wallet="GABC"), _score(wallet="GXYZ")], db_path)

    scores = get_latest_scores(wallet="GXYZ", db_path=db_path)
    assert len(scores) == 1
    assert scores[0].wallet == "GXYZ"


def test_get_latest_scores_filters_flags_in_sql(monkeypatch):
    executed = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConnection:
        def executescript(self, _script):
            return None

        def commit(self):
            return None

        def execute(self, query, params):
            executed.append((query, params))
            return FakeCursor()

    from contextlib import contextmanager

    @contextmanager
    def fake_connect(_db_path=None):
        yield FakeConnection()

    monkeypatch.setattr("detection.storage._connect", fake_connect)

    get_latest_scores(benford_flag=True, ml_flag=False, db_path="fake.db")

    query, params = executed[-1]
    compact_query = " ".join(query.split())
    assert "rs.benford_flag = ?" in compact_query
    assert "rs.ml_flag = ?" in compact_query
    assert params == (1, 0)


def test_get_latest_scores_sorts_by_requested_column_in_sql(monkeypatch):
    executed = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConnection:
        def executescript(self, _script):
            return None

        def commit(self):
            return None

        def execute(self, query, params):
            executed.append((query, params))
            return FakeCursor()

    from contextlib import contextmanager

    @contextmanager
    def fake_connect(_db_path=None):
        yield FakeConnection()

    monkeypatch.setattr("detection.storage._connect", fake_connect)

    get_latest_scores(sort_by="confidence", db_path="fake.db")

    query, _params = executed[-1]
    assert "ORDER BY rs.confidence DESC" in " ".join(query.split())


def test_get_latest_scores_rejects_invalid_sort_by(db_path):
    with pytest.raises(ValueError, match="sort_by"):
        get_latest_scores(sort_by="invalid", db_path=db_path)


def test_save_scores_noop_on_empty_list(db_path):
    save_scores([], db_path)
    assert get_latest_scores(db_path=db_path) == []


def test_get_latest_scores_applies_limit_offset_in_sql(tmp_path, monkeypatch):
    """Ensure paging is done in SQL, not by loading all rows in Python."""
    import detection.storage as storage_module

    db_path = str(tmp_path / "ledgerlens.db")

    # Mock sqlite3 connection and cursor behavior
    calls = {}

    class FakeConn:
        def __init__(self):
            self._executed = []

        def execute(self, query, params):
            calls["query"] = query
            calls["params"] = params

            class FakeCursor:
                def fetchall(self_inner):
                    return []

            return FakeCursor()

        def executescript(self, _):
            return None

        def commit(self):
            return None

        def close(self):
            return None

    class FakeContext:
        def __enter__(self_inner):
            return FakeConn()

        def __exit__(self_inner, exc_type, exc, tb):
            return False

    def fake_connect(_db_path=None):
        return FakeContext()

    monkeypatch.setattr(storage_module, "_connect", lambda db_path=None: fake_connect(db_path))

    storage_module.init_db(db_path)
    storage_module.get_latest_scores(wallet=None, limit=5, offset=10, db_path=db_path)

    assert "LIMIT ? OFFSET ?" in calls["query"]
    assert calls["params"] == (5, 10)


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

def test_fresh_db_reaches_latest_schema_version(db_path):
    """A brand-new database is migrated all the way to len(_MIGRATIONS)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        assert get_schema_version(conn) == len(_MIGRATIONS)


def test_migrate_db_from_version_zero(db_path):
    """A DB with no schema_version table (version 0) is fully migrated."""
    # Create a bare SQLite file with no tables.
    conn = sqlite3.connect(db_path)
    conn.close()

    with _connect(db_path) as conn:
        assert get_schema_version(conn) == 0
        applied = migrate_db(conn)

    assert len(applied) == len(_MIGRATIONS)
    with _connect(db_path) as conn:
        assert get_schema_version(conn) == len(_MIGRATIONS)


def test_migrate_db_idempotent(db_path):
    """Re-running migrate_db on an already-current database is a no-op."""
    init_db(db_path)
    with _connect(db_path) as conn:
        applied = migrate_db(conn)
    assert applied == []


def test_failed_migration_leaves_applying_status(db_path, monkeypatch):
    """A migration with bad SQL leaves the log row in 'applying' state."""
    import detection.storage as storage_module

    bad_migrations = [
        (1, "initial schema", _MIGRATIONS[0][2]),
        (2, "bad migration", "THIS IS NOT VALID SQL;"),
    ]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", bad_migrations)

    with _connect(db_path) as conn:
        with pytest.raises(Exception):
            migrate_db(conn)

        rows = conn.execute(
            "SELECT version, status FROM schema_migrations WHERE version = 2"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "applying"


def test_interrupted_migration_raises_on_next_startup(db_path, monkeypatch):
    """If a log row with status='applying' exists, migrate_db raises SchemaMigrationError."""
    import detection.storage as storage_module

    bad_migrations = [
        (1, "initial schema", _MIGRATIONS[0][2]),
        (2, "bad migration", "THIS IS NOT VALID SQL;"),
    ]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", bad_migrations)

    # First run: migration 2 fails, leaves 'applying' row.
    with _connect(db_path) as conn:
        with pytest.raises(Exception):
            migrate_db(conn)

    # Second run: detects the interrupted migration and raises SchemaMigrationError.
    with _connect(db_path) as conn:
        with pytest.raises(SchemaMigrationError, match="2"):
            migrate_db(conn)


def test_save_and_get_scores_on_migrated_db(db_path):
    """Existing save_scores / get_latest_scores work normally on a migrated database."""
    init_db(db_path)
    s = _score()
    save_scores([s], db_path)
    results = get_latest_scores(db_path=db_path)
    assert len(results) == 1
    assert results[0].wallet == s.wallet

