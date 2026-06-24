---
title: "Add Backpressure and Flow Control to the Real-Time Trade Streamer"
labels: ["difficulty: advanced", "area: ingestion", "type: feature"]
assignees: []
---

## Summary
`horizon_streamer.py` currently passes ingested trade events directly to the detection pipeline without any queue depth limits or producer throttling. Under high-volume conditions — Stellar ledger spikes, burst trading activity, or a slow detection pipeline — unbounded buffering causes runaway memory growth and can OOM-kill the process. Implementing proper backpressure and flow control will bound memory usage, make the system observable, and prevent detection pipeline slowness from cascading into ingestion failures.

## Background & Context
The LedgerLens ingestion architecture (see README Layer 1) has `horizon_streamer.py` consuming from the Horizon SSE endpoint and feeding events into `detection/feature_engineering.py`. In the current implementation the handoff between these layers is either a direct synchronous call or an unbounded `asyncio.Queue`. Neither provides backpressure.

When the detection pipeline slows down (e.g., during model inference with a large feature batch, or during SHAP computation in `detection/shap_explainer.py`), the producer continues to read from Horizon SSE at full speed, accumulating events in memory. On the Stellar DEX, trading bursts during token launches or market volatility events can produce hundreds of trades per second, making this a realistic production failure mode.

The standard solution is a **bounded queue with producer-side backpressure**: the producer blocks (or drops with metrics) when the queue is full. The chosen strategy must be configurable because different deployment environments have different trade-off preferences:
- **Block**: Zero event loss, but Horizon SSE disconnects if the client stops reading the TCP stream for too long (Horizon typically closes idle SSE connections after ~30 seconds).
- **Drop newest**: Bounded memory, some event loss — acceptable if the historical loader can backfill gaps.
- **Drop oldest**: Bounded memory, prioritizes recency — appropriate for real-time scoring where stale events are less valuable.

## Objectives
- [ ] Replace any unbounded queue or direct call in `HorizonStreamer` with a `BoundedTradeQueue` that enforces a configurable maximum depth, with a configurable overflow strategy (`block`, `drop_newest`, `drop_oldest`).
- [ ] Implement producer-side throttling: when the queue exceeds a configurable high-water mark (e.g., 80% full), slow the SSE read loop using `asyncio.sleep` with exponential back-off before each `queue.put` attempt.
- [ ] Add per-strategy dropped-event counters and expose them via a `StreamerMetrics` dataclass accessible from the outside (used by ISSUE-015 Prometheus export).
- [ ] Add a `--queue-depth` and `--overflow-strategy` option to `cli.py stream`.

## Technical Requirements

**`BoundedTradeQueue` interface:**
```python
class BoundedTradeQueue:
    def __init__(
        self,
        maxsize: int = 1000,
        overflow_strategy: Literal["block", "drop_newest", "drop_oldest"] = "drop_oldest",
    ): ...

    async def put(self, trade: Trade) -> bool:
        """Enqueue trade. Returns True if accepted, False if dropped."""

    async def get(self) -> Trade: ...

    def depth(self) -> int: ...
    def dropped_count(self) -> int: ...
    def high_water_mark_reached_count(self) -> int: ...
```

**High-water-mark throttling** — inside `HorizonStreamer._stream_loop`:
```python
HIGH_WATER_RATIO = 0.8

async def _maybe_throttle(self):
    ratio = self.queue.depth() / self.queue.maxsize
    if ratio >= HIGH_WATER_RATIO:
        delay = min(0.05 * (2 ** self._throttle_level), 2.0)  # cap at 2s
        self._throttle_level += 1
        await asyncio.sleep(delay)
    else:
        self._throttle_level = max(0, self._throttle_level - 1)
```

**`StreamerMetrics` dataclass:**
```python
@dataclass
class StreamerMetrics:
    events_received: int = 0
    events_queued: int = 0
    events_dropped: int = 0
    queue_depth_current: int = 0
    queue_depth_peak: int = 0
    high_water_mark_hits: int = 0
    throttle_sleep_total_seconds: float = 0.0
    last_event_at: datetime | None = None
```

Metrics must be thread-safe: use `threading.Lock` or `asyncio.Lock` depending on the concurrency model; prefer `asyncio.Lock` since the streamer is async.

**Overflow strategies:**
- `drop_newest`: if queue full, discard the incoming event and increment `events_dropped`
- `drop_oldest`: if queue full, call `queue.get_nowait()` to discard the oldest event, then enqueue the new one
- `block`: `await queue.put(trade)` — blocks until space is available; Horizon SSE TCP pressure provides natural back-pressure to the server

**Queue depth configuration** (add to `config/settings.py`):
- `STREAMER_QUEUE_MAXSIZE`: default `1000`
- `STREAMER_OVERFLOW_STRATEGY`: default `"drop_oldest"`
- `STREAMER_HIGH_WATER_RATIO`: default `0.8`

**Dropped-event logging**: log at `WARNING` level every time `events_dropped` crosses a multiple of 100, including the current queue depth, to avoid log flooding while keeping operators informed.

**Metrics snapshot interval**: `HorizonStreamer` should expose a `metrics_snapshot() -> StreamerMetrics` method that returns a copy of the current metrics, callable from the API layer or the Prometheus exporter.

## Security Considerations
- Queue depth limits prevent a malicious or misbehaving Horizon endpoint from causing unbounded memory growth (a form of resource exhaustion / DoS mitigation).
- The `drop_oldest` strategy could theoretically be exploited by an attacker who floods the stream with low-value events to push high-value events out of the queue. Document this trade-off; operators should prefer `block` in high-security environments and rely on the checkpoint layer (ISSUE-001) to recover gaps.
- Metrics exposed via the API (ISSUE-015) must not include raw event content — only aggregate counts and depths.
- Throttle sleep durations must be bounded (cap at 2 seconds as shown above) to prevent the streamer from stalling indefinitely on a slow consumer.

## Testing Requirements
- Unit tests covering `BoundedTradeQueue`: put/get under capacity, put at capacity with each overflow strategy, depth/dropped_count accuracy
- Unit tests covering `_maybe_throttle`: verify sleep is called with correct delay at high-water mark, verify throttle level resets when queue drains
- Unit tests covering `StreamerMetrics`: verify all counters increment correctly on each code path
- Integration tests: mock a fast Horizon SSE producer and a slow consumer (artificial `asyncio.sleep` in consumer); assert queue depth stabilizes at `maxsize` and dropped count increments (for `drop_newest`/`drop_oldest`); assert no unbounded memory growth
- Integration tests: verify that `block` strategy causes the producer coroutine to pause (use `asyncio.wait_for` with timeout to assert it does not return immediately when queue is full)
- Edge cases: queue `maxsize=1`, overflow on first event, metrics snapshot during concurrent puts/gets, rapid strategy changes (not supported — must raise `RuntimeError` if changed after construction)
- Performance benchmark: `drop_oldest` strategy should sustain > 5,000 events/second through the queue on a single core without memory growth beyond `maxsize * sizeof(Trade)`

## Documentation Requirements
- Update `README.md` CLI Reference section for `cli.py stream` with new `--queue-depth` and `--overflow-strategy` flags
- Add docstrings to `BoundedTradeQueue`, `StreamerMetrics`, and `_maybe_throttle` explaining the backpressure model
- Update `config/settings.py` inline comments for new settings
- Add a `docs/ingestion.md` section on flow control, overflow strategies, and when to choose each

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: Python `asyncio`, bounded queues, backpressure patterns, SSE/WebSocket streaming
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Python async engineer with experience designing producer-consumer pipelines with backpressure. Familiarity with `asyncio.Queue`, high-throughput async event processing, and systems observability (metrics, logging) is essential. Experience with streaming systems like Kafka or similar is a strong plus.
