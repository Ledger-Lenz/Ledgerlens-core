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
| `-v` / `--verbose` | — | off | Log every failed poll attempt at DEBUG. |

Exit code `0` means the endpoint recovered within the timeout; `1` means it did
not. Connection errors during polling are expected while a fault is active and
are logged at DEBUG rather than aborting the run.

## Related

- `helm/chaos-mesh-values.yaml` — Helm values used to install the Chaos Mesh
  controller itself (service account `chaos-mesh`, UI disabled).
- `.github/ISSUES/ISSUE-117.md` — the broader "Chaos Engineering Test Suite"
  tracking issue.
- `ROADMAP.md` — chaos testing under the resilience workstream.
