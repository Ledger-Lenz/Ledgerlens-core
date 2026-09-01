# Benchmarks

This directory holds three standalone performance scripts. They are **not**
correctness tests — they measure latency / throughput and, in two of the three
cases, exit non-zero when a hard threshold is breached.

| Script | What it measures | Run in CI? |
|--------|------------------|------------|
| `benchmark_scoring.py` | p50/p95/p99 latency of the scoring pipeline, with regression detection against a committed baseline | Helper functions only (see below) |
| `benchmark_feature_engineering.py` | Numba JIT vs pure-Python speed of the `feature_engineering.py` hot loops | No — local only |
| `horizon_checkpoint.py` | Cursor-checkpoint flush latency under a 10 000-event replay | No — local only |

All three assume the project is installed (`pip install -e .` plus the test
requirements) and are run from the repository root.

---

## `benchmark_scoring.py`

### Purpose

Measures latency percentiles for three scoring scenarios and compares them to a
committed baseline so a regression shows up as a CI-style failure:

- **`single_wallet_score`** — score one pre-computed feature vector. Target: p99 < 50 ms.
- **`feature_extraction_1000_trades`** — build one wallet's feature vector from a
  1 000-trade batch. Target: p99 < 200 ms.
- **`batch_scoring_100_wallets`** — score 100 feature vectors in one vectorized
  call. Target: per-wallet cost stays roughly flat versus the single-wallet
  baseline (i.e. batch scoring scales linearly, not worse).

Trade data comes from `tests.factories.TradeFactory` with a pinned seed, and the
models are small in-process `RandomForestClassifier`s (mirroring
`tests/test_model_inference.py`), so the run is reproducible without pre-trained
artifacts or xgboost/lightgbm installed.

### How to run

```bash
# Measure and compare against benchmarks/baseline.json
python3 benchmarks/benchmark_scoring.py

# Measure and (re)write benchmarks/baseline.json
python3 benchmarks/benchmark_scoring.py --update-baseline
```

> Note: the module docstring also mentions `make benchmark`. That Make target is
> not currently defined (`make benchmark-check` runs a different thing — see
> below). Use `--update-baseline` directly. — TBD: reconcile the docstring and
> the Makefile.

### Output

Prints a JSON object to stdout:

```json
{
  "seed": 42,
  "hardware": { "cpu": "...", "ram_gb": 16.0, "platform": "...", "python_version": "3.12.x" },
  "scenarios": {
    "single_wallet_score":            { "p50_ms": ..., "p95_ms": ..., "p99_ms": ..., "mean_ms": ..., "samples": 200 },
    "feature_extraction_1000_trades": { "p50_ms": ..., "p95_ms": ..., "p99_ms": ..., "mean_ms": ..., "samples": 20 },
    "batch_scoring_100_wallets":      { "p50_ms": ..., ..., "n_wallets": 100, "per_wallet_p50_ms": ... }
  }
}
```

When run without `--update-baseline` it then prints either
`No regressions detected.` (exit 0) or `REGRESSIONS DETECTED:` followed by a list
(exit 1). If `benchmarks/baseline.json` does not exist, the regression check is
skipped and the script still exits 0.

### What a "good" result looks like

- Each scenario's `p99_ms` is at or below its target above.
- Compared to the baseline, no scenario's `p99_ms` is **more than 20 % higher**
  (`REGRESSION_THRESHOLD = 0.20` in the script) — that is the line between noise
  and "a regression worth investigating".
- `batch_scoring_100_wallets.per_wallet_p50_ms` is not more than **50 % above**
  `single_wallet_score.p50_ms` (`check_linear_scaling`, `tolerance = 0.5`).

Absolute numbers are hardware-dependent (hence the `hardware` fingerprint in the
output); the baseline should be regenerated on the reference machine, not copied
between machines. There is no `baseline.json` committed today — the first
`--update-baseline` on a reference machine establishes one.

### CI

The `benchmark` job in `.github/workflows/ci.yml` runs `make benchmark-check`,
which is `pytest -m benchmark`. Because `pytest` only collects from `tests/`,
that command runs the `@pytest.mark.benchmark` tests under `tests/` (e.g.
`tests/test_throughput.py`) and does **not** execute `benchmark_scoring.py`
itself. The script's pure-stdlib helpers (`check_regressions`,
`check_linear_scaling`, `_percentiles`) are unit-tested in the normal CI run by
`tests/test_benchmark_regression_check.py`. Running the actual latency benchmark
and refreshing the baseline is a manual, local step.

---

## `benchmark_feature_engineering.py`

### Purpose

Compares the Numba-JIT and pure-Python code paths for two
`detection/feature_engineering.py` hot loops — `round_trip_trade_frequency` and
`cross_pair_features` — at 1 000 / 10 000 / 50 000 synthetic trades. It exists to
justify keeping the JIT path (`settings.feature_engine_jit_enabled`).

### How to run

```bash
python benchmarks/benchmark_feature_engineering.py
```

Requires `numba` to be installed for the JIT numbers to be meaningful. The script
toggles `settings.feature_engine_jit_enabled` in-process and does one warm-up
call per configuration so JIT compilation is excluded from the timing.

### Output

Plain text, grouped by trade count:

```
=== n=10000 trades ===
  [JIT] round_trip_trade_frequency: 1.23ms
  [JIT] cross_pair_features:        4.56ms
  [Python] round_trip_trade_frequency: 12.34ms
  [Python] cross_pair_features:        45.67ms
```

### What a "good" result looks like

- No hard pass/fail — the script always exits 0 and just prints.
- Expectation: at `n=10000` and `n=50000` the `[JIT]` timings should be
  meaningfully lower than the `[Python]` timings for the same function. At
  `n=1000` the difference may be small or reversed (JIT overhead dominates).
- Compare runs by holding trade count and function fixed and looking at the
  JIT-vs-Python ratio; a run where JIT is *slower* than Python at 50 000 trades
  is a regression worth investigating.
- Concrete target ratios: **TBD — needs investigation** (no thresholds are
  encoded in the script).

Correctness of the two code paths (that JIT and Python produce the same numbers)
is covered separately by `tests/test_feature_engineering_jit.py`.

### CI

Local-only. Not referenced by any workflow in `.github/workflows/` or by the
`Makefile`.

---

## `horizon_checkpoint.py`

### Purpose

A standalone latency check for the ingestion cursor checkpoint
(`ingestion/checkpoint.py`). It replays 10 000 synthetic events through the
default `FlushPolicy` (`max_events=100`, `max_seconds=10.0`), writing the cursor
file each time the policy says to flush, and times each write.

### How to run

```bash
python benchmarks/horizon_checkpoint.py
```

No arguments. The checkpoint file is written under a `TemporaryDirectory`, so the
run leaves nothing behind.

### Output

A single line:

```
10,000 events: 0.123s; checkpoint write p99: 0.456ms
```

### What a "good" result looks like

The script enforces two hard thresholds and exits non-zero if either is
exceeded:

- Total wall-clock for 10 000 events **< 2.0 s**
- Checkpoint-write **p99 < 5.0 ms**

A pass is exit 0 with both numbers comfortably under those limits; a regression
worth investigating is either an exit 1 or numbers creeping toward the limits
across runs. As with the other scripts, absolute numbers depend on disk / host,
so compare like-for-like.

### CI

Local-only. Not referenced by any workflow in `.github/workflows/` or by the
`Makefile`.
