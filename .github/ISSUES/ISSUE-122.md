---
title: "Build Real-Time Score Streaming Engine with Server-Sent Events and Redis Pub/Sub"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary

All LedgerLens clients currently poll `GET /scores/{wallet}` on a fixed interval to detect score changes. A push-based streaming layer — using Server-Sent Events over HTTP/1.1 and Redis Pub/Sub as the message bus — eliminates polling, reduces latency from alert-generation to analyst notification from minutes to sub-second, and decouples the scoring pipeline from the API layer.

## Background & Context

The current polling model has two compounding problems:

1. **Latency**: an analyst configured to poll every 60 seconds may miss a score crossing the alert threshold by up to 59 seconds. For high-risk events, this window is unacceptable.
2. **Load**: 50 connected analyst clients each polling 200 watched wallets every 60 seconds generate ~167 requests/second to the API even when nothing has changed. This load grows linearly with client count.

The solution is a Server-Sent Events (SSE) endpoint `GET /stream/scores` where clients subscribe to a set of wallet addresses. The scoring pipeline publishes score updates to a Redis channel; the SSE handler subscribes to Redis and forwards events to the relevant HTTP clients. A single Redis message fan-outs to all subscribed clients in O(n_subscribed_clients) without any additional DB queries.

## Objectives

- [ ] Implement `ScorePublisher` that publishes a `ScoreUpdateEvent` to `Redis PUBLISH ledgerlens:score:{wallet}` after every successful `model_inference.py` run
- [ ] Implement `GET /stream/scores?wallets=W1,W2,...` as an SSE endpoint using `fastapi.responses.StreamingResponse`
- [ ] Implement `SSEConnectionManager` that tracks active SSE connections and manages Redis subscriptions
- [ ] Implement a Redis `SUBSCRIBE` listener per SSE connection on the channels matching the requested wallets
- [ ] Emit a `heartbeat` SSE comment every 15 seconds to keep connections alive through proxies and load balancers
- [ ] Implement `GET /stream/stats` returning: active connections count, total events published (last 60 min), top 10 wallets by subscriber count
- [ ] Write tests for: event publish → SSE delivery, client disconnect cleanup, heartbeat emission, malformed wallet filter

## Technical Requirements

### Event schema

```python
# api/streaming.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class ScoreUpdateEvent:
    wallet: str
    previous_score: int
    current_score: int
    delta: int                       # current_score - previous_score
    crossed_threshold: Optional[int] # alert threshold crossed (e.g. 70), or None
    triggered_by: str                # "ingestion" | "recompute" | "feedback_boost"
    namespace_id: str
    event_id: str                    # UUID for SSE event ID (allows client reconnect resume)
    published_at: datetime = field(default_factory=datetime.utcnow)

    def to_sse(self) -> str:
        """
        Serialise to SSE wire format:
          id: <event_id>
          event: score_update
          data: <json payload>
          (blank line)
        """
        import json, dataclasses
        payload = dataclasses.asdict(self)
        payload["published_at"] = self.published_at.isoformat()
        return (
            f"id: {self.event_id}\n"
            f"event: score_update\n"
            f"data: {json.dumps(payload)}\n\n"
        )
```

### Score publisher

```python
class ScorePublisher:
    CHANNEL_PREFIX = "ledgerlens:score:"

    def __init__(self, redis_client): ...

    async def publish(self, event: ScoreUpdateEvent) -> None:
        """
        Publish event to Redis channel ledgerlens:score:{wallet}.
        Also publish to ledgerlens:score:* for clients subscribed to all wallets.
        Store last event per wallet in Redis hash 'ledgerlens:last_event' (TTL 24h).
        """
        channel = f"{self.CHANNEL_PREFIX}{event.wallet}"
        payload = json.dumps(dataclasses.asdict(event), default=str)
        async with self._redis.pipeline() as pipe:
            pipe.publish(channel, payload)
            pipe.publish(f"{self.CHANNEL_PREFIX}*", payload)
            pipe.hset("ledgerlens:last_event", event.wallet, payload)
            pipe.expire("ledgerlens:last_event", 86400)
            await pipe.execute()
```

### SSE connection manager

```python
class SSEConnectionManager:
    def __init__(self, redis_pool): ...

    async def subscribe(
        self,
        connection_id: str,
        wallets: list[str],
        last_event_id: Optional[str],   # from SSE reconnect header
    ) -> AsyncGenerator[str, None]:
        """
        1. Validate wallet addresses (alphanumeric + hyphen, max 64 chars each).
        2. If last_event_id is set, replay any events missed since that event ID
           (look up in Redis 'ledgerlens:last_event' hash).
        3. Subscribe to Redis channels for requested wallets.
        4. Yield SSE events as they arrive; yield heartbeat comments every 15s.
        5. On GeneratorExit / CancelledError, unsubscribe and clean up.
        """
        ...

    async def get_stats(self) -> dict:
        """Return active_connections, events_last_60min, top_10_wallets."""
        ...
```

### SSE endpoint

```python
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from typing import Optional

router = APIRouter()

@router.get("/stream/scores")
async def stream_scores(
    request: Request,
    wallets: str = Query(..., description="Comma-separated wallet addresses, max 50"),
    last_event_id: Optional[str] = Query(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """
    SSE endpoint. Client subscribes by providing wallet addresses.
    Reconnect: client sends Last-Event-ID header; missed events are replayed.
    Max 50 wallets per connection. Excess returns 422.
    """
    wallet_list = [w.strip() for w in wallets.split(",")][:50]
    connection_id = str(uuid.uuid4())

    return StreamingResponse(
        manager.subscribe(connection_id, wallet_list, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",    # disable Nginx proxy buffering
            "Connection": "keep-alive",
        },
    )
```

### Integration point in `model_inference.py`

```python
# After writing the new score to the DB, publish the event:
if new_score != previous_score:
    event = ScoreUpdateEvent(
        wallet=wallet,
        previous_score=previous_score,
        current_score=new_score,
        delta=new_score - previous_score,
        crossed_threshold=_check_threshold_crossing(previous_score, new_score),
        triggered_by=trigger,
        namespace_id=namespace_id,
        event_id=str(uuid.uuid4()),
    )
    await score_publisher.publish(event)
```

### Configuration

```
SSE_HEARTBEAT_INTERVAL_SECONDS=15
SSE_MAX_WALLETS_PER_CONNECTION=50
SSE_MISSED_EVENT_REPLAY_WINDOW_SECONDS=300   # replay events from last 5 min on reconnect
REDIS_PUBSUB_POOL_SIZE=10
```

## Security Considerations

- **Namespace isolation**: `SSEConnectionManager.subscribe` must verify that each requested wallet belongs to the authenticated client's namespace. Silently drop (not error) wallets outside the namespace — leaking that a wallet exists in another namespace is a data exposure
- **Wallet address validation**: validate each wallet against a regex `^[A-Z0-9]{56}$` (Stellar public key format) before subscribing. Reject the entire request with 422 if any address is invalid — prevents Redis channel injection via crafted wallet strings
- **Connection limit per API key**: enforce a maximum of 10 concurrent SSE connections per API key using a Redis counter (`ledgerlens:sse_connections:{key_id}`). Return 429 when exceeded to prevent resource exhaustion
- **Heartbeat prevents zombie connections**: SSE connections where the client has disconnected but the TCP session has not timed out will block the generator. The `request.is_disconnected()` check inside the heartbeat loop must trigger generator cleanup within one heartbeat interval
- **Event replay window**: the 5-minute replay window (`SSE_MISSED_EVENT_REPLAY_WINDOW_SECONDS`) limits how much Redis memory is consumed by `last_event` hashes. Do not replay events older than this window even if the client requests them via `Last-Event-ID`

## Testing Requirements

- [ ] `tests/test_streaming.py`
- [ ] Test: `ScorePublisher.publish` writes to both the wallet-specific channel and the wildcard channel
- [ ] Test: `ScoreUpdateEvent.to_sse()` produces valid SSE wire format (parseable by a standard SSE client)
- [ ] Test: SSE connection receives events in <100ms after `ScorePublisher.publish` (async integration test with a mock Redis)
- [ ] Test: client subscribing to wallet outside their namespace receives no events for that wallet
- [ ] Test: invalid wallet address in `wallets` query param returns 422 for the entire request
- [ ] Test: `Last-Event-ID` reconnect header triggers replay of cached events from Redis hash
- [ ] Test: client disconnect (simulated `GeneratorExit`) triggers Redis unsubscribe and connection counter decrement
- [ ] Test: heartbeat `:` comment emitted every 15 seconds when no events arrive
- [ ] Test: exceeding 10 concurrent connections per API key returns 429

## Documentation Requirements

- [ ] Docstrings on `ScorePublisher`, `SSEConnectionManager`, `ScoreUpdateEvent`
- [ ] `docs/streaming_api.md`: SSE protocol reference, reconnect semantics (`Last-Event-ID`), heartbeat interval, wallet address validation, namespace isolation guarantees, connection limits, example JavaScript `EventSource` client
- [ ] Update `docs/openapi.json` with the `/stream/scores` and `/stream/stats` endpoints (annotate as `text/event-stream`)
- [ ] Update `.env.example` with the four new configuration variables

## Definition of Done

- [ ] `ScorePublisher`, `SSEConnectionManager`, and `ScoreUpdateEvent` fully implemented
- [ ] `GET /stream/scores` and `GET /stream/stats` endpoints live
- [ ] Score updates published from `model_inference.py` on every score change
- [ ] Namespace isolation enforced at the SSE layer
- [ ] Heartbeat and reconnect replay implemented
- [ ] All tests pass including the disconnect cleanup test
- [ ] `docs/streaming_api.md` authored with JavaScript client example

## For Contributors

**Ideal contributor profile**: You have experience building push-based real-time APIs in Python (SSE, WebSockets, or long-polling) with Redis Pub/Sub as the message bus. You are comfortable with FastAPI's `StreamingResponse`, Python async generators, and `asyncio` cancellation semantics. Experience with SSE reconnect protocols (Last-Event-ID) and proxy buffering issues is a strong advantage.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "real-time streaming APIs", "Redis Pub/Sub", "SSE / WebSocket systems"
2. **Relevant experience** — SSE or WebSocket systems you have built at scale; experience with Redis Pub/Sub fan-out; FastAPI streaming response work
3. **Approach / initial thoughts** — SSE vs WebSocket trade-offs for this use case; how you would handle the zombie-connection problem; thoughts on the Redis pipeline for atomic publish+store
4. **Estimated time** — breakdown by component (publisher, connection manager, SSE endpoint, heartbeat, integration with inference, tests, docs)
