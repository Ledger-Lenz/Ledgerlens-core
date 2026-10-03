"""WebSocket push channel for real-time risk score alerts (#162, #428).

Endpoint: GET /ws/alerts?api_key=<key>[&wallet_filter=G...]

Authentication: api_key query param compared against settings.admin_api_key.
Heartbeat: ping every 30s; connection dropped if no pong within 60s.
Re-authorization: every settings.stream_auth_recheck_interval_seconds (env var
LEDGERLENS_STREAM_AUTH_RECHECK_INTERVAL_SECONDS, default 30s) the connection's
api_key is re-validated; if it was revoked or rotated the socket is closed with
1008, so revocation takes effect within one interval.  Shorter intervals revoke
faster at the cost of one constant-time key comparison per connection per tick
(nothing is added to the broadcast path); 0 disables the re-check.
Max connections: settings.ws_max_connections (env var LEDGERLENS_WS_MAX_CONNECTIONS, default 100).
"""

import asyncio
import logging
import secrets
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from config.settings import settings
from detection.risk_score import RiskScore

logger = logging.getLogger("ledgerlens.ws")

_HEARTBEAT_INTERVAL = 30  # seconds
_PONG_TIMEOUT = 60         # seconds without pong → drop


@dataclass
class _Conn:
    ws: WebSocket
    wallet_filter: str | None
    last_pong: float = field(default_factory=time.monotonic)
    heartbeat_task: asyncio.Task | None = None
    api_key: str | None = None
    auth_task: asyncio.Task | None = None


def _is_authorized(api_key: str) -> bool:
    return bool(settings.admin_api_key) and secrets.compare_digest(
        api_key, settings.admin_api_key
    )


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, _Conn] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self, ws: WebSocket, wallet_filter: str | None, api_key: str | None = None
    ) -> bool:
        """Accept and register a WebSocket connection.

        Returns False (and closes with 1008) if the connection limit is reached.
        When *api_key* is given it is periodically re-validated for the
        lifetime of the connection.
        """
        if len(self._connections) >= settings.ws_max_connections:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return False

        await ws.accept()
        conn = _Conn(ws=ws, wallet_filter=wallet_filter)
        conn.heartbeat_task = asyncio.create_task(self._heartbeat(id(ws), conn))
        interval = settings.stream_auth_recheck_interval_seconds
        if api_key is not None and interval > 0:
            conn.api_key = api_key
            conn.auth_task = asyncio.create_task(self._auth_recheck(id(ws), conn, interval))
        self._connections[id(ws)] = conn
        logger.info("WS connected id=%d total=%d", id(ws), len(self._connections))
        return True

    def disconnect(self, ws: WebSocket) -> None:
        conn = self._connections.pop(id(ws), None)
        if conn and conn.heartbeat_task:
            conn.heartbeat_task.cancel()
        if conn and conn.auth_task and conn.auth_task is not asyncio.current_task():
            conn.auth_task.cancel()
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
            if conn.auth_task:
                conn.auth_task.cancel()
        self._connections.clear()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat(self, ws_id: int, conn: _Conn) -> None:
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                conn_now = self._connections.get(ws_id)
                if conn_now is None:
                    return
                # Drop stale connection
                if time.monotonic() - conn.last_pong > _PONG_TIMEOUT:
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

    async def _auth_recheck(self, ws_id: int, conn: _Conn, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if self._connections.get(ws_id) is not conn:
                    return
                if not _is_authorized(conn.api_key or ""):
                    logger.warning("WS credentials revoked id=%d, disconnecting", ws_id)
                    try:
                        await conn.ws.close(code=status.WS_1008_POLICY_VIOLATION)
                    except Exception:
                        pass
                    self.disconnect(conn.ws)
                    return
        except asyncio.CancelledError:
            pass

    def record_pong(self, ws: WebSocket) -> None:
        conn = self._connections.get(id(ws))
        if conn:
            conn.last_pong = time.monotonic()

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
    if not _is_authorized(api_key):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not await manager.connect(ws, wallet_filter or None, api_key=api_key):
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
