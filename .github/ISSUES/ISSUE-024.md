---
title: "Build a Streaming Feature Computation Engine with Sub-Second Latency"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary
The current `detection/feature_engineering.py` recomputes all 35+ features from scratch for every pipeline run, requiring a full scan of historical trades per wallet per window. This batch-mode approach is incompatible with the real-time scoring path triggered by `ingestion/horizon_streamer.py` (SSE stream), where a new trade arrives every few seconds and risk scores must be updated within 500 ms. A streaming feature computation engine uses incremental state updates — adding one trade and expiring old trades at window boundaries — to produce feature updates in O(1) or O(log N) time rather than O(N).

## Background & Context
`detection/feature_engineering.py` contains `extract_features(wallet, trades, graph_results, benford_results)` which iterates over the full trade list multiple times: once per rolling window (1h, 4h, 24h, 7d, 30d) for Benford features, once for trade pattern features (counterparty concentration, round-trip frequency, etc.), and once for volume/timing features. For a wallet with 10,000 historical trades, this takes ~200 ms, which is acceptable for batch scoring but far too slow for the streaming path.

`ingestion/horizon_streamer.py` calls `run_pipeline.py`'s pipeline logic on each incoming trade batch. The streaming path needs a `StreamingFeatureEngine` that:
1. Maintains per-wallet rolling state (deques for each window, running aggregates for each feature)
2. On trade arrival: appends the new trade to all deques, evicts expired trades from the front, updates running aggregates incrementally
3. Returns the current feature vector in < 50 ms per wallet

Key insight: most features are additive aggregates (sum, count, mean) or order statistics (max, min) that can be maintained with simple accumulators. Counterparty concentration requires a `Counter` that supports O(1) increment/decrement. Benford digit histograms are 9-element arrays updated in O(1) per trade.

The streaming engine must be strictly separated from the batch engine; the batch engine remains the authoritative path for training data generation.

## Objectives
- [ ] Implement `StreamingFeatureEngine` class in `detection/feature_engineering.py` (or a new `detection/streaming_features.py`) with `update(trade: Trade) -> FeatureVector` and `get_features(wallet: str) -> FeatureVector` methods
- [ ] Implement incremental state accumulators for all 35 baseline features: rolling deques per window, running sums/counts, `Counter` for counterparty concentration, 9-element digit histograms for Benford
- [ ] Integrate `StreamingFeatureEngine` into `ingestion/horizon_streamer.py` so that each SSE trade batch triggers `engine.update(trade)` and a re-score rather than a full pipeline recompute
- [ ] Add a `latency_ms` field to the `RiskScore` record (or emit a Prometheus-compatible metric) measuring end-to-end time from trade receipt to score update

## Technical Requirements

**Data structures per wallet per window:**
```python
@dataclass
class WindowState:
    window_seconds: int
    trades: deque            # deque of Trade, ordered by timestamp (newest at right)
    digit_hist: np.ndarray   # shape (9,) — count of leading digits 1..9
    volume_sum: float
    trade_count: int
    counterparty_counts: Counter  # {counterparty_wallet: trade_count}
    timestamps: deque        # deque of float (Unix epoch), mirrors trades deque
    # round-trip tracking: last seen trade direction per counterparty
    last_direction: Dict[str, str]  # counterparty → "buy" | "sell"
    round_trip_count: int
```

**Eviction strategy:**
- On each `update(trade)`, after appending, pop from the left of `trades` and `timestamps` while `trade.timestamp - timestamps[0] > window_seconds`
- When popping an evicted trade: decrement `digit_hist[digit(evicted.amount)]`, subtract from `volume_sum`, decrement `trade_count`, decrement `counterparty_counts[evicted.counterparty]`, remove zero-count entries from `counterparty_counts`
- Use `collections.deque` (O(1) append/popleft) not `list`

**Incremental feature computations:**
- `counterparty_concentration_ratio`: `max(counterparty_counts.values()) / trade_count`; recompute in O(|unique_counterparties|) after each update — acceptable since typical wallets have < 100 unique counterparties per window
- `chi2_benford`: recompute from `digit_hist` (9 multiplications and divisions); O(9) per update
- `volume_to_unique_counterparty_ratio`: `volume_sum / len(counterparty_counts)`; O(1)
- `intra_minute_clustering`: maintain a `minute_counts: Counter` mapping `floor(timestamp / 60) → count`; update incrementally; compute `max(minute_counts.values()) / trade_count` in O(1) after update

**Latency budget (per trade, per wallet):**
- Deque append/popleft: < 1 μs
- Digit extraction + histogram update: < 5 μs
- Benford chi-square recompute (9 ops): < 10 μs
- Counterparty counter update: < 5 μs
- Full feature vector serialisation to `np.ndarray`: < 100 μs
- **Total target: < 500 μs per wallet per trade**; the 50 ms budget allows up to 100 wallets to be updated per trade arrival

**Consistency guarantee:**
- `StreamingFeatureEngine.get_features(wallet)` and `extract_features(wallet, trades)` (batch) must produce identical feature vectors for the same trade history to within floating-point rounding (tolerance: 1e-9)
- Add a `validate_consistency(wallet, trades)` method that runs both paths and asserts agreement; call this in integration tests

**Persistence:**
- Window state must survive process restarts; serialise `WindowState` to the SQLite store using `detection/storage.py`'s existing connection, adding a `streaming_window_state` BLOB column to the `risk_scores` table or a new dedicated table
- On startup, `StreamingFeatureEngine` loads persisted state for all wallets seen in the last 30 days

## Security Considerations
- `Trade` objects from SSE must be validated against `ingestion/data_models.py` Pydantic schema before being passed to `update()`; malformed trades must be rejected, not silently skipped
- The `digit_hist` array must be bounds-checked: digit extraction must return a value in [1, 9]; an out-of-range index must raise `ValueError` not silently corrupt the histogram
- Window state serialised to SQLite must be stored as a versioned JSON blob (not raw pickle) to prevent deserialization of arbitrary objects on startup

## Testing Requirements
- Unit tests covering:
  - Single trade update: verify digit histogram, volume sum, trade count correct
  - Window eviction: add trade at T=0, advance clock to T=window+1, verify old trade evicted from all accumulators
  - Counterparty concentration after 10 trades to same counterparty, then eviction
  - Round-trip detection: buy then sell to same counterparty within window → `round_trip_count = 1`
- Integration tests covering:
  - Consistency test: 1000-trade sequence produces same feature vector from streaming and batch engines
  - Latency test: 100 sequential `update()` calls complete in < 50 ms total (pytest benchmark or `time.perf_counter`)
  - State persistence round-trip: serialise state, reload, verify `get_features()` unchanged
- Edge cases:
  - Trade with `amount = 0` (skip digit extraction gracefully)
  - Trade timestamp older than current window minimum (late-arriving trade: append but mark as `out_of_order`)
  - Window with 0 trades after mass eviction → all features zero, no division by zero

## Documentation Requirements
- Update `detection/feature_engineering.py` (or new `detection/streaming_features.py`) with class-level docstring explaining the incremental accumulator architecture
- Add `STREAMING_LATENCY_BUDGET_MS` to `config/settings.py` with a comment
- Add a `docs/streaming_feature_engine.md` covering the data structure choices, eviction strategy, and consistency guarantee with the batch engine

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty
- Relevant experience with: Python data structures (`deque`, `Counter`), streaming/online algorithms, real-time feature pipelines, async Python
- Your approach or initial thoughts on state persistence strategy
- Estimated time to complete

**Ideal contributor profile:** Python engineer with experience building online/incremental machine learning feature pipelines; familiarity with event-driven architectures and real-time data processing is essential.
