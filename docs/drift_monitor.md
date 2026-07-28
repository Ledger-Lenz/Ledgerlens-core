# Drift Monitoring

LedgerLens runs **two independent, complementary** drift-detection
mechanisms. They differ in latency, scope, and what they're for — neither
replaces the other.

| | Streaming detectors (Issue-385) | PSI batch monitor (Issue-030 / 135) |
|---|---|---|
| Module | `detection/drift_detectors.py` | `detection/drift_monitor.py` |
| Trigger | Every real-time scoring call | Manual / `cli.py retrain-check` (cron) |
| Latency | ~100-200 observations | Bounded only by operator/cron cadence — hours to weeks |
| Algorithm | ADWIN + Page-Hinkley (per-feature, streaming) | Population Stability Index (per-feature, batch histogram) |
| Memory | Bounded, O(log W) per feature | Full snapshot history in SQLite |
| Response | Gates conformal recalibration (`ConformalCalibrator.adapt_online`) | Webhook alert + retrain recommendation |
| Endpoint | `GET /health/drift` | `GET /admin/drift-reports` |

Use the streaming detectors to answer "is something wrong *right now*?" and
PSI to answer "how has our feature distribution characteristically shifted
over the last N days, and should we retrain?"

## Streaming detectors

### ADWIN (Bifet & Gavaldà, 2007)

Maintains a variable-length window per feature. On every observation, the
window is re-examined for a statistically significant split point (via a
variance-aware Hoeffding-style bound); if one is found, the older half is
dropped and a change is signalled. Implemented in
`detection.drift_detectors.ADWINDriftDetector`.

Memory is bounded via an exponential histogram of buckets (the standard
MOA/river engineering approximation): buckets are merged once a "row" holds
more than `max_buckets_per_row` (default 5) entries, giving O(M log(W/M))
memory per feature regardless of how long the stream has run. Cut-points are
checked at bucket boundaries rather than every single sample, trading a
small amount of split precision for O(log W) amortised cost per update.

### Page-Hinkley (Page, 1954)

Tracks a cumulative sum of deviations from the running mean (offset by a
noise-tolerance `delta`) and its running minimum; fires when the gap between
the two exceeds `threshold`. Implemented in
`detection.drift_detectors.PageHinkleyDetector`. Resets its cumulative
statistics on firing so it keeps detecting subsequent shifts.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ADWIN_DELTA` | `0.002` | ADWIN confidence parameter; lower = fewer false positives, slower to react |
| `PAGE_HINKLEY_THRESHOLD` | `50.0` | Page-Hinkley firing threshold; higher = fewer false alarms, slower detection |
| `PAGE_HINKLEY_DELTA` | `0.005` | Page-Hinkley noise tolerance |
| `DRIFT_ACTIVE_COOLDOWN_OBSERVATIONS` | `200` | How many subsequent observations a firing stays "active" for, gating conformal adaptation |

### Where they run

One `ADWINDriftDetector` + one `PageHinkleyDetector` pair is created per
scored feature (`detection.feature_engineering.FEATURE_NAMES`, currently 122
features) via the process-wide `detection.drift_detectors.get_drift_registry()`
singleton. `detection.model_inference.score_with_uncertainty` feeds every
scored feature vector to the registry as a side effect of scoring — this
runs on the real-time scoring hot path, so it is a cheap, O(log W)-per-feature
update, not the O(W) batch scan PSI does.

When any detector fires, a best-effort `drift.detected` webhook event is
enqueued to active subscribers (reusing the existing
`detection.webhook_queue` / `detection.webhook_registry` infrastructure), and
the registry's `is_active()` flips true for the next
`DRIFT_ACTIVE_COOLDOWN_OBSERVATIONS` observations.

### Observability: `GET /health/drift`

Returns current state for every tracked feature:

```json
{
  "drift_active": false,
  "last_drifted_features": [],
  "last_event_at": null,
  "config": {
    "adwin_delta": 0.002,
    "page_hinkley_threshold": 50.0,
    "page_hinkley_delta": 0.005,
    "cooldown_observations": 200
  },
  "features": {
    "benford_first_digit_ks_stat": {
      "adwin": {"width": 4213, "estimation": 0.031, "n_detections": 0, "last_detection_at_width": null},
      "page_hinkley": {"statistic": 2.1, "n_observations": 4213, "n_detections": 0, "last_detection_at_n": null}
    },
    "...": "..."
  }
}
```

`drift_active` is the same signal that gates conformal recalibration — see
[`docs/uncertainty_quantification.md`](uncertainty_quantification.md) for
how detected drift is coupled to the coverage guarantee.

## PSI batch monitor

Unchanged by this work — see the module docstring in
`detection/drift_monitor.py` and `GET /admin/drift-reports` for the existing
per-feature PSI tracking, three-tier escalation, and webhook alerting. It
remains the tool for thorough, retrospective characterisation of gradual
distribution drift and for deciding when a full model retrain
(`cli.py retrain-check`) is warranted.
