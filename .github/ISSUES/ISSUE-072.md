---
title: "Build Stateful Rolling-Window Streaming Scorer for Real-Time Detection"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `cli.py stream` and `detection/model_inference.py` to implement a stateful rolling-window scoring loop. As trades arrive via the Horizon SSE stream, maintain per-wallet rolling feature windows (1h, 4h, 24h), recompute features incrementally, and emit a new `RiskScore` whenever a wallet's score changes by ≥5 points. This replaces the current batch-only scoring with a continuous, low-latency detection pipeline suitable for real-time alerting.

## Background & Context

LedgerLens currently scores wallets in batch mode: `run_pipeline.py` ingests historical data, computes features, and scores all wallets in a single pass. The `cli.py stream` command exists but only streams and stores trades without triggering real-time scoring. This gap means wash-trading activity is only detectable after the batch pipeline runs — potentially hours after the pattern emerges.

Real-time streaming detection requires:
1. **Stateful feature windows**: per-wallet state that accumulates trades within rolling time windows (1h, 4h, 24h), evicting trades older than the window as new ones arrive.
2. **Incremental feature recomputation**: when a new trade arrives for wallet W, recompute only W's features (not all wallets) and re-score W. The 35-feature vector must be recomputable from the rolling window state without replaying the full trade history.
3. **Score delta emission**: emit a `RiskScore` update (and trigger webhook alerts) only when the score changes by ≥5 points from the last emitted score, to avoid alert storms on minor fluctuations.

The rolling window state must survive process restarts: serialize window state to SQLite so the streamer can resume without losing feature history on crash/restart.

Benford features computed across windows of N<30 trades fall back to Monte Carlo bootstrapped p-values (see ISSUE-073 for bootstrap implementation; for this issue, use the existing asymptotic p-values as a placeholder).

## Objectives

- [ ] Implement `RollingWindowState` class in `detection/rolling_window.py` managing per-wallet trade deques for windows `[1h, 4h, 24h]` with automatic eviction of expired trades.
- [ ] Implement `RollingWindowState.add_trade(wallet, trade)` that appends the trade and evicts trades older than the maximum window (24h).
- [ ] Implement `RollingWindowState.get_window(wallet, hours) -> list[Trade]` returning trades within the specified window.
- [ ] Implement `RollingWindowStore` for SQLite persistence: `save_state(wallet, state)` and `load_state(wallet) -> RollingWindowState`.
- [ ] Implement `IncrementalScorer` in `detection/model_inference.py` wrapping `RollingWindowState`, `FeatureEngineering`, and `ModelInference` with method `score_on_trade(trade) -> Optional[RiskScore]`.
- [ ] `score_on_trade` returns a `RiskScore` only when the new score differs from the last emitted score by ≥ `SCORE_DELTA_THRESHOLD` (default: 5).
- [ ] Extend `cli.py stream` to instantiate `IncrementalScorer` and pass each incoming trade to `score_on_trade`; persist any emitted `RiskScore` and trigger webhook dispatch.
- [ ] Persist rolling window state to SQLite on every N trades (configurable: `STREAM_CHECKPOINT_INTERVAL`, default: 100) to limit data loss on crash.
- [ ] Implement graceful shutdown: on SIGTERM/SIGINT, checkpoint all in-memory window states before exiting.
- [ ] Expose `GET /stream/status` endpoint showing: trades/second (rolling 60s average), active wallet windows count, last trade timestamp.
- [ ] All new code covered by tests; ≥90% branch coverage.

## Technical Requirements

### `RollingWindowState` (`detection/rolling_window.py`)

```python
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, List

WINDOW_HOURS = [1, 4, 24]

class WalletWindow:
    def __init__(self):
        # One deque per window; trades are added to all windows; eviction is window-specific
        self._trades: Deque[Trade] = deque()    # All trades for this wallet, up to 24h
        self._last_score: Optional[float] = None
        self._last_scored_at: Optional[datetime] = None

    def add(self, trade: Trade) -> None:
        self._trades.append(trade)
        self._evict(hours=24)

    def get(self, hours: int) -> List[Trade]:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        return [t for t in self._trades if t.timestamp >= cutoff]

    def _evict(self, hours: int) -> None:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        while self._trades and self._trades[0].timestamp < cutoff:
            self._trades.popleft()

class RollingWindowState:
    def __init__(self):
        self._wallets: Dict[str, WalletWindow] = {}

    def add_trade(self, wallet: str, trade: Trade) -> None:
        if wallet not in self._wallets:
            self._wallets[wallet] = WalletWindow()
        self._wallets[wallet].add(trade)

    def get_window(self, wallet: str, hours: int) -> List[Trade]:
        if wallet not in self._wallets:
            return []
        return self._wallets[wallet].get(hours)

    @property
    def active_wallets(self) -> int:
        return len(self._wallets)
```

### `IncrementalScorer` (`detection/model_inference.py`)

```python
class IncrementalScorer:
    def __init__(
        self,
        window_state: RollingWindowState,
        feature_engineering: FeatureEngineering,
        model_inference: ModelInference,
        score_delta_threshold: int = 5,
    ):
        self._window = window_state
        self._fe = feature_engineering
        self._infer = model_inference
        self._delta = score_delta_threshold
        self._last_scores: Dict[str, int] = {}   # wallet -> last emitted score

    def score_on_trade(self, trade: Trade) -> Optional[RiskScore]:
        """
        Update rolling window with trade; recompute features for trade.base_account;
        re-score; return RiskScore if |new_score - last_score| >= delta, else None.
        """
        wallet = trade.base_account
        self._window.add_trade(wallet, trade)
        
        # Build feature vector from rolling windows
        features = self._fe.compute_incremental(
            wallet=wallet,
            trades_1h=self._window.get_window(wallet, 1),
            trades_4h=self._window.get_window(wallet, 4),
            trades_24h=self._window.get_window(wallet, 24),
        )
        new_score = self._infer.score(wallet, trade.asset_pair, features)
        last_score = self._last_scores.get(wallet, -999)
        
        if abs(new_score.score - last_score) >= self._delta:
            self._last_scores[wallet] = new_score.score
            return new_score
        return None
```

### `FeatureEngineering.compute_incremental()` extension

```python
def compute_incremental(
    self,
    wallet: str,
    trades_1h: List[Trade],
    trades_4h: List[Trade],
    trades_24h: List[Trade],
) -> Dict[str, float]:
    """
    Compute all 35 features from rolling window trades.
    Benford features computed over each window independently.
    Graph features use a local subgraph built from 24h trades only.
    Cross-pair features use 24h trades.
    """
    ...
```

### SQLite persistence schema

```sql
CREATE TABLE IF NOT EXISTS rolling_window_checkpoints (
    wallet      TEXT NOT NULL,
    trades_json TEXT NOT NULL,      -- JSON-serialised list of Trade dicts (24h window)
    last_score  INTEGER,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wallet)
);
```

### `cli.py stream` extension

```python
@app.command("stream")
def stream(
    checkpoint_interval: int = typer.Option(100, envvar="STREAM_CHECKPOINT_INTERVAL"),
    score_delta: int = typer.Option(5, envvar="STREAM_SCORE_DELTA_THRESHOLD"),
):
    """Stream trades from Horizon SSE and score incrementally."""
    scorer = IncrementalScorer(...)
    trades_since_checkpoint = 0
    
    for trade in horizon_streamer.stream():
        result = scorer.score_on_trade(trade)
        if result:
            store.save(result)
            webhook_worker.enqueue_alert(result)
        trades_since_checkpoint += 1
        if trades_since_checkpoint >= checkpoint_interval:
            checkpoint_store.save_all(scorer.window_state)
            trades_since_checkpoint = 0
```

### Configuration

```
STREAM_CHECKPOINT_INTERVAL=100
STREAM_SCORE_DELTA_THRESHOLD=5
STREAM_WINDOW_HOURS=1,4,24
```

## Security Considerations

- **Checkpoint data contains trade history**: the `rolling_window_checkpoints` table stores serialised trades (amounts, counterparties, timestamps). This is moderately sensitive data — gate checkpoint access to the local process; do not expose checkpoint contents via the API.
- **Memory bounds**: a wallet processing very high trade volume over 24h could accumulate thousands of trades in `WalletWindow`. Implement a hard cap: `MAX_TRADES_PER_WALLET_WINDOW=10000`; evict oldest trades when exceeded. Log a WARNING if the cap is reached (it may indicate a high-volume legitimate market-maker or a synthetic stress test).
- **Serialisation safety**: checkpoint JSON serialisation uses `trade.dict()` (Pydantic serialiser), not `pickle`, to prevent code execution on checkpoint load.
- **SIGTERM handler**: the graceful shutdown handler must complete checkpointing before exiting. Use `signal.signal(signal.SIGTERM, handler)` and a threading.Event for clean shutdown.

## Testing Requirements

- **Unit — `WalletWindow.add` eviction**: insert trades at t=0h, 1h, 2h; after 24h passes (mock clock), verify only trades within 24h remain.
- **Unit — `get_window` scoping**: insert 5 trades within 1h and 5 older than 1h; `get_window(1)` returns exactly 5.
- **Unit — `IncrementalScorer` delta suppression**: two consecutive scores of 82 and 83 (delta=1 < threshold=5) → `score_on_trade` returns None on second call.
- **Unit — `IncrementalScorer` delta trigger**: scores 70 then 76 (delta=6 >= 5) → second call returns `RiskScore`.
- **Unit — first trade always emits**: no prior score (`last_score=-999`) → any score triggers emission.
- **Unit — checkpoint save/load**: save state for 3 wallets; reload; assert wallet window contents match.
- **Unit — memory cap**: insert 10,001 trades for one wallet; assert window contains exactly 10,000.
- **Integration — stream loop**: mock 50 SSE trades for 5 wallets; assert `store.save` called only when delta threshold met.
- **Integration — graceful shutdown**: send SIGINT; assert checkpoint is written before process exits.
- **Integration — `GET /stream/status`**: assert response contains `active_wallets`, `trades_per_second`, `last_trade_at`.

## Documentation Requirements

- Docstrings on `RollingWindowState`, `WalletWindow`, and `IncrementalScorer`.
- Update `README.md` CLI Reference for `stream` command with new options.
- New file `docs/streaming_scorer.md` covering: architecture, window management, delta threshold rationale, checkpoint strategy, and graceful shutdown.
- Document `STREAM_*` configuration variables in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `RollingWindowState` and `WalletWindow` implemented in `detection/rolling_window.py`.
- [ ] `IncrementalScorer.score_on_trade()` implemented with delta threshold logic.
- [ ] `FeatureEngineering.compute_incremental()` implemented for all 35 features.
- [ ] `cli.py stream` drives `IncrementalScorer` with periodic checkpointing.
- [ ] Graceful SIGTERM/SIGINT shutdown with final checkpoint.
- [ ] `GET /stream/status` endpoint operational.
- [ ] Memory cap on per-wallet trade accumulation.
- [ ] All unit and integration tests pass; ≥90% branch coverage.
- [ ] `docs/streaming_scorer.md` written.
- [ ] `.env.example` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience building stateful streaming data pipelines — ideally in fraud detection, anomaly detection, or time-series monitoring contexts. You understand rolling window data structures (deque-based eviction), incremental feature computation, and the tradeoffs between accuracy and latency in streaming ML. Familiarity with the Horizon SSE API and LedgerLens's feature engineering schema (35-feature vector) will accelerate implementation significantly.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., streaming systems, real-time ML, Python backend, event-driven architectures).
2. **Relevant experience**: stateful streaming pipelines, real-time scoring systems, or Horizon SSE integrations you have shipped.
3. **Approach / thoughts**: how would you handle the cold-start problem — a wallet that appears in the stream with no prior history, where 1h/4h/24h windows are all empty? Would you defer scoring until a minimum number of trades accumulates?
4. **Estimated time**: realistic estimate to complete to the Definition of Done standard.
