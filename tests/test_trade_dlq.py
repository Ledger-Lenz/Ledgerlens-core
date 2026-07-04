"""Tests for ingestion/dlq.py — Trade ingestion Dead-Letter Queue."""
import sqlite3
from datetime import datetime

import pytest

from ingestion.dlq import DLQErrorClass, TradeDLQ


@pytest.fixture
def dlq(tmp_path):
    db = str(tmp_path / "test_dlq.db")
    # Bootstrap schema via storage.init_db
    from detection.storage import init_db
    init_db(db)
    return TradeDLQ(db_path=db)


# ---------------------------------------------------------------------------
# push()
# ---------------------------------------------------------------------------

def test_push_returns_id(dlq):
    row_id = dlq.push("horizon_streamer", DLQErrorClass.NETWORK_ERROR, "timeout", {"foo": "bar"})
    assert isinstance(row_id, int)
    assert row_id >= 1


def test_push_serialises_dict(dlq):
    dlq.push("historical_loader", DLQErrorClass.PARSE_ERROR, "bad field", {"amount": 1.5})
    entries = dlq.list_entries()
    assert len(entries) == 1
    assert '"amount"' in entries[0].raw_record


def test_push_accepts_string_raw_record(dlq):
    dlq.push("operations_loader", DLQErrorClass.SCHEMA_ERROR, "unknown field", '{"raw": true}')
    entries = dlq.list_entries()
    assert entries[0].raw_record == '{"raw": true}'


# ---------------------------------------------------------------------------
# list_entries() filters
# ---------------------------------------------------------------------------

def test_list_entries_all(dlq):
    dlq.push("s1", DLQErrorClass.NETWORK_ERROR, "err1", {})
    dlq.push("s2", DLQErrorClass.PARSE_ERROR, "err2", {})
    assert len(dlq.list_entries()) == 2


def test_list_entries_filter_status(dlq):
    dlq.push("s1", DLQErrorClass.NETWORK_ERROR, "err", {})
    id2 = dlq.push("s2", DLQErrorClass.NETWORK_ERROR, "err", {})
    dlq.mark_dead(id2)
    pending = dlq.list_entries(status="pending")
    assert len(pending) == 1
    assert pending[0].status == "pending"


def test_list_entries_filter_error_class(dlq):
    dlq.push("s1", DLQErrorClass.NETWORK_ERROR, "net", {})
    dlq.push("s2", DLQErrorClass.PARSE_ERROR, "parse", {})
    results = dlq.list_entries(error_class=DLQErrorClass.PARSE_ERROR)
    assert len(results) == 1
    assert results[0].error_class == DLQErrorClass.PARSE_ERROR


def test_list_entries_filter_source(dlq):
    dlq.push("horizon_streamer", DLQErrorClass.NETWORK_ERROR, "err", {})
    dlq.push("historical_loader", DLQErrorClass.NETWORK_ERROR, "err", {})
    results = dlq.list_entries(source="horizon_streamer")
    assert len(results) == 1
    assert results[0].source == "horizon_streamer"


def test_list_entries_limit_offset(dlq):
    for i in range(5):
        dlq.push("s", DLQErrorClass.UNKNOWN, f"err{i}", {})
    page1 = dlq.list_entries(limit=2, offset=0)
    page2 = dlq.list_entries(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {e.id for e in page1}.isdisjoint({e.id for e in page2})


# ---------------------------------------------------------------------------
# mark_replayed() / mark_dead()
# ---------------------------------------------------------------------------

def test_mark_replayed_updates_status(dlq):
    row_id = dlq.push("s", DLQErrorClass.NETWORK_ERROR, "err", {})
    dlq.mark_replayed(row_id)
    entry = dlq.list_entries()[0]
    assert entry.status == "replayed"
    assert entry.replayed_at is not None
    assert isinstance(entry.replayed_at, datetime)


def test_mark_replayed_increments_retry_count(dlq):
    row_id = dlq.push("s", DLQErrorClass.NETWORK_ERROR, "err", {})
    dlq.mark_replayed(row_id)
    entry = dlq.list_entries()[0]
    assert entry.retry_count == 1


def test_mark_dead_updates_status(dlq):
    row_id = dlq.push("s", DLQErrorClass.SCHEMA_ERROR, "err", {})
    dlq.mark_dead(row_id)
    entry = dlq.list_entries()[0]
    assert entry.status == "dead"


# ---------------------------------------------------------------------------
# get_replayable()
# ---------------------------------------------------------------------------

def test_get_replayable_returns_network_errors_by_default(dlq):
    dlq.push("s", DLQErrorClass.NETWORK_ERROR, "timeout", {})
    dlq.push("s", DLQErrorClass.PARSE_ERROR, "bad field", {})
    replayable = dlq.get_replayable()
    assert len(replayable) == 1
    assert replayable[0].error_class == DLQErrorClass.NETWORK_ERROR


def test_get_replayable_excludes_non_pending(dlq):
    row_id = dlq.push("s", DLQErrorClass.NETWORK_ERROR, "err", {})
    dlq.mark_dead(row_id)
    assert dlq.get_replayable() == []


def test_get_replayable_explicit_error_class(dlq):
    dlq.push("s", DLQErrorClass.SCHEMA_ERROR, "schema mismatch", {})
    dlq.push("s", DLQErrorClass.NETWORK_ERROR, "timeout", {})
    results = dlq.get_replayable(error_class=DLQErrorClass.SCHEMA_ERROR)
    assert len(results) == 1
    assert results[0].error_class == DLQErrorClass.SCHEMA_ERROR


def test_get_replayable_max_entries(dlq):
    for _ in range(10):
        dlq.push("s", DLQErrorClass.NETWORK_ERROR, "err", {})
    assert len(dlq.get_replayable(max_entries=3)) == 3


# ---------------------------------------------------------------------------
# classify_exception()
# ---------------------------------------------------------------------------

def test_classify_validation_error(dlq):
    try:
        from pydantic import BaseModel
        class M(BaseModel):
            x: int
        M(x="not-an-int")
    except Exception as exc:
        result = dlq.classify_exception(exc)
    assert result == DLQErrorClass.PARSE_ERROR


def test_classify_network_timeout(dlq):
    class TimeoutError(Exception):
        pass
    assert dlq.classify_exception(TimeoutError()) == DLQErrorClass.NETWORK_ERROR


def test_classify_sqlite_operational_error(dlq):
    exc = sqlite3.OperationalError("no such table")
    assert dlq.classify_exception(exc) == DLQErrorClass.STORAGE_ERROR


def test_classify_schema_error(dlq):
    class HorizonSchemaError(Exception):
        pass
    assert dlq.classify_exception(HorizonSchemaError()) == DLQErrorClass.SCHEMA_ERROR


def test_classify_version_error(dlq):
    class HorizonVersionError(Exception):
        pass
    assert dlq.classify_exception(HorizonVersionError()) == DLQErrorClass.VERSION_ERROR


def test_classify_unknown(dlq):
    assert dlq.classify_exception(RuntimeError("something weird")) == DLQErrorClass.UNKNOWN
