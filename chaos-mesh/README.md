# chaos-mesh

Chaos-engineering experiments for LedgerLens, run with
[Chaos Mesh](https://chaos-mesh.org/). Each YAML in this directory injects one
fault into the `ledgerlens` namespace; `verify_experiment.py` checks that the
system recovers afterwards.

## Experiment definitions

| File | Kind | What it simulates |
| --- | --- | --- |
| `pod-kill-api.yaml` | `PodChaos` (`pod-kill`, `mode: one`) | Kills one API pod every 10 minutes for 30s — validates that the API Deployment reschedules and traffic recovers. |
| `pod-kill-ingestion.yaml` | `PodChaos` (`pod-kill`, `mode: one`) | Kills one `ingestion-worker` pod every 10 minutes for 30s — validates that ingestion resumes after a worker is lost. |
| `network-partition-ingestion.yaml` | `NetworkChaos` (`partition`, `direction: to`) | Partitions the API pods from the `ingestion-worker` pods for 60s — validates graceful degradation when the API cannot reach ingestion. |
| `network-partition-redis.yaml` | `NetworkChaos` (`partition`, `direction: to`) | Partitions the API pods from Redis for 60s — validates the feature-store / cache fallback path when Redis is unreachable. |

All experiments are created in the `chaos-mesh` namespace and select workloads
in the `ledgerlens` namespace by `app.kubernetes.io/*` labels.

## Running an experiment end to end

1. **Deploy** — apply one experiment:

   ```bash
   kubectl apply -f chaos-mesh/pod-kill-api.yaml
   ```

2. **Observe** — while the fault is active, watch pods, dashboards and logs:

   ```bash
   kubectl get pods -n ledgerlens -w
   kubectl describe networkchaos,podchaos -n chaos-mesh
   ```

   The `pod-kill-*` experiments run on a cron (`@every 10m`); the
   `network-partition-*` experiments run once for their `duration` (60s).

3. **Verify** — once the experiment's `duration` has elapsed, confirm recovery:

   ```bash
   # Against a real target (Kubernetes-hosted staging, port-forward, etc.)
   python chaos-mesh/verify_experiment.py --health-url https://ledgerlens.staging.example/health

   # Local default: http://localhost:8000/health
   python chaos-mesh/verify_experiment.py
   ```

4. **Clean up** — delete the experiment:

   ```bash
   kubectl delete -f chaos-mesh/pod-kill-api.yaml
   ```

## `verify_experiment.py`

Polls `GET /health` every 2 seconds until it returns HTTP 200 with
`{"status": "ok"}`, or until the timeout elapses.

| Option | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `--health-url` | `HEALTH_URL` | `http://localhost:8000/health` | Health endpoint to poll. |
| `--timeout` | `HEALTH_TIMEOUT_S` | `60` | Seconds to keep polling before failing. |
| `--expect-degraded CIRCUIT` | — | off | While the fault is active, assert `/health` reports graceful degraded mode for `CIRCUIT` (see below), then assert it closes again on recovery. |
| `-v` / `--verbose` | — | off | Log every failed poll attempt at DEBUG. |

Exit code `0` means the endpoint recovered within the timeout; `1` means it did
not. Connection errors during polling are expected while a fault is active and
are logged at DEBUG rather than aborting the run.

## Feature-store degraded-mode contract

`network-partition-redis.yaml` asserts specific degraded behavior of
`detection/feature_store.py`, not just liveness. Run it with:

```bash
kubectl apply -f chaos-mesh/network-partition-redis.yaml
python chaos-mesh/verify_experiment.py --expect-degraded feature_store_redis \
  --health-url https://ledgerlens.staging.example/health
```

**During the partition** (checked by `assert_degraded`):

- `GET /health` keeps returning **HTTP 200** — a Redis partition must never
  escalate to a 503 hard failure. Any 503 fails the experiment immediately.
- `status` is `"degraded"` and `circuits.feature_store_redis` is `"open"` or
  `"half_open"` (the breaker trips after 3 consecutive Redis failures).
- Reads and writes go to the in-process fallback dict (LRU-bounded at
  `max_fallback_entries`). Feature state is still derived from live trades;
  Redis values are never read while the circuit is open.

**After the partition heals** (checked by `assert_recovery`):

- After the breaker's 30s recovery timeout, a successful Redis call closes the
  circuit: `status` returns to `"ok"` with `circuits.feature_store_redis ==
  "closed"`.
- Catch-up / cache warm: on the first read of a key, a fallback entry whose
  `last_updated` is newer than the Redis copy (or with no Redis copy) is
  written back to Redis and returned, so state accumulated during the outage
  is never shadowed by the stale pre-partition Redis value (no
  stale-as-fresh). Keys written back to Redis are dropped from the fallback
  dict.

## Chaos day and the resilience scorecard

`.github/workflows/chaos-day.yml` runs the full suite against staging every
Monday at 04:00 UTC (or on demand via *Run workflow*). It calls
`run_chaos_day.py`, which for each experiment applies it, runs the degraded
assertion (where one is defined) and the recovery check, then always deletes
the experiment.

Each run publishes a **resilience scorecard**:

- `scorecard.json` — pass/fail, time to enter degraded mode, and recovery
  time per experiment, plus the overall score and commit SHA. Uploaded as the
  `resilience-scorecard` artifact (kept 400 days), so runs are historically
  comparable.
- `scorecard.md` — the same data as a table, posted to the workflow's job
  summary.

The previous run's scorecard is downloaded and compared. A regression (an
experiment that passed before and now fails, or recovery time up by more than
50%) fails the run and is listed in the summary.

### Adding a new experiment

1. Add the Chaos Mesh YAML to this directory, targeting the
   `ledgerlens-staging` namespace, and add a row to the table above.
2. Register it in `EXPERIMENTS` in `run_chaos_day.py` with an `inject_wait_s`
   (how long to wait after `kubectl apply` before verifying), plus
   `expect_degraded` if it trips a `/health` circuit that should be asserted.
3. If it adds a new degraded mode, document the contract in this README.
4. Trigger `chaos-day.yml` manually once to confirm it passes and appears in
   the scorecard.

## Related

- `helm/chaos-mesh-values.yaml` — Helm values used to install the Chaos Mesh
  controller itself (service account `chaos-mesh`, UI disabled).
- `.github/ISSUES/ISSUE-117.md` — the broader "Chaos Engineering Test Suite"
  tracking issue.
- `ROADMAP.md` — chaos testing under the resilience workstream.
