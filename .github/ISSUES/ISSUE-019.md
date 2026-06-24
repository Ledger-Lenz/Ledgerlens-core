---
title: "Build Streaming Benford Digit-Frequency Counter with Configurable Window Sizes"
labels: ["difficulty: intermediate", "area: detection", "type: feature"]
assignees: []
---

## Summary
The Benford engine currently recomputes digit frequencies from scratch over the full trade history on every analysis run, which is O(n) per call and becomes prohibitive for wallets with large trade counts. A streaming digit-frequency counter — maintaining running digit histograms over configurable sliding windows — enables O(1) incremental updates and sub-millisecond Benford analysis on each new trade event.

## Background & Context
`detection/benford_engine.py` computes Benford's Law conformity by tallying the leading-digit distribution of trade amounts across a wallet's history. For high-frequency traders with tens of thousands of events, this full-rescan approach creates latency spikes in the real-time scoring path. The fix is a streaming counter that maintains per-window digit tallies and updates them in constant time.

The Stellar Horizon SSE stream delivers trades at up to hundreds per second during peak activity. To keep the real-time scorer under the 50ms p99 latency SLA, Benford feature extraction must be incremental.

## Objectives
- [ ] Implement `BenfordStreamCounter` class in `detection/benford_engine.py` with `update(amount)` and `window_stats(window_size)` methods
- [ ] Support multiple simultaneous windows (e.g., 100, 500, 1000 trades) using a circular buffer per window
- [ ] Expose chi-square statistic, MAD, and Z-scores from each window via `window_stats()`
- [ ] Integrate `BenfordStreamCounter` into `detection/model_inference.py` so Benford features are computed incrementally
- [ ] Benchmark: verify O(1) `update()` and O(9) `window_stats()` complexity

## Technical Requirements

```python
class BenfordStreamCounter:
    BENFORD_EXPECTED = [0.301, 0.176, 0.125, 0.097, 0.079, 0.067, 0.058, 0.051, 0.046]

    def __init__(self, windows: list[int] = [100, 500, 1000]):
        # circular buffer per window, digit tally array
        ...

    def update(self, amount: float) -> None:
        """O(len(windows)) update — extract leading digit, update all window tallies."""
        ...

    def window_stats(self, window: int) -> BenfordStats:
        """Return chi_square, mad, z_scores for the given window size."""
        ...
```

Window sizes and defaults should be configurable via `BENFORD_WINDOWS=100,500,1000` env var.

## Definition of Done
- [ ] `BenfordStreamCounter.update()` runs in < 1 µs on a standard dev machine
- [ ] `window_stats()` matches batch-computed results to within floating-point tolerance
- [ ] Tests cover window rollover, single-digit amounts, and zero-trade edge case
- [ ] Integrated into real-time scoring path and confirmed < 1ms Benford feature extraction

## For Contributors
Ideal background: streaming algorithms, circular buffers, statistical computing in Python. Comment with your approach to handling the circular buffer rollover and how you will validate numerical equivalence with the batch implementation.
