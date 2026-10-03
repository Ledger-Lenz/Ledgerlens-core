"""Mid-session credential revocation terminates long-lived WS/SSE sessions (#972)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status

import api.streaming_router as streaming_router
from api.ws_router import ConnectionManager
from config.settings import settings

INTERVAL = 0.05


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "ledgerlens_admin_api_key", "key-v1")
    monkeypatch.setattr(settings, "stream_auth_recheck_interval_seconds", INTERVAL)


def _ws():
    ws = AsyncMock()
    ws.close = AsyncMock()
    return ws


async def test_ws_revoked_key_disconnects_within_bound(monkeypatch):
    manager = ConnectionManager()
    ws = _ws()
    assert await manager.connect(ws, None, api_key="key-v1")

    await asyncio.sleep(INTERVAL * 2)
    assert id(ws) in manager._connections  # still authorized

    monkeypatch.setattr(settings, "ledgerlens_admin_api_key", "key-v2")  # revoke
    await asyncio.sleep(INTERVAL * 2 + 0.05)  # documented bound: one interval

    assert id(ws) not in manager._connections
    ws.close.assert_awaited_with(code=status.WS_1008_POLICY_VIOLATION)
    await manager.close_all()


async def test_ws_recheck_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setattr(settings, "stream_auth_recheck_interval_seconds", 0)
    manager = ConnectionManager()
    ws = _ws()
    assert await manager.connect(ws, None, api_key="key-v1")
    assert manager._connections[id(ws)].auth_task is None
    await manager.close_all()


async def test_ws_disconnect_cancels_recheck_task():
    manager = ConnectionManager()
    ws = _ws()
    await manager.connect(ws, None, api_key="key-v1")
    task = manager._connections[id(ws)].auth_task
    manager.disconnect(ws)
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


async def test_sse_stream_ends_after_revocation(monkeypatch):
    valid = {"ok": True}
    monkeypatch.setattr(streaming_router, "_auth_still_valid", lambda request: valid["ok"])

    class _Manager:
        async def subscribe(self, **kwargs):
            while True:
                await asyncio.sleep(INTERVAL / 2)
                yield ": heartbeat\n\n"

    monkeypatch.setattr(streaming_router, "_get_manager", lambda: _Manager())
    response = await streaming_router.stream_scores(
        request=MagicMock(), wallets="G" * 56, last_event_id=None
    )

    received = 0
    async def consume():
        nonlocal received
        async for _ in response.body_iterator:
            received += 1
            if received == 3:
                valid["ok"] = False

    await asyncio.wait_for(consume(), timeout=INTERVAL * 10)
    assert 3 <= received <= 6
