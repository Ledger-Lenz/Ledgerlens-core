# Detection Benchmark

A versioned, labeled wash-trading dataset used to measure detection
precision/recall across releases. CI (`detection-benchmark` job in
`.github/workflows/ci.yml`) runs the detection pipeline against it on every
change, writes a baseline-vs-current delta table to the job summary, and fails
if any metric drops more than `--tolerance` (default 0.02).

```
benchmarks/detection/
  dataset.py            # deterministic pattern generators (DATASET_VERSION)
  run.py                # detection run + delta report
  datasets/<version>/
    trades.csv          # trades with a `pattern` column
    labels.csv          # account, label (1 = wash, 0 = clean), pattern
    baseline.json       # expected precision/recall for this version
```

Covered patterns: `circular`, `self_matching`, `layering`, plus `clean` traffic.

## Running locally

```bash
python -m benchmarks.detection.run                    # compare to baseline
python -m benchmarks.detection.run --update-baseline  # accept new numbers
```

Update the baseline only when a detection change is an intended improvement
(or an accepted trade-off), and say so in the PR.

## Adding a new pattern type

1. Add a generator `def my_pattern(rng) -> (rows, accounts)` to `dataset.py`
   using `_row(...)` and a unique account prefix; register it in `PATTERNS`.
2. Bump `DATASET_VERSION` (e.g. `v1` -> `v2`) — never rewrite a published
   version's data.
3. `python -m benchmarks.detection.dataset` to write `datasets/<new version>/`.
4. `python -m benchmarks.detection.run --update-baseline` to record the baseline.
5. Commit the new `datasets/<version>/` directory; the per-pattern recall row appears automatically.
