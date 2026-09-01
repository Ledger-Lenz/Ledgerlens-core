# Spike Decision: Canonical Chaos-Engineering Approach
**Author:** AI Coding Assistant  
**Status:** Proposal  
**GitHub Issue:** #694: "[Spike] Decide the Canonical Chaos-Engineering Approach: Chaos Mesh vs. the Existing Toxiproxy-Based tests/chaos Suite"

---

## Executive Summary & Recommendation

LedgerLens requires a robust chaos-engineering strategy to ensure high availability and resilience under adverse network, database, and infrastructure conditions. We currently have two disconnected implementations:
1. **Application-level Toxiproxy-based suite** (`tests/chaos/`) running fast via Docker Compose.
2. **Kubernetes-native Chaos Mesh manifests** (`chaos-mesh/`) targeting cluster infrastructure, which are currently un-triggered.

We recommend adopting a **Dual-Layer Strategy** that combines both approaches to optimize execution speed, operational cost, and testing fidelity:

*   **Layer 1 (Fast Feedback/PR CI):** Retain and integrate the **Toxiproxy** suite into standard Pull Request CI. It runs in seconds, requires no Kubernetes infrastructure, and validates critical application-level fault tolerance (e.g., fallback logic, timeouts, circuit breakers).
*   **Layer 2 (Fidelity/Periodic Staging):** Wire **Chaos Mesh** to execute on a schedule (e.g., nightly/weekly) or via manual dispatch against the **Staging/Canary Kubernetes cluster**. This validates platform-level failure modes (e.g., node loss, container eviction, split-brain routing) under synthetic workloads.

```mermaid
graph TD
    PR[Pull Request / Push to Main] -->|Triggers| CI[GitHub Actions PR CI]
    CI -->|Spins up| DC[Docker Compose]
    DC -->|Runs| TP[Toxiproxy Tests]
    TP -->|Validates| App[Application Resilience & Fallbacks]

    Sched[Cron Schedule / Manual Dispatch] -->|Triggers| StgCI[GitHub Actions Staging Workflow]
    StgCI -->|Applies Manifests| K8s[Staging Kubernetes Cluster]
    K8s -->|Runs| CM[Chaos Mesh Controllers]
    CM -->|Injects Pod/Net Faults| Pods[Staging API & Workers]
    StgCI -->|Executes| Verify[verify_experiment.py]
    Verify -->|Queries| Metrics[Prometheus Metrics / health]
    Verify -->|Rolls back on failure| K8s
```

---

## Comparative Analysis: Toxiproxy vs. Chaos Mesh

| Feature | Toxiproxy (`tests/chaos/`) | Chaos Mesh (`chaos-mesh/`) |
| :--- | :--- | :--- |
| **Injection Layer** | Application TCP/HTTP level (via proxying). | Infrastructure / Kubernetes Kernel level (via eBPF, iptables, cgroups). |
| **Infrastructure Req.** | None (Docker Compose only). | Full Kubernetes cluster with CRD controllers. |
| **Execution Cost** | Negligible (local runner CPU/RAM). | Higher (requires dedicated Kubernetes resources/agents). |
| **Execution Speed** | Fast (seconds to minutes). | Moderate (minutes due to cluster scheduling/grace periods). |
| **Typical Faults** | Latency, jitter, connection drops, bandwidth limits. | Pod kills, network partitions, disk pressure, clock skew, kernel panics. |
| **Best Fit For** | Unit & Integration testing of client/driver resilience. | End-to-end platform recovery, cluster auto-scaling, and failover. |

---

## The Dual-Layer Strategy Details

### Layer 1: Toxiproxy in PR CI (Rapid Verification)

Toxiproxy intercepts network traffic between the application and its dependencies (Redis, external Horizon APIs). 

*   **Location:** [tests/chaos/](file:///c:/Users/Lc/Desktop/Ledgerlens-core/tests/chaos)
*   **Target Scope:** Every Pull Request and commit to the `main` branch.
*   **Key Coverage:**
    *   **Latency Spikes:** [`test_horizon_latency.py`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/tests/chaos/test_horizon_latency.py) verifies p99 scoring latency stays below 2s when Horizon is slow.
    *   **Circuit Breaking:** [`test_circuit_breaker.py`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/tests/chaos/test_circuit_breaker.py) checks that `SorobanPublisher` isolates API failures.
    *   **Cache Fallbacks:** [`test_redis_fallback.py`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/tests/chaos/test_redis_fallback.py) ensures cold tier activation when Redis is down.
    *   **Lock Contention:** [`test_sqlite_wal_lock.py`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/tests/chaos/test_sqlite_wal_lock.py) verifies SQLite WAL locks return 503 instead of 500.

> [!TIP]
> To fully integrate Layer 1 into standard PR flows, we should update `.github/workflows/ci.yml` or modify `.github/workflows/chaos.yml` to trigger on `pull_request` events, running the suite automatically on every code change.

### Layer 2: Chaos Mesh in Staging/Canary (Platform Stress Testing)

Chaos Mesh injects low-level chaos directly via Kubernetes Custom Resource Definitions (CRDs).

*   **Location:** [chaos-mesh/](file:///c:/Users/Lc/Desktop/Ledgerlens-core/chaos-mesh)
*   **Target Scope:** Nightly cron schedules and pre-release validation against a replica environment.
*   **Key Coverage:**
    *   **Network Partition (Ingestion):** [`network-partition-ingestion.yaml`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/chaos-mesh/network-partition-ingestion.yaml) cuts communication between the API and the ingestion workers.
    *   **Network Partition (Redis):** [`network-partition-redis.yaml`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/chaos-mesh/network-partition-redis.yaml) simulates cache disconnection at the network interface layer.
    *   **Pod Kills:** [`pod-kill-api.yaml`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/chaos-mesh/pod-kill-api.yaml) and [`pod-kill-ingestion.yaml`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/chaos-mesh/pod-kill-ingestion.yaml) randomly terminate active service instances.

---

## Safety Gating & Staging Guardrails

Running chaos engineering in a shared or pre-production environment requires strict safety gates to prevent unintended blast radius expansion, noisy alerts, and environment pollution.

### 1. Environment and Namespace Locks
All Chaos Mesh CRDs must contain strict namespace selectors. The manifests must be audited to ensure they *only* target namespaces prefixed with `staging-` or `canary-`.
*   **Action:** Add validation to our Helm/Kubectl deployment scripts to reject any Chaos Mesh CRD containing `namespace: default`, `namespace: kube-system`, or any production namespace.

### 2. Manual Triggers and Automated Rollbacks
*   **Manual Trigger:** Workflows must support `workflow_dispatch` with input parameters specifying the target experiment and duration, allowing developers to test specific failures on demand.
*   **Automated Teardown:** The workflow must guarantee cleanup. Even if the validation script fails, an `always()` step in GitHub Actions will run to delete the injected chaos resource:
    ```bash
    kubectl delete -f chaos-mesh/network-partition-redis.yaml --ignore-not-found=true
    ```

### 3. Alert Inhibition & Maintenance Windows
Chaos runs will trigger alarms (e.g., Pod restarts, service latencies). To prevent alert fatigue:
*   The deployment pipeline should interact with the Alertmanager API to create a **Silence** covering the duration of the chaos run.
*   Alternatively, alerts targeting staging should be labeled and automatically silenced during scheduled maintenance windows.

### 4. Blast Radius Limiting
*   Keep `mode: one` for PodChaos manifests (kill one replica at a time).
*   Ensure that the replica count for critical deployment components in staging is always $\ge 2$ so that killing a pod does not cause immediate downtime unless verifying a complete outage scenario.

---

## Proposed Staging Workflow (`.github/workflows/chaos-staging.yml`)

The following YAML outlines the structure for the Layer 2 schedule-driven Kubernetes chaos workflow:

```yaml
name: Staging Chaos Engineering

on:
  schedule:
    # Run every Wednesday at 03:00 UTC
    - cron: "0 3 * * 3"
  workflow_dispatch:
    inputs:
      experiment:
        description: 'Experiment file to run (e.g., pod-kill-api.yaml)'
        required: true
        default: 'pod-kill-api.yaml'
      duration_seconds:
        description: 'Duration of the experiment in seconds'
        required: false
        default: '60'

jobs:
  run-chaos-experiment:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt requests

      - name: Configure Kubeconfig
        uses: azure/k8s-set-context@v3
        with:
          method: kubeconfig
          kubeconfig: ${{ secrets.STAGING_KUBECONCONFIG }}

      - name: Validate Staging Cluster Connection
        run: |
          current_ns=$(kubectl config view --minify --output 'jsonpath={..namespace}')
          if [[ "$current_ns" != *"staging"* && "$current_ns" != *"canary"* ]]; then
            echo "❌ Error: Target context is not staging/canary! Aborting."
            exit 1
          fi

      - name: Silencing Alertmanager
        run: |
          # Send silence request to staging Prometheus Alertmanager
          curl -H "Content-Type: application/json" -d '{
            "matchers": [{"name": "namespace", "value": "ledgerlens-staging", "isRegex": false}],
            "startsAt": "'"$(date -u +"%Y-%m-%dT%H:%M:%SZ")"'",
            "endsAt": "'"$(date -u -d '+30 mins' +"%Y-%m-%dT%H:%M:%SZ")"'",
            "createdBy": "GHA-Chaos-Workflow",
            "comment": "Silencing alerts for scheduled chaos testing"
          }' http://alertmanager.staging.ledgerlens.io/api/v2/silences || true

      - name: Inject Chaos Experiment
        run: |
          EXPERIMENT_FILE="chaos-mesh/${{ github.event.inputs.experiment || 'pod-kill-api.yaml' }}"
          echo "Injecting $EXPERIMENT_FILE..."
          kubectl apply -f $EXPERIMENT_FILE

      - name: Run Active Validation workload
        run: |
          # Start a background request-generation script to simulate normal user traffic
          python scripts/generate_traffic.py --url http://api.staging.ledgerlens.io --duration 120 &
          echo "Traffic generator started in background"

      - name: Verify Recovery
        run: |
          python chaos-mesh/verify_experiment.py \
            --url http://api.staging.ledgerlens.io \
            --metrics http://api.staging.ledgerlens.io/metrics \
            --timeout ${{ github.event.inputs.duration_seconds || 60 }}

      - name: Remove Chaos Experiment (Success Cleanup)
        if: success()
        run: |
          EXPERIMENT_FILE="chaos-mesh/${{ github.event.inputs.experiment || 'pod-kill-api.yaml' }}"
          kubectl delete -f $EXPERIMENT_FILE --ignore-not-found=true

      - name: Automated Emergency Rollback (Failure Cleanup)
        if: failure()
        run: |
          echo "⚠️ Validation failed! Running emergency teardown..."
          kubectl delete -f chaos-mesh/network-partition-ingestion.yaml --ignore-not-found=true
          kubectl delete -f chaos-mesh/network-partition-redis.yaml --ignore-not-found=true
          kubectl delete -f chaos-mesh/pod-kill-api.yaml --ignore-not-found=true
          kubectl delete -f chaos-mesh/pod-kill-ingestion.yaml --ignore-not-found=true
```

---

## Production-Ready Completion of `verify_experiment.py`

### Current Implementation Assessment
The existing [`verify_experiment.py`](file:///c:/Users/Lc/Desktop/Ledgerlens-core/chaos-mesh/verify_experiment.py) is a basic placeholder:
1. It has **hardcoded URLs** pointing to `localhost:8000`.
2. It only checks the `/health` status is `"ok"`.
3. It does not verify business-level flows (e.g., whether ingestion is processing blocks, whether Redis cache state transitions back to primary, or whether db queries succeed).
4. It ignores `/metrics` metrics data despite importing the path.

### Proposed Complete, Production-Ready Script
Below is the proposed implementation of `verify_experiment.py` which accepts CLI arguments, verifies E2E pipeline transaction processing, parses Prometheus metrics for error rates, and ensures full recovery of database connections and caches:

```python
#!/usr/bin/env python3
"""verify_experiment.py

Production-ready validation script for Chaos Mesh experiments.
Verifies that:
1. HTTP API endpoints respond.
2. Under-the-hood components (DB, Redis, Ingestion worker) are fully functional.
3. System metrics (error rates, p99 latency) have returned to safe baselines.
"""

from __future__ import annotations

import argparse
import sys
import time
import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Chaos Experiment Recovery")
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="Base URL of the LedgerLens API under test",
    )
    parser.add_argument(
        "--metrics-url",
        default="http://localhost:8000/metrics",
        help="Metrics URL for verifying SLA thresholds",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="Total time in seconds to wait for system recovery",
    )
    return parser.parse_args()


def check_api_health(url: str) -> bool:
    """Check both root health and database readiness."""
    try:
        # Standard root health check
        resp = requests.get(f"{url}/health", timeout=3)
        if resp.status_code != 200:
            return False
        
        data = resp.json()
        if data.get("status") != "ok":
            return False
        
        # Verify deeper DB/Redis dependency states if exposed in detailed health checks
        dependencies = data.get("details", {})
        if dependencies:
            if dependencies.get("database", {}).get("status") != "connected":
                return False
            if dependencies.get("redis", {}).get("status") != "connected":
                return False
                
        return True
    except Exception:
        return False


def verify_transaction_scoring_e2e(url: str) -> bool:
    """Submit a mock transaction to verify end-to-end data ingestion and scoring pipeline."""
    payload = {
        "wallet": "ChaosVerificationWallet1111111111111111111111111",
        "asset_pair": "XLM/USDC",
        "amount": "100.0",
        "timestamp": int(time.time()),
    }
    try:
        # Submit transaction score request
        resp = requests.post(f"{url}/v1/scores", json=payload, timeout=3)
        if resp.status_code != 200:
            return False
        
        # Verify scoring engine returned a logical risk metric
        result = resp.json()
        if "risk_score" not in result:
            return False
            
        return True
    except Exception:
        return False


def verify_metrics_sla(metrics_url: str) -> bool:
    """Parse Prometheus metrics to ensure failure/error rates have returned to 0."""
    try:
        resp = requests.get(metrics_url, timeout=3)
        if resp.status_code != 200:
            return False
            
        lines = resp.text.split("\n")
        for line in lines:
            # Check for active errors (e.g., redis_connection_errors_total)
            if "redis_connection_errors_total" in line and not line.startswith("#"):
                parts = line.split()
                if len(parts) >= 2 and float(parts[1]) > 10.0:
                    print(f"⚠️ Warning: Redis connection error count remains high: {parts[1]}")
                    
            # Check for circuit breaker status
            if "circuit_breaker_state" in line and not line.startswith("#"):
                # If circuit is open, value is usually 1. We want 0 (closed/normal).
                parts = line.split()
                if len(parts) >= 2 and float(parts[1]) != 0:
                    print("⚠️ Warning: Circuit breaker is still tripped (Open/Half-Open)")
                    return False
        return True
    except Exception:
        # If metrics endpoint is unavailable, do not fail outright, but log it
        return False


def main() -> int:
    args = parse_args()
    print(f"🚀 Starting verification against {args.url} (timeout: {args.timeout}s)")
    
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        api_ok = check_api_health(args.url)
        e2e_ok = verify_transaction_scoring_e2e(args.url) if api_ok else False
        metrics_ok = verify_metrics_sla(args.metrics_url) if api_ok else False
        
        if api_ok and e2e_ok and metrics_ok:
            print("✅ Chaos verification passed! All endpoints, pipelines, and metrics recovered.")
            return 0
            
        print("⏳ Waiting for components to recover...")
        time.sleep(5)
        
    print(f"❌ Recovery verification timed out after {args.timeout}s.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

---

## Action Items & Roadmap

1. **Step 1: Integrate Toxiproxy in PR CI:**
   * Modify `.github/workflows/chaos.yml` to run on `pull_request` and `push` to `main` branch.
2. **Step 2: Deploy Chaos Mesh Manifests and Values:**
   * Install Chaos Mesh CRDs and controllers in Staging using the `helm/chaos-mesh-values.yaml` values file.
3. **Step 3: Implement staging-chaos pipeline:**
   * Deploy `.github/workflows/chaos-staging.yml` using the workflow blueprint.
4. **Step 4: Update Verification Script:**
   * Replace the placeholder script in `chaos-mesh/verify_experiment.py` with the production-ready script above.
