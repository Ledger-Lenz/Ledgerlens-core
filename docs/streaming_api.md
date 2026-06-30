# Score Streaming API (SSE)

## Overview

LedgerLens exposes a push-based streaming API using **Server-Sent Events (SSE)**. Clients subscribe to a set of wallet addresses and receive `score_update` events in real-time as scores change, eliminating the need to poll `/scores/{wallet}`.

**Architecture:**
1. The scoring pipeline publishes a `ScoreUpdateEvent` to Redis (`PUBLISH ledgerlens:score:{wallet}`) after every score change.
2. SSE handlers subscribe to Redis channels and forward events to connected clients.
3. A Redis hash (`ledgerlens:last_event`) stores the last event per wallet for reconnect replay.

## SSE Protocol Reference

### Endpoint

```
GET /stream/scores?wallets=WALLET1,WALLET2,...
```

**Query parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `wallets` | Yes | Comma-separated Stellar wallet addresses (max 50) |
| `Last-Event-ID` | No | Last received event ID for reconnect replay |

**Response headers:**

```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

### Event Format

```
id: <uuid4>
event: score_update
data: {"wallet":"GABC...","previous_score":65,"current_score":82,...}

```

Each event is separated by a blank line. The `id:` field enables client-side reconnect.

### Event Schema

```json
{
  "wallet": "GABC...XYZ",
  "previous_score": 65,
  "current_score": 82,
  "delta": 17,
  "crossed_threshold": 70,
  "triggered_by": "ingestion",
  "namespace_id": "ns_default",
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "published_at": "2026-06-30T12:00:00.123456+00:00"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `wallet` | string | Stellar wallet address |
| `previous_score` | int | Score before recompute |
| `current_score` | int | Score after recompute |
| `delta` | int | `current_score - previous_score` |
| `crossed_threshold` | int\|null | Alert threshold crossed (50/70/85/95), or null |
| `triggered_by` | string | `"ingestion"` \| `"recompute"` \| `"feedback_boost"` |
| `namespace_id` | string | Namespace this wallet belongs to |
| `event_id` | string | UUID4 — use as `Last-Event-ID` on reconnect |
| `published_at` | ISO 8601 | UTC timestamp of publication |

### Heartbeat

When no events arrive, a heartbeat comment is emitted every 15 seconds:

```
: heartbeat

```

This keeps the connection alive through proxies and load balancers that would otherwise close idle connections.

## Reconnect Semantics

Browser's `EventSource` API automatically reconnects on disconnect and sends the `Last-Event-ID` header. LedgerLens replays the last cached event for each subscribed wallet if it differs from `Last-Event-ID`.

Replay window: **5 minutes** (`SSE_MISSED_EVENT_REPLAY_WINDOW_SECONDS=300`). Events older than this window are not replayed even if the client requests them — this limits Redis memory usage.

## Wallet Address Validation

Each wallet address is validated against the Stellar public key format:

```
^[A-Z0-9]{56}$
```

If **any** address in the `wallets` parameter is invalid, the entire request returns **HTTP 422**. This prevents Redis channel injection via crafted wallet strings.

## Namespace Isolation

Events are filtered by namespace at the SSE layer. If a wallet belongs to a different namespace than the authenticated client, the event is **silently dropped** (not errored). This prevents leaking that a wallet exists in another namespace.

## Connection Limits

Maximum **10 concurrent SSE connections** per API key. Exceeding this returns **HTTP 429**:

```json
{
  "detail": "Too many concurrent SSE connections for this API key. Maximum is 10."
}
```

The counter is tracked in Redis (`ledgerlens:sse_connections:{key_id}`) and decremented when the connection closes.

## JavaScript Client Example

```javascript
// Subscribe to score updates for two wallets
const wallets = [
    'GABC...XYZ',
    'GDEF...PQR'
].join(',');

const es = new EventSource(
    `http://localhost:8000/stream/scores?wallets=${wallets}`
);

// Handle score updates
es.addEventListener('score_update', (event) => {
    const data = JSON.parse(event.data);
    console.log(`${data.wallet.slice(0,8)}... score: ${data.current_score} (${data.delta >= 0 ? '+' : ''}${data.delta})`);
    
    if (data.crossed_threshold) {
        console.warn(`ALERT: threshold ${data.crossed_threshold} crossed!`);
    }
});

// Handle connection errors (EventSource auto-reconnects)
es.onerror = (err) => {
    console.error('SSE connection error', err);
};

// Reconnect is automatic — EventSource sends Last-Event-ID header
// so missed events are replayed from the last 5 minutes.
```

### With authentication header (modern browsers / Node.js):

```javascript
// Note: EventSource does not support custom headers in all browsers.
// Use fetch + ReadableStream for custom header support:
const response = await fetch(
    `http://localhost:8000/stream/scores?wallets=${wallets}`,
    {
        headers: {
            'X-LedgerLens-Admin-Key': 'your-admin-key',
            'Accept': 'text/event-stream'
        }
    }
);

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // Parse SSE events from buffer...
}
```

## Stats Endpoint

```
GET /stream/stats
```

Returns streaming statistics:

```json
{
  "active_connections": 12,
  "events_last_60min": 348,
  "top_wallets": [
    {"wallet": "GABC...XYZ", "subscribers": 5},
    {"wallet": "GDEF...PQR", "subscribers": 3}
  ]
}
```

## Configuration

```env
SSE_HEARTBEAT_INTERVAL_SECONDS=15
SSE_MAX_WALLETS_PER_CONNECTION=50
SSE_MISSED_EVENT_REPLAY_WINDOW_SECONDS=300
REDIS_PUBSUB_POOL_SIZE=10
```

## Integration: Publishing from Model Inference

In `detection/model_inference.py`, after writing a new score:

```python
from api.streaming import ScoreUpdateEvent, check_threshold_crossing

if new_score != previous_score:
    event = ScoreUpdateEvent(
        wallet=wallet,
        previous_score=previous_score,
        current_score=new_score,
        delta=new_score - previous_score,
        crossed_threshold=check_threshold_crossing(previous_score, new_score),
        triggered_by=trigger,  # "ingestion" | "recompute" | "feedback_boost"
        namespace_id=namespace_id,
        event_id=str(uuid.uuid4()),
    )
    await score_publisher.publish(event)
```

The `ScorePublisher` instance should be created once at startup and reused:

```python
import redis.asyncio as aioredis
from api.streaming import ScorePublisher

redis_client = aioredis.from_url(settings.redis_url)
score_publisher = ScorePublisher(redis_client)
```

## Proxy / Load Balancer Configuration

For Nginx, disable response buffering:

```nginx
location /stream/scores {
    proxy_pass http://ledgerlens-api;
    proxy_buffering off;
    proxy_cache off;
    proxy_set_header Connection '';
    proxy_http_version 1.1;
    chunked_transfer_encoding on;
}
```

The `X-Accel-Buffering: no` header is set automatically by the SSE endpoint.
