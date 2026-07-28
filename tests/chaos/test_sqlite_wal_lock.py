"""Chaos scenario #3: SQLite WAL locked.

Holds an exclusive write lock on the database while the API is serving
requests and verifies that the API returns 503 with a Retry-After header
rather than an unhandled 500.

Run with:
    pytest tests/chaos/test_sqlite_wal_lock.py -m chaos -v

Design notes
------------
- ``chaos_client`` is a fixture (not a tuple), so tests receive it directly
  and obtain ``client`` / ``db_path`` through named attributes.
- All requests target ``/v1/scores`` (the versioned path).  The legacy
  ``/scores`` path issues a 302 redirect, which would mask the 503 we want
  to assert.
- ``_hold_exclusive_lock`` is a daemon thread so it never blocks test
  teardown if the lock is released early.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.chaos


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@dataclass
class _ChaosClient:
    """Thin wrapper returned by the chaos_client fixture.

    Keeps ``client`` and ``db_path`` together without exposing a bare tuple
    (which would silently unpack to (TestClient, str) and hide type errors).
    """

    client: TestClient
    db_path: str


@pytest.fixture()
def chaos_client(tmp_path, monkeypatch) -> _ChaosClient:
    """TestClient pointed at a fresh DB that we can lock externally."""
    db_path = str(tmp_path / "chaos_test.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)

    # Initialise schema so the DB file exists before we try to lock it
    from detection.storage import init_db

    init_db()

    from api.main import app

    return _ChaosClient(client=TestClient(app), db_path=db_path)


# ---------------------------------------------------------------------------
# Lock helper
# ---------------------------------------------------------------------------


def _hold_exclusive_lock(
    db_path: str, hold_seconds: float, ready: threading.Event
) -> None:
    """Background thread: hold an exclusive SQLite lock for ``hold_seconds``."""
    conn = sqlite3.connect(db_path, timeout=0)
    conn.execute("BEGIN EXCLUSIVE")
    ready.set()
    time.sleep(hold_seconds)
    conn.rollback()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sqlite_wal_locked_returns_503_with_retry_after(chaos_client: _ChaosClient):
    """API returns 503 + Retry-After when the SQLite WAL is exclusively locked."""
    client = chaos_client.client
    db_path = chaos_client.db_path

    ready = threading.Event()
    lock_thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, 3.0, ready),
        daemon=True,
    )
    lock_thread.start()
    ready.wait(timeout=2)

    try:
        # /v1/scores reads the DB — must degrade gracefully
        resp = client.get("/v1/scores")
        assert resp.status_code == 503, (
            f"Expected 503 when DB is locked, got {resp.status_code}: {resp.text}"
        )
        assert "Retry-After" in resp.headers, (
            "503 response must include a Retry-After header for client back-off"
        )
        retry_after = int(resp.headers["Retry-After"])
        assert retry_after > 0, "Retry-After must be a positive integer (seconds)"
    finally:
        lock_thread.join(timeout=5)


def test_sqlite_lock_does_not_produce_unhandled_500(chaos_client: _ChaosClient):
    """No unhandled 500 is returned while the DB is locked."""
    client = chaos_client.client
    db_path = chaos_client.db_path

    ready = threading.Event()
    lock_thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, 2.0, ready),
        daemon=True,
    )
    lock_thread.start()
    ready.wait(timeout=2)

    try:
        resp = client.get("/v1/scores")
        assert resp.status_code != 500, (
            f"Unhandled 500 returned while DB is locked: {resp.text}"
        )
    finally:
        lock_thread.join(timeout=5)


def test_sqlite_lock_recovery(chaos_client: _ChaosClient):
    """After the lock is released, /v1/scores returns 200 within 60 s."""
    client = chaos_client.client
    db_path = chaos_client.db_path

    ready = threading.Event()
    lock_thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, 1.0, ready),
        daemon=True,
    )
    lock_thread.start()
    ready.wait(timeout=2)
    lock_thread.join(timeout=5)  # wait for lock to be released

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        resp = client.get("/v1/scores")
        if resp.status_code == 200:
            break
        time.sleep(1)
    else:
        pytest.fail(
            "API did not recover to 200 within 60 s after SQLite lock release"
        )


def test_sqlite_lock_legacy_path_still_redirects(chaos_client: _ChaosClient):
    """The legacy /scores redirect (302) is served from routing, not the DB.

    Even under a held lock the redirect response must arrive — it never
    touches the database.
    """
    client = chaos_client.client
    db_path = chaos_client.db_path

    ready = threading.Event()
    lock_thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, 2.0, ready),
        daemon=True,
    )
    lock_thread.start()
    ready.wait(timeout=2)

    try:
        resp = client.get("/scores", follow_redirects=False)
        # Routing happens before DB access, so the redirect must still come back
        assert resp.status_code == 302, (
            f"Expected 302 redirect for legacy /scores path, got {resp.status_code}"
        )
    finally:
        lock_thread.join(timeout=5)
