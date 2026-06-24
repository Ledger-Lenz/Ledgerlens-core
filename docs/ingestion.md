# Ingestion Pipeline

## Rate Limiting and Backpressure

The Horizon SSE streamer (`ingestion/horizon_streamer.py`) includes three
rate-management components:

### Token Bucket (`ingestion/rate_limiter.py`)

A classic token-bucket algorithm that refills at `HORIZON_RATE_LIMIT` tokens
per second up to `HORIZON_RATE_BUCKET_CAPACITY` tokens. Each SSE event
consumes one token. If no token is available, the consumer blocks
asynchronously until one refills. This smooths burst consumption while
allowing short bursts up to bucket capacity.

### Backpressure Controller

Monitors the downstream processing queue. When `queue.qsize()` reaches
`HORIZON_QUEUE_HIGH_WATERMARK` (default 1000), SSE consumption pauses and a
WARNING log is emitted. Consumption resumes when the queue drains below
`HORIZON_QUEUE_LOW_WATERMARK` (default 500).

### Adaptive Rate Controller

On HTTP 429 from Horizon, the current token rate is halved immediately. The
rate is then restored linearly toward the configured rate over
`RATE_RESTORE_SECONDS` (default 60). This mirrors TCP-style AIMD congestion
control.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `HORIZON_RATE_LIMIT` | 50 | Tokens per second (average request rate) |
| `HORIZON_RATE_BUCKET_CAPACITY` | 100 | Max burst tokens |
| `HORIZON_QUEUE_HIGH_WATERMARK` | 1000 | Pause SSE at this queue depth |
| `HORIZON_QUEUE_LOW_WATERMARK` | 500 | Resume SSE at this queue depth |
| `RATE_RESTORE_SECONDS` | 60 | Seconds to restore rate after 429 |

## Tuning

- **High throughput**: increase `HORIZON_RATE_LIMIT` and capacity, but stay
  within Horizon's published rate limits.
- **Slow downstream**: lower watermarks to engage backpressure earlier.
- **Frequent 429s**: lower `HORIZON_RATE_LIMIT` or increase
  `RATE_RESTORE_SECONDS` for gentler recovery.

## Monitoring

`GET /stream/rate-limiter` (admin-key gated) returns:

```json
{
  "configured_rate": 50.0,
  "current_rate": 50.0,
  "bucket_level": 95.3,
  "backpressure_active": false,
  "queue_size": 42,
  "last_429_at": null
}
```
