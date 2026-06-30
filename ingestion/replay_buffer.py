"""Sequence-gap-detecting replay buffer for OrderBookEvents.

Buffers events keyed by their paging_token/id and emits them in
ledger-sequence order. Gaps in the paging_token sequence are held back
until the missing token arrives or gap_timeout_seconds elapses.
"""
from __future__ import annotations

import heapq
import logging
import time

from ingestion.data_models import OrderBookEvent

logger = logging.getLogger("ledgerlens.replay_buffer")


def _sort_key(token: str) -> tuple:
    """Return a comparable key: numeric tokens sort numerically, others lexicographically."""
    try:
        return (0, int(token), "")
    except ValueError:
        return (1, 0, token)


class OrderBookReplayBuffer:
    """Sequence-gap-detecting replay buffer for OrderBookEvents.

    Buffers events keyed by their paging_token/id and emits them in
    ledger-sequence order. Detects gaps in paging_token sequences and
    holds back events until the gap is filled or the timeout expires.

    Args:
        max_size: Maximum events to buffer before forcing a flush.
        gap_timeout_seconds: Seconds to wait for a gap to be filled before
            emitting out-of-order events anyway.
    """

    def __init__(self, max_size: int = 1000, gap_timeout_seconds: float = 30.0) -> None:
        self._max_size = max_size
        self._gap_timeout = gap_timeout_seconds
        # Min-heap entries: (_sort_key(token), token)
        self._heap: list[tuple] = []
        self._events: dict[str, OrderBookEvent] = {}
        self._ingested_at: dict[str, float] = {}
        # Tracks the last token *emitted* so we can detect gaps against it.
        # None means nothing has been emitted yet.
        self._last_emitted_key: tuple | None = None

    def ingest(self, event: OrderBookEvent) -> None:
        """Add an event to the buffer."""
        token = event.id
        if token in self._events:
            return  # duplicate — ignore
        key = _sort_key(token)
        heapq.heappush(self._heap, (key, token))
        self._events[token] = event
        self._ingested_at[token] = time.monotonic()
        if len(self._events) >= self._max_size:
            logger.warning("replay_buffer: max_size %d reached, forcing flush", self._max_size)
            self._expire_all()

    def _expire_all(self) -> None:
        """Mark every buffered event as timed-out so drain() will release them all."""
        past = time.monotonic() - self._gap_timeout - 1.0
        for token in self._ingested_at:
            self._ingested_at[token] = past

    def drain(self, now_ts: float | None = None) -> list[OrderBookEvent]:
        """Return events that are ready to emit in sorted order.

        An event is ready when:
        1. Its sort key is consecutive to (or follows) the last emitted key with
           no buffered gap before it, OR
        2. The gap_timeout has elapsed since the event was buffered.
        """
        now = now_ts if now_ts is not None else time.monotonic()
        ready: list[OrderBookEvent] = []

        while self._heap:
            key, token = self._heap[0]
            if token not in self._events:
                heapq.heappop(self._heap)
                continue

            timed_out = (now - self._ingested_at[token]) >= self._gap_timeout

            if not timed_out:
                # Check for a gap: is there any buffered event with a *smaller* key?
                # Since _heap is a min-heap the front IS the smallest key, so
                # there is no smaller buffered event.  The gap question is whether
                # a token *between* _last_emitted_key and this key is missing.
                # For purely numeric tokens we can check exact consecutiveness;
                # for opaque strings we conservatively emit in order without gap
                # detection beyond "nothing smaller is buffered".
                if self._last_emitted_key is not None and key[0] == 0:
                    # Numeric regime: enforce strict consecutiveness.
                    expected_next = self._last_emitted_key[1] + 1
                    if key[1] > expected_next:
                        # There is a numeric gap — hold back.
                        break
                # Non-numeric or no prior emission: emit (heap-min, so no smaller item buffered).

            # Pop and emit.
            heapq.heappop(self._heap)
            del self._ingested_at[token]
            ready.append(self._events.pop(token))
            self._last_emitted_key = key

        return ready

    def flush_all(self) -> list[OrderBookEvent]:
        """Force-flush all buffered events in sorted order (e.g. on shutdown)."""
        result: list[OrderBookEvent] = []
        while self._heap:
            key, token = heapq.heappop(self._heap)
            if token in self._events:
                result.append(self._events.pop(token))
                self._last_emitted_key = key
        self._ingested_at.clear()
        return result

    @property
    def size(self) -> int:
        """Number of events currently buffered."""
        return len(self._events)
