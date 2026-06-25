"""WebSocket push channel for real-time risk score alerts (#162).

Endpoint: GET /ws/alerts?api_key=<key>[&wallet_filter=G...]

Authentication: api_key query param compared against settings.admin_api_key.
Heartbeat: ping every 30s; connection dropped if no pong within 60s.
Max connections: WS_MAX_CONNECTIONS env var (default 100).
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from config.settings import settings
from detection.risk_score import RiskScore

logger = logging.getLogger("ledgerlens.ws")

_MAX_CONNECTIONS = int(os.getenv("WS_MAX_CONNECTIONS", "100"))
_HEARTBEAT_INTERVAL = 30  # seconds
_PONG_TIMEOUT = 60         # seconds without pong → drop


@dataclass
class _Conn:
    ws: WebSocket
    wallet_filter: str | None
    last_pong: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    heartbeat_task: asyncio.Task | None = None


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, _Conn] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, ws: WebSocket, wallet_filter: str | None) -> bool:
        """Accept and register a WebSocket connection.

        Returns False (and closes with 1008) if the connection limit is reached.
        """
        if len(self._connections) >= _MAX_CONNECTIONS:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return False

        await ws.accept()
        conn = _Conn(ws=ws, wallet_filter=wallet_filter)
        conn.heartbeat_task = asyncio.create_task(self._heartbeat(id(ws), conn))
        self._connections[id(ws)] = conn
        logger.info("WS connected id=%d total=%d", id(ws), len(self._connections))
        return True

    def disconnect(self, ws: WebSocket) -> None:
        conn = self._connections.pop(id(ws), None)
        if conn and conn.heartbeat_task:
            conn.heartbeat_task.cancel()
        logger.info("WS disconnected id=%d total=%d", id(ws), len(self._connections))

    async def close_all(self) -> None:
        """Gracefully close all connections on server shutdown."""
        for conn in list(self._connections.values()):
            try:
                await conn.ws.close(code=status.WS_1001_GOING_AWAY)
            except Exception:
                pass
            if conn.heartbeat_task:
                conn.heartbeat_task.cancel()
        self._connections.clear()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat(self, ws_id: int, conn: _Conn) -> None:
        loop = asyncio.get_event_loop()
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                conn_now = self._connections.get(ws_id)
                if conn_now is None:
                    return
                # Drop stale connection
                if loop.time() - conn.last_pong > _PONG_TIMEOUT:
                    logger.warning("WS pong timeout id=%d, dropping", ws_id)
                    await conn.ws.close(code=status.WS_1001_GOING_AWAY)
                    self.disconnect(conn.ws)
                    return
                try:
                    await conn.ws.send_json({"event": "ping"})
                except Exception:
                    self.disconnect(conn.ws)
                    return
        except asyncio.CancelledError:
            pass

    def record_pong(self, ws: WebSocket) -> None:
        conn = self._connections.get(id(ws))
        if conn:
            conn.last_pong = asyncio.get_event_loop().time()

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast(self, risk_score: RiskScore) -> None:
        payload = {
            "event": "risk_score_alert",
            "data": {
                "wallet": risk_score.wallet,
                "asset_pair": risk_score.asset_pair,
                "score": risk_score.score,
                "benford_flag": risk_score.benford_flag,
                "ml_flag": risk_score.ml_flag,
                "confidence": risk_score.confidence,
                "timestamp": risk_score.timestamp.isoformat(),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        dead: list[WebSocket] = []
        for conn in list(self._connections.values()):
            if conn.wallet_filter and conn.wallet_filter != risk_score.wallet:
                continue
            try:
                await conn.ws.send_json(payload)
            except Exception:
                dead.append(conn.ws)
        for ws in dead:
            self.disconnect(ws)


# Module-level singleton shared by the endpoint and run_pipeline.py
manager = ConnectionManager()

router = APIRouter()


@router.websocket("/ws/alerts")
async def ws_alerts(
    ws: WebSocket,
    api_key: str = "",
    wallet_filter: str | None = None,
) -> None:
    # --- Authentication ---
    if not settings.admin_api_key or not secrets.compare_digest(api_key, settings.admin_api_key):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not await manager.connect(ws, wallet_filter or None):
        return  # limit reached; already closed

    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("event") == "pong":
                manager.record_pong(ws)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.disconnect(ws)


async def broadcast_alert(risk_score: RiskScore) -> None:
    """Push a risk score alert to all relevant WebSocket subscribers.

    Call this from run_pipeline.py after a RiskScore exceeds the threshold.
    """
    await manager.broadcast(risk_score)
