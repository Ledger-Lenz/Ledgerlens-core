"""FastAPI router for SSE score streaming endpoints.

Endpoints
---------
GET /stream/scores?wallets=W1,W2,...
    SSE stream. Clients subscribe by providing comma-separated Stellar wallet
    addresses (max 50).  Supports reconnect via ``Last-Event-ID`` header.

GET /stream/stats
    Returns active connection count, events in last 60 min, and top-10
    wallets by subscriber count.

Security
--------
- Wallet addresses validated against Stellar public key regex ``^[A-Z0-9]{56}$``.
  Invalid address → 422 for the entire request.
- Connection limit: 10 concurrent SSE connections per API key (429 when exceeded).
- Namespace isolation enforced at the SSEConnectionManager layer.

Example (JavaScript EventSource client)
-----------------------------------------
    const es = new EventSource(
        'http://localhost:8000/stream/scores?wallets=GABC...XYZ',
        { headers: { 'X-LedgerLens-Admin-Key': 'your-key' } }
    );
    es.addEventListener('score_update', (e) => {
        const data = JSON.parse(e.data);
        console.log(data.wallet, data.current_score, data.delta);
    });
    // On disconnect, EventSource auto-reconnects using the last event ID.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.streaming import (
    SSEConnectionManager,
    _MAX_CONNECTIONS_PER_KEY,
    _decrement_connection_count,
    _increment_connection_count,
    _validate_wallet_address,
)

router = APIRouter(prefix="/stream", tags=["Score Streaming (SSE)"])

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_manager: Optional[SSEConnectionManager] = None


def _get_manager() -> SSEConnectionManager:
    global _manager
    if _manager is None:
        try:
            import redis.asyncio as aioredis

            from config.settings import settings

            redis_client = aioredis.from_url(
                settings.redis_url,
                max_connections=settings.redis_pubsub_pool_size,
                decode_responses=True,
            )
            _manager = SSEConnectionManager(
                redis_pool=redis_client,
                heartbeat_interval=settings.sse_heartbeat_interval_seconds,
                replay_window_seconds=settings.sse_missed_event_replay_window_seconds,
                max_wallets=settings.sse_max_wallets_per_connection,
            )
        except ImportError:
            # Redis not available — return a no-op manager
            _manager = _FallbackManager()
    return _manager


class _FallbackManager:
    """No-op SSE manager when Redis is unavailable."""

    _active_connections: dict = {}

    async def subscribe(self, *args, **kwargs):
        yield ": Redis unavailable — SSE streaming requires Redis.\n\n"

    async def get_stats(self) -> dict:
        return {"active_connections": 0, "events_last_60min": 0, "top_wallets": []}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class StreamStatsResponse(BaseModel):
    active_connections: int
    events_last_60min: int
    top_wallets: list


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/scores",
    summary="Subscribe to real-time score updates via SSE",
    description=(
        "Server-Sent Events endpoint. Provide comma-separated Stellar wallet "
        "addresses (max 50) to subscribe to score updates. On reconnect, send "
        "the `Last-Event-ID` header to replay missed events from the last 5 minutes."
    ),
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}},
        422: {"description": "Invalid wallet address format."},
        429: {"description": "Connection limit exceeded for this API key."},
    },
)
async def stream_scores(
    request: Request,
    wallets: str = Query(
        ...,
        description=(
            "Comma-separated Stellar wallet addresses to subscribe to "
            f"(max {_MAX_CONNECTIONS_PER_KEY * 5} chars per address, "
            "max 50 wallets per connection)."
        ),
    ),
    last_event_id: Optional[str] = Query(
        None,
        alias="Last-Event-ID",
        description="Last received SSE event ID for reconnect replay.",
    ),
) -> StreamingResponse:
    """SSE endpoint for real-time score updates.

    Clients receive ``score_update`` events whenever a watched wallet's score
    changes.  A ``: heartbeat`` comment is emitted every 15 seconds to keep
    the connection alive through proxies.

    Reconnect: if the connection drops, the browser's EventSource API
    automatically reconnects and sends ``Last-Event-ID``.  Missed events
    from the last 5 minutes are replayed.
    """
    # Parse and validate wallet list
    raw_wallets = [w.strip() for w in wallets.split(",") if w.strip()]
    invalid = [w for w in raw_wallets if not _validate_wallet_address(w)]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid wallet address(es): {', '.join(invalid[:3])}"
                + (" ..." if len(invalid) > 3 else "")
                + ". Expected Stellar public key format: 56 uppercase alphanumeric characters."
            ),
        )

    wallet_list = raw_wallets[:_MAX_CONNECTIONS_PER_KEY * 5]  # hard cap

    # Connection limit per API key (best-effort when Redis available)
    api_key_id = request.headers.get("X-LedgerLens-Admin-Key", "anonymous")
    connection_id = str(uuid.uuid4())

    manager = _get_manager()

    # Try to enforce connection limit
    try:

        if hasattr(manager, "_redis_pool"):
            count = await _increment_connection_count(manager._redis_pool, api_key_id)
            if count > _MAX_CONNECTIONS_PER_KEY:
                await _decrement_connection_count(manager._redis_pool, api_key_id)
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Too many concurrent SSE connections for this API key. "
                        f"Maximum is {_MAX_CONNECTIONS_PER_KEY}."
                    ),
                )
    except HTTPException:
        raise
    except Exception:
        pass  # Redis unavailable — skip limit enforcement

    async def event_generator():
        try:
            async for chunk in manager.subscribe(
                connection_id=connection_id,
                wallets=wallet_list,
                last_event_id=last_event_id,
                request=request,
            ):
                yield chunk
        finally:
            try:

                if hasattr(manager, "_redis_pool"):
                    await _decrement_connection_count(manager._redis_pool, api_key_id)
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/stats",
    response_model=StreamStatsResponse,
    summary="SSE streaming statistics",
    description="Returns active connection count, events in last 60 minutes, and top-10 wallets by subscriber count.",
)
async def get_stream_stats() -> StreamStatsResponse:
    """Return SSE streaming statistics."""
    manager = _get_manager()
    stats = await manager.get_stats()
    return StreamStatsResponse(**stats)
