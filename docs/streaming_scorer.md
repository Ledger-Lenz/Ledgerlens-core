# Streaming Scorer

Real-time wash-trading detection via stateful per-wallet rolling windows.

## Architecture

```
Horizon SSE stream
      │
      ▼
 stream_trades()          (ingestion/horizon_streamer.py)
      │
      ▼
 IncrementalScorer.score_on_trade(trade)
      │
      ├─ RollingWindowState.add_trade(wallet, trade)   evict > 24h
      │
      ├─ FeatureEngineering.compute_incremental(wallet, 1h/4h/24h trades)
      │
      ├─ ModelInference.score(wallet, asset_pair, features)
      │
      └─ |new_score - last_score| >= delta?
               yes → save + webhook alert
               no  → discard
```

## Window Management

`WalletWindow` holds a single `deque[Trade]` covering up to 24 hours per wallet.  
On each `add(trade)` call:

1. Trades older than 24 h are popped from the left (`_evict`).
2. The new trade is appended to the right.
3. If the deque length exceeds `MAX_TRADES_PER_WALLET_WINDOW` (10 000), the oldest entry is dropped and a `WARNING` is logged. This bounds memory for high-volume market-makers.

`get(hours)` does a linear scan and returns all trades whose `ledger_close_time` falls within the last *hours*.

## Delta Threshold Rationale

A raw ML score fluctuates by 1–3 points on consecutive trades due to minor feature changes (counterparty pool growing by one trade, Benford digit distribution shifting slightly). Without a minimum delta, every trade for a watched wallet would trigger an alert — creating alert storms and masking genuine risk escalation.

The default threshold of **5 points** was chosen to:
- Suppress minor statistical noise in the rolling feature computation.
- Trigger on meaningful escalations (e.g. round-trip rate climbing above 0.15).
- Stay below the "significant change" boundary (~10 points) so real threats are not delayed.

Configure via `STREAM_SCORE_DELTA_THRESHOLD` in `.env`.

## Checkpoint Strategy

Window state is serialised to `rolling_window_checkpoints` (SQLite) every `STREAM_CHECKPOINT_INTERVAL` trades (default: 100). Each wallet row stores:

| Column | Content |
|---|---|
| `wallet` | Stellar account ID |
| `trades_json` | JSON array of last 24-h trades via `trade.model_dump()` |
| `last_score` | Most recently emitted score integer |
| `updated_at` | UTC timestamp of last write |

Serialisation uses Pydantic's `model_dump` (not `pickle`) to prevent code execution on load.

On startup, `RollingWindowStore.load_all()` repopulates in-memory windows from all persisted rows, so the streamer resumes from where it stopped.

## Graceful Shutdown

The `stream` CLI command installs handlers for `SIGTERM` and `SIGINT`:

```python
signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)
```

The handler:
1. Logs the shutdown signal.
2. Calls `checkpoint_store.save_all(scorer.window_state)` — persists all in-memory windows.
3. Sets a `threading.Event` that causes the stream loop to exit after the current trade.

A final checkpoint is also written after the loop exits cleanly (e.g. the SSE stream closes).

## Configuration

| Variable | Default | Description |
|---|---|---|
| `STREAM_CHECKPOINT_INTERVAL` | `100` | Persist window state every N trades |
| `STREAM_SCORE_DELTA_THRESHOLD` | `5` | Minimum score change (0–100) to emit an alert |
| `STREAM_WINDOW_HOURS` | `1,4,24` | Rolling window sizes in hours |

## Stream Status API

`GET /stream/status` returns live streaming metrics:

```json
{
  "trades_per_second": 4.2,
  "active_wallets": 312,
  "last_trade_at": "2026-06-24T15:30:00Z"
}
```

- `trades_per_second` — rolling 60-second average; `0.0` when the stream is stopped.
- `active_wallets` — count of wallets with live windows; falls back to the checkpoint table when the stream is not running in the same process.
- `last_trade_at` — ISO 8601 UTC timestamp of the last processed trade, or `null`.

## Security Notes

- `rolling_window_checkpoints` stores trade counterparties and amounts. Do not expose checkpoint rows via the API.
- Checkpoint JSON uses Pydantic serialisation — `pickle` is never used.
- The `MAX_TRADES_PER_WALLET_WINDOW = 10 000` cap prevents a single high-volume account from consuming unbounded RAM.
