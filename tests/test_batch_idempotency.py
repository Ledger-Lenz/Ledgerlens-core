"""Idempotency-Key support for POST /scores/batch (Issue #976)."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings as _settings


@pytest.fixture
def client(tmp_path):
    from api import batch_router

    calls: list[str] = []

    async def _fake_process(job_id, wallets):
        calls.append(job_id)
        batch_router._active_jobs.discard(job_id)

    with (
        patch.object(_settings, "ledgerlens_db_path", str(tmp_path / "batch.db")),
        patch.object(batch_router, "_process_batch", _fake_process),
    ):
        batch_router._init_batch_table()
        batch_router._active_jobs.clear()
        app = FastAPI()
        app.include_router(batch_router.router)
        yield TestClient(app), calls


def test_same_key_is_processed_once(client):
    tc, calls = client
    body = {"wallets": ["GABC", "GDEF"]}
    headers = {"Idempotency-Key": "retry-123"}

    first = tc.post("/scores/batch", json=body, headers=headers)
    second = tc.post("/scores/batch", json=body, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(calls) == 1


def test_different_keys_and_no_key_are_processed_separately(client):
    tc, calls = client
    body = {"wallets": ["GABC"]}
    ids = {
        tc.post("/scores/batch", json=body, headers={"Idempotency-Key": "a"}).json()["job_id"],
        tc.post("/scores/batch", json=body, headers={"Idempotency-Key": "b"}).json()["job_id"],
        tc.post("/scores/batch", json=body).json()["job_id"],
        tc.post("/scores/batch", json=body).json()["job_id"],
    }
    assert len(ids) == 4
    assert len(calls) == 4


def test_key_expires_after_window(client):
    tc, calls = client
    body = {"wallets": ["GABC"]}
    headers = {"Idempotency-Key": "expiring"}
    first = tc.post("/scores/batch", json=body, headers=headers).json()["job_id"]
    with patch.object(_settings, "batch_idempotency_window_hours", -1):
        second = tc.post("/scores/batch", json=body, headers=headers).json()["job_id"]
    assert first != second
    assert len(calls) == 2
