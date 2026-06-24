---
title: "Implement Incremental Benford Engine with O(1) Window Updates"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary

Replace the full-recompute approach in `detection/benford_engine.py` with an incremental digit-count accumulator scheme. Each of the five time windows (1h, 4h, 24h, 7d, 30d) maintains a running `DigitCounter` object; new transactions increment counts and expired transactions decrement them, avoiding O(n) scans on every scoring call. This reduces Benford feature computation from O(n) per window per wallet to O(1) per new transaction, enabling real-time scoring on the streaming pipeline.

## Background & Context

`detection/benford_engine.py` currently computes Benford features by loading all transactions within a window, extracting leading digits, and computing chi-square, Z-scores, and MAD from scratch. For the batch pipeline this is acceptable, but for `cli.py stream` — processing a new trade every few milliseconds — this becomes a bottleneck: at 500 trades/second, O(n) recomputation on a 24-hour window of 50,000 trades means 25 million operations per second just for Benford features.

The incremental approach maintains 9-element digit count arrays `[n1, n2, ..., n9]` per window. When a new trade arrives, increment the corresponding digit bucket. When a trade expires from the window (its timestamp is older than the window size), decrement its digit bucket. The chi-square, Z-score, and MAD can then be computed in O(9) = O(1) from the current counts.

The complication is the expiry mechanism: we need to know which transactions expired from each window. This requires a per-wallet, per-window sorted event queue (by timestamp). Transactions are appended on arrival and popped from the front when expired.

For the 1h and 4h windows, a `collections.deque` is sufficient. For the 7d and 30d windows, where the event count can reach millions, a SQLite-backed expiry queue is needed to avoid unbounded memory growth.

## Objectives

- [ ] Implement `DigitCounter` dataclass with 9-element count array and methods for increment, decrement, chi-square, Z-score, and MAD computation
- [ ] Implement `IncrementalWindow` that wraps a `DigitCounter` and an expiry deque/queue; handles `add_transaction` and `expire_transactions` efficiently
- [ ] Implement `IncrementalBenfordEngine` with five `IncrementalWindow` instances per wallet
- [ ] Replace the full-recompute `compute_benford_features` function with `IncrementalBenfordEngine.update(wallet, amount, timestamp)` and `IncrementalBenfordEngine.get_features(wallet)`
- [ ] For windows > 4h, use SQLite-backed expiry queue (not in-memory deque) to bound memory
- [ ] Implement `DigitCounter.to_dict()` and `from_dict()` for SQLite serialisation (JSON blob)
- [ ] Verify that incremental results match the reference full-recompute results to within floating-point tolerance (1e-9) — add a correctness test
- [ ] Benchmark: `update()` for a new transaction in < 100µs p99

## Technical Requirements

### DigitCounter

```python
# detection/benford_engine.py

import math
import numpy as np
from dataclasses import dataclass, field

BENFORD_EXPECTED = np.array([
    math.log10(1 + 1/d) for d in range(1, 10)
])  # shape (9,), sums to 1.0

@dataclass
class DigitCounter:
    counts: np.ndarray = field(default_factory=lambda: np.zeros(9, dtype=np.int64))

    def increment(self, amount: float) -> None:
        d = _leading_digit(amount)
        if d is not None:
            self.counts[d - 1] += 1

    def decrement(self, amount: float) -> None:
        d = _leading_digit(amount)
        if d is not None:
            self.counts[d - 1] = max(0, self.counts[d - 1] - 1)

    @property
    def total(self) -> int:
        return int(self.counts.sum())

    def chi_square(self) -> float:
        n = self.total
        if n < 5:
            return 0.0  # insufficient data
        observed = self.counts / n
        return float(np.sum((observed - BENFORD_EXPECTED) ** 2 / BENFORD_EXPECTED) * n)

    def z_scores(self) -> np.ndarray:
        """Returns shape (9,) Z-score per digit."""
        n = self.total
        if n < 5:
            return np.zeros(9)
        observed = self.counts / n
        std = np.sqrt(BENFORD_EXPECTED * (1 - BENFORD_EXPECTED) / n)
        std = np.where(std < 1e-12, 1e-12, std)
        return (observed - BENFORD_EXPECTED) / std

    def mad(self) -> float:
        """Mean Absolute Deviation from Benford distribution."""
        n = self.total
        if n < 5:
            return 0.0
        observed = self.counts / n
        return float(np.mean(np.abs(observed - BENFORD_EXPECTED)))

    def to_dict(self) -> dict:
        return {"counts": self.counts.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "DigitCounter":
        return cls(counts=np.array(d["counts"], dtype=np.int64))


def _leading_digit(amount: float) -> int | None:
    """Return leading significant digit (1-9) or None for 0/negative/NaN."""
    if not math.isfinite(amount) or amount <= 0:
        return None
    s = f"{amount:.10e}"
    return int(s[0])
```

### IncrementalWindow

```python
from collections import deque
from datetime import datetime, timedelta

@dataclass
class TimestampedAmount:
    amount: float
    timestamp: datetime

class IncrementalWindow:
    def __init__(
        self,
        window_label: str,          # e.g. "1h", "24h"
        window_seconds: float,
        use_sqlite_queue: bool,     # True for windows > 4h
        wallet: str,
        db_path: str | None = None,
    ):
        self.counter = DigitCounter()
        self._window_seconds = window_seconds
        self._queue: deque[TimestampedAmount] = deque()  # for short windows
        # For long windows: SQLite table `benford_expiry_queue`

    def add(self, amount: float, ts: datetime) -> None:
        """Add transaction; expire old ones; update counter."""
        self._expire(ts)
        self.counter.increment(amount)
        self._enqueue(amount, ts)

    def _expire(self, now: datetime) -> None:
        """Remove transactions older than window_seconds from counter and queue."""
        cutoff = now - timedelta(seconds=self._window_seconds)
        if self._use_sqlite_queue:
            self._expire_sqlite(cutoff)
        else:
            while self._queue and self._queue[0].timestamp < cutoff:
                old = self._queue.popleft()
                self.counter.decrement(old.amount)
```

### SQLite expiry queue schema

```sql
CREATE TABLE IF NOT EXISTS benford_expiry_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet      TEXT NOT NULL,
    window_label TEXT NOT NULL,
    amount      REAL NOT NULL,
    ts          TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beq_wallet_window_ts
    ON benford_expiry_queue(wallet, window_label, ts ASC);
```

### IncrementalBenfordEngine

```python
WINDOWS = {
    "1h":  3_600,
    "4h":  14_400,
    "24h": 86_400,
    "7d":  604_800,
    "30d": 2_592_000,
}
SQLITE_BACKED_WINDOWS = {"24h", "7d", "30d"}

class IncrementalBenfordEngine:
    def __init__(self, db_path: str): ...

    def update(self, wallet: str, amount: float, timestamp: datetime) -> None:
        """Process one transaction; O(1) per window."""
        ...

    def get_features(self, wallet: str) -> dict[str, float]:
        """
        Return 15 features: chi_sq_{w}, z_score_max_{w}, mad_{w}
        for w in ['1h', '4h', '24h', '7d', '30d'].
        Returns zeros for wallets with < 5 transactions in a window.
        """
        ...

    def persist_counters(self) -> None:
        """Serialise all DigitCounter states to SQLite for restart recovery."""
        ...

    def load_counters(self) -> None:
        """Restore DigitCounter states from SQLite on startup."""
        ...
```

### Counter persistence schema

```sql
CREATE TABLE IF NOT EXISTS benford_counters (
    wallet      TEXT NOT NULL,
    window_label TEXT NOT NULL,
    counts_json TEXT NOT NULL,    -- JSON array of 9 ints
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wallet, window_label)
);
```

### Configuration

```
BENFORD_SQLITE_WINDOW_THRESHOLD_SECONDS=14400   # windows > 4h use SQLite queue
BENFORD_COUNTER_PERSIST_INTERVAL_S=30           # flush counters every 30s
```

## Security Considerations

- **Decrement underflow**: `DigitCounter.decrement` must clamp to 0 — counts can never go negative. This can happen if the engine is restarted without the expiry queue but with persisted counters. Add a consistency check on startup that recomputes from the expiry queue if total < 0 (defensive)
- **SQLite expiry queue size**: the 30d window for 1000 wallets at 100 trades/day = 3 million rows. Enforce a hard cap of 5 million rows with oldest-wallet eviction; log WARNING when > 4 million
- **Amount validation**: all `amount` values must pass `math.isfinite(amount) and amount > 0` before `_leading_digit` is called. Reject and log invalid amounts rather than silently returning None
- **Counter serialisation integrity**: the JSON counts array must always be length-9 with non-negative integers. Validate this on `from_dict` load; log ERROR and reset to zeros if invalid
- **Concurrent access**: `IncrementalBenfordEngine` is not thread-safe by design (single-threaded streaming pipeline). Add an assertion `assert threading.current_thread() is threading.main_thread()` in `update()` to catch accidental multi-threaded use

## Testing Requirements

- [ ] `tests/test_benford_engine.py` — unit tests for `DigitCounter` and `IncrementalWindow`
- [ ] Test: `DigitCounter` chi_square / Z-scores / MAD match reference full-recompute results to within 1e-9 for 1000-sample synthetic datasets
- [ ] Test: `IncrementalWindow.add` then expire produces same counter state as fresh batch computation
- [ ] Test: decrement underflow clamps to 0
- [ ] Test: `_leading_digit` for edge cases: 0, negative, NaN, Inf, very large (1e308), very small (1e-308)
- [ ] Test: counter persist/load round-trip produces identical DigitCounter state
- [ ] Test: SQLite expiry queue cap triggers eviction and logs WARNING
- [ ] Test: correctness — run 10,000 transactions through incremental engine and compare all 15 features against reference batch engine (tolerance 1e-9)
- [ ] Benchmark: `update()` p99 < 100µs for short windows; < 500µs for 30d SQLite window (`@pytest.mark.benchmark`)

## Documentation Requirements

- [ ] Docstrings on `DigitCounter`, `IncrementalWindow`, `IncrementalBenfordEngine`
- [ ] Add `docs/benford_incremental.md` explaining the O(1) update derivation, the SQLite vs deque window split, counter serialisation, and restart recovery procedure
- [ ] Update existing Benford's Law section in `README.md` to mention the incremental engine
- [ ] Document both SQLite tables in `docs/database_schema.md`
- [ ] Update `.env.example` with two new configuration variables

## Definition of Done

- [ ] `DigitCounter`, `IncrementalWindow`, and `IncrementalBenfordEngine` fully implemented
- [ ] Full-recompute `compute_benford_features` replaced (or deprecated with a compatibility shim)
- [ ] Correctness test passes (incremental matches reference to 1e-9)
- [ ] Benchmark passes (< 100µs p99 update for short windows)
- [ ] Counter persistence and restart recovery verified by test
- [ ] SQLite expiry queue with hard cap implemented
- [ ] All existing Benford tests pass without modification
- [ ] `docs/benford_incremental.md` authored

## For Contributors

**Ideal contributor profile**: You have experience building high-performance streaming aggregation systems — sliding window statistics, running averages, or real-time feature computation pipelines. You understand the expiry/eviction mechanics of time-series windows and are comfortable with the performance characteristics of Python deques vs SQLite for different data volumes. Strong numerical Python skills (NumPy) and familiarity with the existing Benford's Law implementation are expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "streaming aggregation systems", "real-time feature engineering", "high-performance Python"
2. **Relevant experience** — systems where you built O(1) sliding-window aggregations; any profiling work on Python numeric pipelines
3. **Approach / initial thoughts** — your thoughts on the SQLite-backed expiry queue for 30d windows vs alternatives (e.g., Redis sorted sets with ZRANGEBYSCORE); any concerns about the decrement-underflow edge case
4. **Estimated time** — breakdown by component (DigitCounter, IncrementalWindow, engine, persistence, tests, docs)
