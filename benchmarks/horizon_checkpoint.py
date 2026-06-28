"""Standalone performance check for Horizon cursor checkpoint flushing."""

from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path

from ingestion.checkpoint import CursorCheckpoint, FlushPolicy


def main() -> None:
    policy = FlushPolicy()
    write_latencies: list[float] = []
    events_since_flush = 0
    last_flush = time.monotonic()
    started = last_flush

    with tempfile.TemporaryDirectory() as directory:
        checkpoint = CursorCheckpoint(Path(directory) / "cursor.json")
        for event_number in range(10_000):
            events_since_flush += 1
            now = time.monotonic()
            if policy.should_flush(events_since_flush, last_flush, now):
                write_started = time.perf_counter()
                checkpoint.save(f"{event_number + 1}-0")
                write_latencies.append((time.perf_counter() - write_started) * 1000)
                events_since_flush = 0
                last_flush = now

    elapsed = time.monotonic() - started
    p99_ms = statistics.quantiles(write_latencies, n=100)[98]
    print(f"10,000 events: {elapsed:.3f}s; checkpoint write p99: {p99_ms:.3f}ms")
    if elapsed >= 2.0 or p99_ms >= 5.0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
