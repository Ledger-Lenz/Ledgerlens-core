import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from config.settings import settings
from detection.storage import RiskScoreStore
from ingestion.historical_loader import (
    ChunkProgress,
    ParallelHistoricalLoader,
    ProgressTracker,
    partition_range,
)


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(number: int, timestamp: datetime) -> dict:
    return {
        "id": str(number),
        "paging_token": f"{number}-0",
        "ledger_close_time": timestamp.isoformat().replace("+00:00", "Z"),
        "base_account": f"GBASE{number}",
        "counter_account": f"GCOUNTER{number}",
        "base_asset_type": "native",
        "counter_asset_type": "credit_alphanum4",
        "counter_asset_code": "USDC",
        "counter_asset_issuer": "GISSUER",
        "base_amount": "10.0",
        "counter_amount": "2.0",
        "price": {"n": 1, "d": 5},
        "base_is_seller": True,
        "trade_type": "orderbook",
    }


@pytest.mark.parametrize(
    ("hours", "chunk_hours", "count"),
    [(12, 6, 2), (13, 6, 3), (2, 6, 1)],
)
def test_partition_range_boundaries(hours, chunk_hours, count):
    end = START + timedelta(hours=hours)
    chunks = partition_range(START, end, chunk_hours)
    assert len(chunks) == count
    assert chunks[0][0] == START
    assert chunks[-1][1] == end
    assert all(left[1] == right[0] for left, right in zip(chunks, chunks[1:]))


def test_partition_range_last_chunk_is_short():
    chunks = partition_range(START, START + timedelta(hours=7), 6)
    assert chunks[-1][1] - chunks[-1][0] == timedelta(hours=1)


def test_partition_range_rejects_zero_duration():
    with pytest.raises(ValueError, match="start must be before end"):
        partition_range(START, START)


def test_progress_tracker_transitions_and_load(tmp_path):
    path = tmp_path / "progress.json"
    tracker = ProgressTracker(path)
    tracker.register(ChunkProgress("abc", START, START + timedelta(hours=1), "pending"))
    tracker.mark_in_progress("abc")
    tracker.mark_complete("abc", 12)

    loaded = ProgressTracker(path).progress["abc"]
    assert loaded.status == "complete"
    assert loaded.records_fetched == 12
    assert loaded.completed_at is not None
    assert not list(tmp_path.glob("*.tmp"))


def test_progress_tracker_failed_and_corrupt_fallback(tmp_path):
    path = tmp_path / "progress.json"
    tracker = ProgressTracker(path)
    tracker.register(ChunkProgress("abc", START, START + timedelta(hours=1), "pending"))
    tracker.mark_failed("abc", "boom")
    assert ProgressTracker(path).progress["abc"].error == "boom"
    path.write_text("{not-json")
    assert ProgressTracker(path).progress == {}


class FakeClient:
    def __init__(self, pages_by_start, failed_starts=None):
        self.pages_by_start = pages_by_start
        self.failed_starts = set(failed_starts or ())
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        if params is not None:
            key = params["start_time"]
            page = 0
        else:
            _, key, page_text = path.split("|", 2)
            page = int(page_text)
        if key in self.failed_starts:
            raise RuntimeError("Horizon 500")
        pages = self.pages_by_start.get(key, [])
        records = pages[page] if page < len(pages) else []
        next_link = f"next|{key}|{page + 1}" if page + 1 < len(pages) else ""
        return {
            "_embedded": {"records": records},
            "_links": {"next": {"href": next_link}},
        }


def _configure_data_dir(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(settings, "data_dir", str(data_dir))
    return data_dir


async def test_fetch_chunk_paginates_and_writes_validated_trades(tmp_path, monkeypatch):
    data_dir = _configure_data_dir(monkeypatch, tmp_path)
    chunk = (START, START + timedelta(hours=1))
    key = START.isoformat().replace("+00:00", "Z")
    client = FakeClient(
        {
            key: [
                [_record(1, START)],
                [_record(2, START + timedelta(minutes=1))],
                [_record(3, START + timedelta(minutes=2))],
            ]
        }
    )
    db_path = tmp_path / "trades.db"
    loader = ParallelHistoricalLoader(
        client,
        RiskScoreStore(str(db_path)),
        progress_path=data_dir / "progress.json",
    )
    result = await loader._fetch_chunk(chunk, None, __import__("asyncio").Semaphore(1))

    assert result.records_fetched == 3
    assert len(client.calls) == 3
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3


async def test_parallel_load_continues_after_failed_chunk(tmp_path, monkeypatch):
    data_dir = _configure_data_dir(monkeypatch, tmp_path)
    chunks = partition_range(START, START + timedelta(hours=3), 1)
    pages = {}
    for index, (chunk_start, _) in enumerate(chunks):
        key = chunk_start.isoformat().replace("+00:00", "Z")
        pages[key] = [[_record(index + 1, chunk_start)]]
    failed_key = chunks[1][0].isoformat().replace("+00:00", "Z")
    client = FakeClient(pages, {failed_key})
    loader = ParallelHistoricalLoader(
        client,
        RiskScoreStore(str(tmp_path / "trades.db")),
        concurrency=3,
        chunk_hours=1,
        progress_path=data_dir / "progress.json",
    )

    result = await loader.load(START, START + timedelta(hours=3))
    assert result.completed_chunks == 2
    assert result.failed_chunks == 1
    assert result.total_records == 2


async def test_concurrency_above_chunk_count_and_empty_response(tmp_path, monkeypatch):
    data_dir = _configure_data_dir(monkeypatch, tmp_path)
    loader = ParallelHistoricalLoader(
        FakeClient({}),
        RiskScoreStore(str(tmp_path / "trades.db")),
        concurrency=8,
        chunk_hours=6,
        progress_path=data_dir / "progress.json",
    )
    result = await loader.load(START, START + timedelta(hours=1))
    assert result.completed_chunks == 1
    assert result.failed_chunks == 0
    assert result.total_records == 0


async def test_resume_skips_completed_chunks(tmp_path, monkeypatch):
    data_dir = _configure_data_dir(monkeypatch, tmp_path)
    progress_path = data_dir / "progress.json"
    client = FakeClient({})
    loader = ParallelHistoricalLoader(
        client,
        RiskScoreStore(str(tmp_path / "trades.db")),
        concurrency=8,
        chunk_hours=1,
        progress_path=progress_path,
    )
    chunks = partition_range(START, START + timedelta(hours=2), 1)
    for chunk in chunks:
        chunk_id = __import__("hashlib").sha256(
            f"{chunk[0].isoformat()}|{chunk[1].isoformat()}|".encode()
        ).hexdigest()[:8]
        loader.tracker.register(ChunkProgress(chunk_id, *chunk, "pending"))
        loader.tracker.mark_complete(chunk_id, 5)

    result = await loader.load(START, START + timedelta(hours=2), resume=True)
    assert result.skipped_chunks == 2
    assert result.total_records == 10
    assert client.calls == []


async def test_empty_asset_pair_range_and_deduplication(tmp_path, monkeypatch):
    data_dir = _configure_data_dir(monkeypatch, tmp_path)
    key = START.isoformat().replace("+00:00", "Z")
    record = _record(1, START)
    client = FakeClient({key: [[record, record]]})
    db_path = tmp_path / "trades.db"
    loader = ParallelHistoricalLoader(
        client,
        RiskScoreStore(str(db_path)),
        progress_path=data_dir / "progress.json",
    )
    result = await loader.load(START, START + timedelta(hours=1), asset_pair="XLM/USDC")
    assert result.total_records == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


async def test_sqlite_write_failure_marks_chunk_failed(tmp_path, monkeypatch):
    data_dir = _configure_data_dir(monkeypatch, tmp_path)
    key = START.isoformat().replace("+00:00", "Z")

    class BrokenStore:
        def upsert_trades(self, _trades):
            raise sqlite3.OperationalError("disk full")

    loader = ParallelHistoricalLoader(
        FakeClient({key: [[_record(1, START)]]}),
        BrokenStore(),
        progress_path=data_dir / "progress.json",
    )
    result = await loader.load(START, START + timedelta(hours=1))
    assert result.failed_chunks == 1
    assert next(iter(loader.tracker.progress.values())).status == "failed"


def test_progress_path_cannot_escape_data_directory(tmp_path, monkeypatch):
    _configure_data_dir(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="inside DATA_DIR"):
        ParallelHistoricalLoader(
            FakeClient({}),
            object(),
            progress_path=tmp_path / "outside.json",
        )


async def test_synthetic_performance_target(tmp_path, monkeypatch):
    data_dir = _configure_data_dir(monkeypatch, tmp_path)
    pages = {}
    for chunk_index in range(10):
        chunk_start = START + timedelta(hours=chunk_index)
        key = chunk_start.isoformat().replace("+00:00", "Z")
        pages[key] = [[
            _record(chunk_index * 100 + i, chunk_start + timedelta(seconds=i))
            for i in range(100)
        ]]
    loader = ParallelHistoricalLoader(
        FakeClient(pages),
        RiskScoreStore(str(tmp_path / "trades.db")),
        concurrency=10,
        chunk_hours=1,
        progress_path=data_dir / "progress.json",
    )
    started = time.perf_counter()
    result = await loader.load(START, START + timedelta(hours=10))
    assert result.total_records == 1000
    assert time.perf_counter() - started < 5
