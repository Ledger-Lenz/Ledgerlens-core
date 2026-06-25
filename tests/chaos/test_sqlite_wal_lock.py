"""Chaos scenario #3: SQLite WAL locked.

Holds an exclusive write lock on the database while the API is serving
requests and verifies that the API returns 503 with a Retry-After header
rather than an unhandled 500.

Run with:
    docker compose --profile chaos up -d
    pytest tests/chaos/test_sqlite_wal_lock.py -m chaos -v
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.chaos


@pytest.fixture()
def chaos_client(tmp_path, monkeypatch):
    """TestClient pointed at a fresh DB that we can lock externally."""
    db_path = str(tmp_path / "chaos_test.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)

    import config.settings as settings_module
    object.__setattr__(settings_module.settings, "db_path", db_path)

    # Initialise schema so the DB file exists
    from detection.storage import init_db
    init_db()

    from api.main import app
    return TestClient(app), db_path


def _hold_exclusive_lock(db_path: str, hold_seconds: float, ready: threading.Event):
    """Background thread that holds an exclusive SQLite lock for `hold_seconds`."""
    conn = sqlite3.connect(db_path, timeout=0)
    conn.execute("BEGIN EXCLUSIVE")
    ready.set()
    time.sleep(hold_seconds)
    conn.rollback()
    conn.close()


def test_sqlite_wal_locked_returns_503_with_retry_after(chaos_client):
    """API returns 503 + Retry-After when SQLite WAL is exclusively locked."""
    client, db_path = chaos_client

    ready = threading.Event()
    lock_thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, 3.0, ready),
        daemon=True,
    )
    lock_thread.start()
    ready.wait(timeout=2)

    try:
        # /scores reads the DB — should degrade gracefully
        resp = client.get("/scores")
        assert resp.status_code == 503, (
            f"Expected 503 when DB is locked, got {resp.status_code}: {resp.text}"
        )
        assert "Retry-After" in resp.headers, (
            "503 response must include Retry-After header for client back-off"
        )
        retry_after = int(resp.headers["Retry-After"])
        assert retry_after > 0, "Retry-After must be a positive integer (seconds)"
    finally:
        lock_thread.join(timeout=5)


def test_sqlite_lock_does_not_produce_unhandled_500(chaos_client):
    """No unhandled 500 (internal server error) when the DB is locked."""
    client, db_path = chaos_client

    ready = threading.Event()
    lock_thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, 2.0, ready),
        daemon=True,
    )
    lock_thread.start()
    ready.wait(timeout=2)

    try:
        resp = client.get("/scores")
        # 500 indicates an unhandled exception — must not happen
        assert resp.status_code != 500, (
            f"Unhandled 500 returned while DB locked: {resp.text}"
        )
    finally:
        lock_thread.join(timeout=5)


def test_sqlite_lock_recovery(chaos_client):
    """After the lock is released, /scores returns 200 within 60 s."""
    client, db_path = chaos_client

    ready = threading.Event()
    lock_thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, 1.0, ready),
        daemon=True,
    )
    lock_thread.start()
    ready.wait(timeout=2)
    lock_thread.join(timeout=5)

    # DB lock released — /scores should return 200 again
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        resp = client.get("/scores")
        if resp.status_code == 200:
            break
        time.sleep(1)
    else:
        pytest.fail("API did not recover to 200 within 60 s after SQLite lock release")
