"""Tests for detection/storage.py — Issue #516.

Cleanup applied
---------------
- Extracted shared ``_FakeConnection`` / ``_fake_connect`` helpers (replacing
  two identical inline class definitions that were copy-pasted between
  ``test_get_latest_scores_filters_flags_in_sql`` and
  ``test_get_latest_scores_sorts_by_requested_column_in_sql``).
- Removed the redundant ``from contextlib import contextmanager`` import that
  appeared inside ``test_get_latest_scores_filters_flags_in_sql``; the import
  is declared once at module level alongside the other stdlib imports.
- Eliminated the separate ``FakeContext`` class used by
  ``test_get_latest_scores_applies_limit_offset_in_sql``; it duplicated the
  same fake-connection logic and is now replaced by the shared helper.
- All existing behaviour is preserved; no assertions or test logic changed.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
# Shared fake-connection helpers (replaces duplicated inline classes)
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Minimal cursor that records the last execute() call and returns nothing."""

    def __init__(self):
        self._executed: list[tuple] = []

    def fetchall(self):
        return []


class _FakeConnection:
    """Connection stub that captures every execute() call for assertion."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    def executescript(self, _script: str) -> None:
        return None

    def commit(self) -> None:
        return None

    def execute(self, query: str, params: tuple = ()) -> _FakeCursor:
        self.executed.append((query, params))
        return _FakeCursor()

    def close(self) -> None:
        return None


def _fake_connect_factory():
    """Return a (cm, conn) pair where cm is a context-manager-returning callable.

    Pass ``cm`` to ``monkeypatch.setattr(storage_module, "_connect", cm)`` and
    inspect ``conn.executed`` afterwards to assert on the SQL that was issued.
    """
    conn = _FakeConnection()

    @contextmanager
    def _cm(_db_path=None):
        yield conn

    return _cm, conn


# ---------------------------------------------------------------------------
# Existing tests (behaviour unchanged)
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
    """Flag filters (benford_flag, ml_flag) are pushed to SQL, not Python."""
    import detection.storage as storage_module

    cm, conn = _fake_connect_factory()
    monkeypatch.setattr(storage_module, "_connect", cm)

    get_latest_scores(benford_flag=True, ml_flag=False, db_path="fake.db")

    query, params = conn.executed[-1]
    compact_query = " ".join(query.split())
    assert "rs.benford_flag = ?" in compact_query
    assert "rs.ml_flag = ?" in compact_query
    assert params == (1, 0)


def test_get_latest_scores_sorts_by_requested_column_in_sql(monkeypatch):
    """ORDER BY clause uses the caller-requested column, not hard-coded 'score'."""
    import detection.storage as storage_module

    cm, conn = _fake_connect_factory()
    monkeypatch.setattr(storage_module, "_connect", cm)

    get_latest_scores(sort_by="confidence", db_path="fake.db")

    query, _params = conn.executed[-1]
    assert "ORDER BY rs.confidence DESC" in " ".join(query.split())


def test_get_latest_scores_rejects_invalid_sort_by(db_path):
    with pytest.raises(ValueError, match="sort_by"):
        get_latest_scores(sort_by="invalid", db_path=db_path)


def test_save_scores_noop_on_empty_list(db_path):
    save_scores([], db_path)
    assert get_latest_scores(db_path=db_path) == []


def test_get_latest_scores_applies_limit_offset_in_sql(tmp_path, monkeypatch):
    """Paging (LIMIT / OFFSET) is performed in SQL, not by loading all rows in Python."""
    import detection.storage as storage_module

    db_path = str(tmp_path / "ledgerlens.db")

    cm, conn = _fake_connect_factory()
    monkeypatch.setattr(storage_module, "_connect", cm)

    storage_module.init_db(db_path)
    storage_module.get_latest_scores(wallet=None, limit=5, offset=10, db_path=db_path)

    # The last execute() call must contain LIMIT and OFFSET placeholders.
    last_query, last_params = conn.executed[-1]
    assert "LIMIT ? OFFSET ?" in last_query
    assert last_params == (5, 10)


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


# ---------------------------------------------------------------------------
# Additional regression tests (new coverage)
# ---------------------------------------------------------------------------


def test_save_multiple_wallets_returned_as_latest(db_path):
    """save_scores with multiple distinct wallet/pair combos are all returned."""
    scores = [
        _score(wallet="GABC", asset_pair="XLM/USDC", score=80),
        _score(wallet="GXYZ", asset_pair="XLM/USDC", score=60),
        _score(wallet="GABC", asset_pair="XLM/BTC", score=40),
    ]
    save_scores(scores, db_path)

    all_scores = get_latest_scores(db_path=db_path)
    assert len(all_scores) == 3


def test_get_latest_scores_sort_by_timestamp(db_path):
    """sort_by='timestamp' returns scores ordered by recency descending."""
    t_old = datetime.now(timezone.utc) - timedelta(hours=2)
    t_new = datetime.now(timezone.utc)

    save_scores(
        [
            _score(wallet="GABC", score=50, timestamp=t_old),
            _score(wallet="GXYZ", score=50, timestamp=t_new),
        ],
        db_path,
    )

    results = get_latest_scores(sort_by="timestamp", db_path=db_path)
    assert len(results) == 2
    # Most recent first
    assert results[0].wallet == "GXYZ"
    assert results[1].wallet == "GABC"


def test_get_latest_scores_limit_zero_returns_empty(db_path):
    """LIMIT 0 returns an empty list (SQL semantics, not a Python slice)."""
    save_scores([_score()], db_path)

    results = get_latest_scores(limit=0, offset=0, db_path=db_path)
    assert results == []


def test_get_latest_scores_filter_by_asset_pair(db_path):
    """asset_pair kwarg restricts results to the requested pair."""
    save_scores(
        [
            _score(wallet="GABC", asset_pair="XLM/USDC"),
            _score(wallet="GABC", asset_pair="XLM/BTC"),
        ],
        db_path,
    )

    results = get_latest_scores(asset_pair="XLM/USDC", db_path=db_path)
    assert len(results) == 1
    assert results[0].asset_pair == "XLM/USDC"
