#!/usr/bin/env python3
"""verify_experiment.py

Refactored production-ready validation script for Chaos Mesh experiments.
Verifies API health status, /v1/scores retrieval, and Prometheus SLA metrics.
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
        "--api-key",
        default="",
        help="Optional API key for authenticated requests",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="Total time in seconds to wait for system recovery",
    )
    return parser.parse_args()


def check_api_health(url: str) -> bool:
    """Check API health and database/models readiness."""
    try:
        resp = requests.get(f"{url}/health", timeout=3)
        if resp.status_code != 200:
            print(f"⚠️ Health endpoint returned status {resp.status_code}")
            return False
        
        data = resp.json()
        status = data.get("status")
        db = data.get("db")
        models = data.get("models")
        circuits = data.get("circuits", {})

        print(f"ℹ️ Health details: status={status}, db={db}, models={models}, circuits={circuits}")

        if status not in ("ok", "degraded"):
            return False
        if db != "ok":
            return False
        if models != "ok":
            return False
            
        # Ensure circuits are returning to closed state after recovery
        if circuits:
            for circuit_name, circuit_state in circuits.items():
                if circuit_state != "closed":
                    print(f"⚠️ Circuit '{circuit_name}' is in '{circuit_state}' state.")
                    return False
        return True
    except Exception as e:
        print(f"⚠️ Failed to connect to health endpoint: {e}")
        return False


def check_scores_endpoint(url: str, api_key: str) -> bool:
    """Call /v1/scores to verify end-to-end database read and route availability."""
    headers = {}
    if api_key:
        headers["X-LedgerLens-Api-Key"] = api_key
    try:
        resp = requests.get(f"{url}/v1/scores", headers=headers, timeout=5)
        if resp.status_code != 200:
            print(f"⚠️ /v1/scores returned status {resp.status_code}: {resp.text}")
            return False
        
        scores = resp.json()
        print(f"✅ /v1/scores returned {len(scores)} scores successfully.")
        return True
    except Exception as e:
        print(f"⚠️ Failed to call /v1/scores: {e}")
        return False


def check_prometheus_metrics(metrics_url: str) -> bool:
    """Parse Prometheus metrics to ensure failure/error rates and circuit states are healthy."""
    try:
        resp = requests.get(metrics_url, timeout=3)
        if resp.status_code != 200:
            print(f"⚠️ Metrics endpoint returned status {resp.status_code}")
            return False
        
        lines = resp.text.split("\n")
        circuit_breakers_ok = True
        redis_errors_ok = True

        for line in lines:
            if line.startswith("#") or not line.strip():
                continue
            
            # Check circuit breaker status (0 is typically closed/healthy)
            if "circuit_breaker_state" in line:
                parts = line.split()
                if len(parts) >= 2:
                    val = float(parts[1])
                    if val != 0:
                        print(f"⚠️ Prometheus metric shows open circuit breaker: {line}")
                        circuit_breakers_ok = False
            
            # Check Redis connection errors
            if "redis_connection_errors_total" in line:
                parts = line.split()
                if len(parts) >= 2:
                    val = float(parts[1])
                    if val > 10.0:
                        print(f"⚠️ High count of Redis connection errors observed: {val}")
                        redis_errors_ok = False

        return circuit_breakers_ok and redis_errors_ok
    except Exception as e:
        print(f"⚠️ Failed to parse metrics endpoint: {e}")
        return False


def main() -> int:
    args = parse_args()
    print(f"🚀 Starting verification: API={args.url}, Metrics={args.metrics_url}, Timeout={args.timeout}s")
    
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        print("\nChecking system state...")
        health_ok = check_api_health(args.url)
        scores_ok = check_scores_endpoint(args.url, args.api_key) if health_ok else False
        metrics_ok = check_prometheus_metrics(args.metrics_url) if health_ok else False

        if health_ok and scores_ok and metrics_ok:
            print("\n✅ Verification SUCCESS! System fully recovered.")
            return 0
        
        time.sleep(5)
        
    print(f"\n❌ Verification FAILED: Timeout reached after {args.timeout}s.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
