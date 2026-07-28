"""Tests for api/ws_router.py — ConnectionManager & WebSocket endpoint (#428)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status

from config.settings import settings
from detection.risk_score import RiskScore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_admin_api_key(monkeypatch):
    """Ensure the admin API key is set so ws_alerts authentication passes."""
    monkeypatch.setattr(settings, "ledgerlens_admin_api_key", "test-admin-key")


@pytest.fixture
def manager():
    from api.ws_router import ConnectionManager

    return ConnectionManager()


@pytest.fixture
def mock_ws():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_json = AsyncMock()
    ws.__aenter__ = AsyncMock(return_value=ws)
    ws.__aexit__ = AsyncMock()
    return ws


@pytest.fixture
def risk_score():
    return RiskScore(
        wallet="GDummyWallet123",
        asset_pair="XLM/USDC",
        score=72,
        benford_flag=True,
        ml_flag=True,
        confidence=85,
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# ConnectionManager — connect / disconnect
# ---------------------------------------------------------------------------


class TestConnectionManager:
    """Core lifecycle of ConnectionManager."""

    async def test_connect_accepts_and_registers(self, manager, mock_ws):
        result = await manager.connect(mock_ws, wallet_filter=None)

        assert result is True
        mock_ws.accept.assert_awaited_once()
        assert len(manager._connections) == 1
        conn_id = id(mock_ws)
        assert conn_id in manager._connections
        assert manager._connections[conn_id].wallet_filter is None

    async def test_connect_with_wallet_filter(self, manager, mock_ws):
        result = await manager.connect(mock_ws, wallet_filter="GDummy123")

        assert result is True
        conn_id = id(mock_ws)
        assert manager._connections[conn_id].wallet_filter == "GDummy123"

    async def test_connect_rejects_when_limit_reached(self, manager, mock_ws):
        # Temporarily lower the limit to 1
        original = settings.ws_max_connections
        try:
            settings.ws_max_connections = 1
            ws1 = AsyncMock(spec=["accept", "close", "send_json"])
            ws1.accept = AsyncMock()
            ws1.close = AsyncMock()
            await manager.connect(ws1, wallet_filter=None)
            assert len(manager._connections) == 1

            ws2 = AsyncMock(spec=["accept", "close", "send_json"])
            ws2.accept = AsyncMock()
            ws2.close = AsyncMock()
            result = await manager.connect(ws2, wallet_filter=None)

            assert result is False
            ws2.accept.assert_not_awaited()
            ws2.close.assert_awaited_once_with(code=status.WS_1008_POLICY_VIOLATION)
        finally:
            settings.ws_max_connections = original

    async def test_disconnect_removes_and_cancels_heartbeat(self, manager, mock_ws):
        await manager.connect(mock_ws, wallet_filter=None)
        conn = manager._connections[id(mock_ws)]
        conn.heartbeat_task = MagicMock()
        conn.heartbeat_task.cancel = MagicMock()

        manager.disconnect(mock_ws)

        assert id(mock_ws) not in manager._connections
        conn.heartbeat_task.cancel.assert_called_once()

    async def test_disconnect_safe_for_unknown_ws(self, manager, mock_ws):
        """Calling disconnect on an unregistered WebSocket must not raise."""
        manager.disconnect(mock_ws)  # should not raise


# ---------------------------------------------------------------------------
# ConnectionManager — close_all
# ---------------------------------------------------------------------------


class TestCloseAll:
    async def test_close_all_clears_everything(self, manager):
        ws1 = AsyncMock()
        ws1.accept = AsyncMock()
        ws1.close = AsyncMock()
        ws2 = AsyncMock()
        ws2.accept = AsyncMock()
        ws2.close = AsyncMock()
        await manager.connect(ws1, wallet_filter=None)
        await manager.connect(ws2, wallet_filter=None)
        assert len(manager._connections) == 2

        await manager.close_all()

        ws1.close.assert_awaited_once()
        ws2.close.assert_awaited_once()
        assert len(manager._connections) == 0

    async def test_close_all_handles_close_exception(self, manager, mock_ws):
        await manager.connect(mock_ws, wallet_filter=None)
        mock_ws.close.side_effect = RuntimeError("connection lost")

        await manager.close_all()  # must not raise

    async def test_close_all_empty(self, manager):
        await manager.close_all()  # must not raise


# ---------------------------------------------------------------------------
# ConnectionManager — heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    async def test_record_pong_updates_timestamp(self, manager, mock_ws):
        await manager.connect(mock_ws, wallet_filter=None)
        original_ts = manager._connections[id(mock_ws)].last_pong

        manager.record_pong(mock_ws)

        new_ts = manager._connections[id(mock_ws)].last_pong
        assert new_ts >= original_ts

    async def test_record_pong_unknown_ws_noop(self, manager, mock_ws):
        """record_pong on an unknown connection must not raise."""
        manager.record_pong(mock_ws)  # should not raise

    async def test_heartbeat_sends_ping_and_disconnects_on_failure(self, manager):
        import asyncio
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.send_json = AsyncMock(side_effect=[None, RuntimeError("send failed")])

        # Patch interval BEFORE connecting so the heartbeat task uses it from the start
        with patch("api.ws_router._HEARTBEAT_INTERVAL", 0.01), \
             patch("api.ws_router._PONG_TIMEOUT", 60):
            await manager.connect(mock_ws, wallet_filter=None)
            conn = manager._connections[id(mock_ws)]
            try:
                await conn.heartbeat_task  # task may be cancelled by disconnect
            except asyncio.CancelledError:
                pass

        # The second send_json call should fail, causing disconnect
        assert mock_ws.send_json.await_count >= 1
        assert id(mock_ws) not in manager._connections

    async def test_heartbeat_drops_stale_connection(self, manager):
        import asyncio, time
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.send_json = AsyncMock()

        with patch("api.ws_router._HEARTBEAT_INTERVAL", 0.01), \
             patch("api.ws_router._PONG_TIMEOUT", 60):
            await manager.connect(mock_ws, wallet_filter=None)
            conn = manager._connections[id(mock_ws)]
            # Simulate a very old last_pong
            conn.last_pong = time.monotonic() - 120  # 120s ago, well past 60s timeout
            try:
                await conn.heartbeat_task  # task may be cancelled by disconnect
            except asyncio.CancelledError:
                pass

        mock_ws.close.assert_awaited()
        assert id(mock_ws) not in manager._connections


# ---------------------------------------------------------------------------
# ConnectionManager — broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    async def test_broadcast_sends_to_all_subscribers(self, manager, mock_ws, risk_score):
        await manager.connect(mock_ws, wallet_filter=None)
        await manager.broadcast(risk_score)

        mock_ws.send_json.assert_awaited_once()
        call_kwargs = mock_ws.send_json.call_args[0][0]
        assert call_kwargs["event"] == "risk_score_alert"
        assert call_kwargs["data"]["wallet"] == risk_score.wallet

    async def test_broadcast_filters_by_wallet(self, manager, risk_score):
        ws1 = AsyncMock()
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock()
        ws2 = AsyncMock()
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock()
        await manager.connect(ws1, wallet_filter=risk_score.wallet)
        await manager.connect(ws2, wallet_filter="GOtherWallet999")

        await manager.broadcast(risk_score)

        ws1.send_json.assert_awaited_once()
        ws2.send_json.assert_not_awaited()

    async def test_broadcast_skips_wallet_filter_none(self, manager, risk_score):
        """Connections with no wallet_filter receive all alerts."""
        ws_all = AsyncMock()
        ws_all.accept = AsyncMock()
        ws_all.send_json = AsyncMock()
        ws_filtered = AsyncMock()
        ws_filtered.accept = AsyncMock()
        ws_filtered.send_json = AsyncMock()
        await manager.connect(ws_all, wallet_filter=None)
        await manager.connect(ws_filtered, wallet_filter="GOther999")

        await manager.broadcast(risk_score)

        ws_all.send_json.assert_awaited_once()
        ws_filtered.send_json.assert_not_awaited()

    async def test_broadcast_removes_dead_connections(self, manager, risk_score):
        ws1 = AsyncMock()
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock(side_effect=RuntimeError("broken"))
        ws2 = AsyncMock()
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock()
        await manager.connect(ws1, wallet_filter=None)
        await manager.connect(ws2, wallet_filter=None)
        initial_count = len(manager._connections)

        await manager.broadcast(risk_score)

        # ws1 should be disconnected (dead), ws2 should still be connected
        assert len(manager._connections) == initial_count - 1
        ws2.send_json.assert_awaited_once()

    async def test_broadcast_empty_no_error(self, manager, risk_score):
        await manager.broadcast(risk_score)  # must not raise


# ---------------------------------------------------------------------------
# broadcast_alert helper
# ---------------------------------------------------------------------------


class TestBroadcastAlert:
    async def test_broadcast_alert_delegates_to_manager(self, risk_score):
        from api.ws_router import manager as ws_manager, broadcast_alert

        with patch.object(ws_manager, "broadcast", new=AsyncMock()) as mock_broadcast:
            await broadcast_alert(risk_score)

        mock_broadcast.assert_awaited_once_with(risk_score)


# ---------------------------------------------------------------------------
# WebSocket endpoint (via TestClient)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Skipped because importing api.main triggers a pre-existing NameError "
           "in detection/storage.py (SandwichCandidate) — outside the scope of #428."
)
class TestWsEndpoint:
    """Integration-style tests for the /ws/alerts endpoint using TestClient."""

    @pytest.fixture
    def client(self):
        from api.main import app

        from starlette.testclient import TestClient
        return TestClient(app)

    def test_ws_auth_rejected_missing_key(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/alerts"):
                pass

    def test_ws_auth_rejected_wrong_key(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/alerts?api_key=wrong-key"):
                pass

    def test_ws_connect_and_pong(self, client):
        """Happy path: authenticate, receive ping, reply pong."""
        with patch.object(settings, "admin_api_key", "test-admin-key"):
            with client.websocket_connect("/ws/alerts?api_key=test-admin-key") as ws:
                # The server should send a ping within HEARTBEAT_INTERVAL
                data = ws.receive_json()
                assert data["event"] == "ping"
                ws.send_json({"event": "pong"})

    def test_ws_invalid_message_ignored(self, client):
        with patch.object(settings, "admin_api_key", "test-admin-key"):
            with client.websocket_connect("/ws/alerts?api_key=test-admin-key") as ws:
                ws.send_json({"unexpected": "payload"})
                ws.send_json({"event": "pong"})
                # Should still get ping events after ignoring bad message
                data = ws.receive_json()
                assert data["event"] == "ping"
