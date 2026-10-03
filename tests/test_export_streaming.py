"""Streaming export with bounded memory (Issue #975)."""

import io
import sqlite3
import tracemalloc
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings

_ROWS = 50_000


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "export.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE risk_scores (id INTEGER PRIMARY KEY, wallet TEXT, asset_pair TEXT, "
        "score INTEGER, benford_flag INTEGER, ml_flag INTEGER, confidence INTEGER, timestamp TEXT)"
    )
    base = datetime.now(timezone.utc) - timedelta(days=1)
    conn.executemany(
        "INSERT INTO risk_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                i,
                f"G{i:055d}",
                "XLM/USDC",
                i % 100,
                0,
                1,
                90,
                (base + timedelta(seconds=i)).isoformat(),
            )
            for i in range(_ROWS)
        ),
    )
    conn.commit()
    conn.close()
    with patch.object(_settings, "ledgerlens_db_path", path):
        yield path


@pytest.fixture
def client(db_path):
    from api import export_router
    from api.auth import require_admin_key

    export_router._rate_limit_store.clear()
    app = FastAPI()
    app.include_router(export_router.router)
    app.dependency_overrides[require_admin_key] = lambda: None
    return TestClient(app)


def _params():
    today = datetime.now(timezone.utc).date()
    return {"from": str(today - timedelta(days=2)), "to": str(today)}


def test_csv_export_streams_with_bounded_memory(client):
    """Server-side peak memory stays well below the full CSV payload size.

    The generator is consumed directly because TestClient buffers the whole
    response body, which would measure the client rather than the server.
    """
    from api import export_router

    p = _params()
    total = 0
    lines = 0
    tracemalloc.start()
    with patch.object(_settings, "export_chunk_size", 500):
        sql, params = export_router._prepare_export(p["from"], p["to"], 0, None)
        for chunk in export_router._csv_stream(sql, params):
            total += len(chunk)
            lines += chunk.count("\n")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert lines == _ROWS + 1
    assert total > 4_000_000
    assert peak < total / 4, f"peak={peak} total={total}"


def test_csv_endpoint_returns_all_rows(client):
    resp = client.get("/export/scores.csv", params=_params())
    assert resp.status_code == 200
    assert resp.text.count("\n") == _ROWS + 1


def test_parquet_export_round_trips(client):
    with patch.object(_settings, "export_chunk_size", 5_000):
        resp = client.get("/export/scores.parquet", params=_params())
    assert resp.status_code == 200
    table = pq.read_table(io.BytesIO(resp.content))
    assert table.num_rows == _ROWS
    assert pq.ParquetFile(io.BytesIO(resp.content)).num_row_groups == _ROWS // 5_000


def test_export_over_row_cap_is_rejected(client):
    with patch.object(_settings, "export_max_rows", 10):
        resp = client.get("/export/scores.csv", params=_params())
    assert resp.status_code == 413
