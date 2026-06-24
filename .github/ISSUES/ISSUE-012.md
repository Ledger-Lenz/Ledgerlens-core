---
title: "Build an Order-Book Event Replay Buffer for Gap Recovery"
labels: ["difficulty: advanced", "area: ingestion", "type: reliability"]
assignees: []
---

## Summary
`ingestion/operations_loader.py` ingests offer-create, offer-update, and offer-cancel events from Horizon's `/operations` endpoint to populate the `order_cancellation_rate` feature used by the ML classifiers. Horizon's SSE event stream occasionally delivers events out of order or with gaps (due to network interruptions or Horizon node failover), which corrupts the offer-lifecycle state machine and produces incorrect cancellation rates. An order-book event replay buffer with sequence-gap detection and ordered replay will ensure the feature engineering pipeline always receives events in correct ledger sequence.

## Background & Context
`ingestion/operations_loader.py` processes three operation types that form the lifecycle of an order-book offer:
1. `manage_sell_offer` (create) — a new offer is placed on the order book
2. `manage_sell_offer` (update) — an existing offer changes price or amount
3. `manage_sell_offer` (cancel / set amount=0) — the offer is cancelled

The `order_cancellation_rate` feature (see README feature groups) measures what fraction of an account's offers are cancelled before filling. A wash trader typically places large offers to create false depth and cancels them without filling — a high cancellation rate is a significant fraud signal.

The feature computation is stateful: to determine whether an offer was cancelled, the loader must track the full lifecycle of every `offer_id`. If events arrive out of order (e.g., a cancel event arrives before the create event for the same offer), the state machine produces a wrong result:
- Seeing a cancel without a prior create means the offer appears to have been "created cancelled" — a phantom offer that inflates the cancel count
- Seeing an update without a create means the offer's initial state is unknown — the update is applied to a missing baseline

Horizon's SSE stream for operations is ordered by `paging_token` (which encodes ledger sequence and operation index). However, network retries, SSE reconnections, and multi-worker scenarios (from ISSUE-003's parallel loader) can deliver events out of order relative to their paging tokens.

## Objectives
- [ ] Implement an `OperationReplayBuffer` in `ingestion/operations_loader.py` that collects incoming `OrderBookEvent` objects, detects sequence gaps (missing paging tokens), and only releases events to the offer-lifecycle state machine once they can be delivered in strict `paging_token` order.
- [ ] Implement `OfferStateTracker` that maintains a dict of `{offer_id: OfferState}` and applies ordered events to correctly compute per-account `offer_cancellation_count`, `offer_fill_count`, and `offer_update_count`.
- [ ] Add sequence gap detection: if the gap between the last delivered `paging_token` and the next expected token exceeds a configurable timeout (default: 30 seconds), flush the buffer and emit a `GAP_DETECTED` warning rather than stalling the pipeline indefinitely.
- [ ] Expose `ReplayBufferStats` (buffer depth, gap count, out-of-order count, flushed-with-gap count) for observability.

## Technical Requirements

**Paging token ordering**: Horizon paging tokens for operations have the format `{ledger_sequence}-{operation_index}` (e.g., `50123456-1`). Tokens must be compared lexicographically by `(ledger_sequence, operation_index)` as integers:
```python
def parse_paging_token(token: str) -> tuple[int, int]:
    parts = token.split("-")
    return (int(parts[0]), int(parts[1]))
```

**`OperationReplayBuffer` interface:**
```python
class OperationReplayBuffer:
    def __init__(
        self,
        gap_timeout_seconds: float = 30.0,
        max_buffer_size: int = 10_000,
    ): ...

    def push(self, event: OrderBookEvent) -> list[OrderBookEvent]:
        """
        Add event to buffer. Returns any events that can now be
        delivered in order (contiguous sequence from last delivered).
        """

    def flush(self, reason: str = "manual") -> list[OrderBookEvent]:
        """
        Force-release all buffered events in paging_token order.
        Used on gap timeout or shutdown. Logs a WARNING with reason.
        """

    def check_timeout(self) -> list[OrderBookEvent]:
        """
        If oldest buffered event has been waiting > gap_timeout_seconds,
        flush and return events. Call this periodically (e.g., every second).
        """

    @property
    def stats(self) -> ReplayBufferStats: ...
```

**Buffer internals**: use a `sortedcontainers.SortedList` keyed on `parse_paging_token(event.paging_token)` for O(log n) inserts and O(1) min-element access:
```python
from sortedcontainers import SortedList
self._buffer: SortedList = SortedList(key=lambda e: parse_paging_token(e.paging_token))
self._next_expected: tuple[int, int] | None = None  # None = accept any first event
```

**Contiguous delivery logic**: after each `push`, walk the sorted buffer and release all events whose paging token immediately follows the last delivered token:
```python
def _drain_contiguous(self) -> list[OrderBookEvent]:
    released = []
    while self._buffer:
        head = self._buffer[0]
        head_key = parse_paging_token(head.paging_token)
        if self._next_expected is None or head_key == self._next_expected:
            self._buffer.pop(0)
            released.append(head)
            self._next_expected = self._increment_token(head_key)
        else:
            break
    return released
```

**`OfferStateTracker`:**
```python
class OfferState(Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    PARTIALLY_FILLED = "partially_filled"

@dataclass
class OfferRecord:
    offer_id: str
    account: str
    state: OfferState
    created_at: datetime
    closed_at: datetime | None = None
    fill_fraction: Decimal = Decimal("0")

class OfferStateTracker:
    def apply(self, event: OrderBookEvent) -> None: ...

    def cancellation_rate(self, account: str) -> float:
        """cancelled / (cancelled + filled) for this account."""

    def stats_for_account(self, account: str) -> OfferStats: ...
```

**`ReplayBufferStats`:**
```python
@dataclass
class ReplayBufferStats:
    events_received: int = 0
    events_delivered: int = 0
    events_in_buffer: int = 0
    out_of_order_count: int = 0
    gap_detected_count: int = 0
    flush_with_gap_count: int = 0
    buffer_depth_peak: int = 0
```

**Gap detection**: a gap occurs when `_next_expected` has been set but `_buffer[0]` does not equal `_next_expected` and the oldest event in the buffer has been waiting longer than `gap_timeout_seconds`. In this case, `flush()` is called with `reason="gap_timeout"` and `gap_detected_count` is incremented.

**Max buffer size**: if `len(self._buffer) >= max_buffer_size`, call `flush(reason="buffer_full")` to prevent unbounded memory growth.

**Configuration** (add to `config/settings.py`):
- `REPLAY_BUFFER_GAP_TIMEOUT_SECONDS`: default `30.0`
- `REPLAY_BUFFER_MAX_SIZE`: default `10_000`

## Security Considerations
- `max_buffer_size` must be enforced to prevent a malicious or buggy source from exhausting memory by sending events with large paging token gaps that are never filled.
- `gap_timeout_seconds` must be bounded to a maximum of 300 seconds — a longer timeout would stall the pipeline indefinitely on a persistent gap.
- The `OfferStateTracker` dict of open offers must be bounded: if it grows beyond a configurable limit (default 100,000 open offers), the oldest offers must be evicted with a `WARNING`. An unbounded tracker dict is a memory exhaustion vector.
- All paging token parsing must be wrapped in `try/except ValueError` — a malformed paging token from a tampered event source must not crash the buffer.
- The `flush()` method must log the reason and count of flushed events at `WARNING` level so operators can detect abnormal gap-flush patterns.

## Testing Requirements
- Unit tests covering `parse_paging_token`: valid token, token with extra segments (raise), non-numeric parts (raise)
- Unit tests covering `OperationReplayBuffer.push()`: in-order events (immediate delivery), out-of-order single event (buffered, then delivered when gap fills), duplicate event (ignored), first event (no prior expected token)
- Unit tests covering `check_timeout()`: event buffered for > `gap_timeout_seconds` triggers flush
- Unit tests covering `flush(reason="buffer_full")`: buffer at `max_buffer_size` + 1 triggers flush
- Unit tests covering `OfferStateTracker.apply()`: create→fill, create→cancel, create→partial→cancel, update without prior create (graceful handling)
- Unit tests covering `OfferStateTracker.cancellation_rate()`: 0%, 50%, 100% cancel rates
- Integration tests: inject 100 events in shuffled order; assert all 100 are eventually delivered in paging token order
- Integration tests: inject events with a permanent gap (token 5 never arrives); assert gap flush fires after timeout
- Edge cases: buffer receives same paging token twice (dedup), buffer receives token that is before `_next_expected` (stale, discard), empty buffer timeout check
- Performance benchmark: 10,000 events in random order through the replay buffer should sort and deliver in < 500 ms

## Documentation Requirements
- Add module-level docstring to `ingestion/operations_loader.py` explaining the offer lifecycle state machine and why ordered delivery is required
- Add docstrings to `OperationReplayBuffer`, `OfferStateTracker`, and `parse_paging_token`
- Update `docs/ingestion.md` with a section on the replay buffer, gap detection, and tuning `gap_timeout_seconds`
- Document `sortedcontainers` as a new dependency in `requirements.txt` with a version pin

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: ordered event buffers, sequence gap detection, state machines, `sortedcontainers`, Stellar order-book operations
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Systems engineer with experience building ordered event replay buffers or stream processing systems with gap tolerance (e.g., Kafka consumer groups, TCP reassembly, event sourcing). Familiarity with Stellar's offer lifecycle (manage_sell_offer create/update/cancel) and the Horizon paging token format is a strong plus.
