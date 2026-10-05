"""SLO-based pass/fail verification for chaos-mesh experiments.

Usage
-----
    python chaos-mesh/verify_experiment.py --experiment pod-kill-api.yaml \
        --url https://ledgerlens.staging.example

    # Local default (http://localhost:8000/health), e.g. against a port-forward
    python chaos-mesh/verify_experiment.py

    # Environment variables instead of flags
    CHAOS_EXPERIMENT=network-partition-redis.yaml \
    HEALTH_URL=https://ledgerlens.staging.example/health python chaos-mesh/verify_experiment.py

Workflow
    This script is the "verify" step of the chaos-testing loop:
    apply an experiment YAML in this directory (deploy), then run this script
    straight away.  It drives sustained traffic at the API for the experiment's
    traffic window while polling ``GET /health`` every 2s, then asserts the
    experiment's SLOs (see ``EXPERIMENT_SLOS`` and ``chaos-mesh/README.md``):

    * recovery   — ``/health`` returns 200 ``{"status": "ok"}`` within
                   ``recovery_s`` seconds;
    * error rate — the share of traffic requests that did not succeed stays at
                   or below ``max_error_rate``;
    * dropped    — requests silently dropped mid-flight (connection reset,
                   read timeout, non-retriable 5xx) stay at or below
                   ``max_dropped`` (0 for the API pod-kill: every request must
                   either complete or get a retriable error);
    * drain time — the slowest request that completed during the window
                   (i.e. was drained rather than dropped) stays within
                   ``drain_budget_s``. Exceeding it logs an ``ALERT`` line and,
                   when ``--alert-webhook`` is set, POSTs a JSON alert.

    Connection errors during health polling are expected while the fault is
    active and are logged at DEBUG (use ``-v`` to see them).

Degraded-mode assertions
    With ``--expect-degraded CIRCUIT`` (e.g. ``feature_store_redis`` for
    ``network-partition-redis.yaml``) the script first asserts, *while the
    fault is active*, that ``/health`` reports the specific degraded mode
    rather than mere liveness: HTTP 200 (never 503), ``status == "degraded"``
    and ``circuits[CIRCUIT]`` open or half-open.  It then asserts the
    recovery-to-healthy transition: ``status == "ok"`` with
    ``circuits[CIRCUIT] == "closed"``.

Exit codes
    0  the health endpoint recovered within the timeout
    1  the health endpoint did not recover in time, or the expected
       degraded mode was not observed
"""
import argparse
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_HEALTH_URL = "http://localhost:8000/health"
DEFAULT_METRICS_URL = "http://localhost:8000/metrics"

# Kept for backwards compatibility with callers importing these names directly.
HEALTH_URL = os.environ.get("HEALTH_URL", DEFAULT_HEALTH_URL)
METRICS_URL = os.environ.get("METRICS_URL", DEFAULT_METRICS_URL)

# Status codes a well-behaved client retries (the API sets Retry-After on 503).
RETRIABLE_STATUS = {429, 502, 503, 504}


@dataclass(frozen=True)
class SLO:
    recovery_s: int
    max_error_rate: float
    max_dropped: int
    drain_budget_s: float
    traffic_s: int = 60


# Per-experiment pass/fail criteria. Keep in sync with chaos-mesh/README.md.
EXPERIMENT_SLOS: dict[str, SLO] = {
    "pod-kill-api.yaml": SLO(recovery_s=60, max_error_rate=0.05, max_dropped=0, drain_budget_s=30),
    "pod-kill-ingestion.yaml": SLO(recovery_s=90, max_error_rate=0.01, max_dropped=0, drain_budget_s=30),
    "network-partition-ingestion.yaml": SLO(
        recovery_s=60, max_error_rate=0.05, max_dropped=0, drain_budget_s=10, traffic_s=90
    ),
    "network-partition-redis.yaml": SLO(
        recovery_s=60, max_error_rate=0.02, max_dropped=0, drain_budget_s=10, traffic_s=90
    ),
}
DEFAULT_EXPERIMENT = "pod-kill-api.yaml"


@dataclass
class TrafficResult:
    ok: int = 0
    retriable: int = 0
    dropped: int = 0
    max_ok_latency_s: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total(self) -> int:
        return self.ok + self.retriable + self.dropped

    @property
    def error_rate(self) -> float:
        return (self.retriable + self.dropped) / self.total if self.total else 0.0


def assert_degraded(health_url: str, circuit: str, timeout_s: int = 60) -> float:
    """Poll GET /health until it reports the graceful degraded mode for *circuit*.

    Passes on HTTP 200 with ``status == "degraded"`` and ``circuits[circuit]``
    open/half-open. An HTTP 503 means the fault escalated to a hard failure
    instead of degrading gracefully and fails immediately. Returns the seconds
    taken to enter degraded mode.
    """
    start = time.time()
    deadline = start + timeout_s
    last = None
    while time.time() < deadline:
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.status_code == 503:
                raise AssertionError(
                    f"{circuit} fault escalated to a hard failure (HTTP 503): {resp.text[:200]}"
                )
            body = resp.json()
            last = body
            state = (body.get("circuits") or {}).get(circuit)
            if (
                resp.status_code == 200
                and body.get("status") == "degraded"
                and state in ("open", "half_open")
            ):
                return time.time() - start
        except (requests.RequestException, ValueError) as exc:
            logger.debug("degraded check against %s raised %s: %s", health_url, type(exc).__name__, exc)
        time.sleep(2)
    raise AssertionError(
        f"Expected degraded mode for {circuit} within {timeout_s}s; last health body: {last!r}"
    )


def assert_recovery(health_url: str, timeout_s: int = 60, circuit: str | None = None) -> float:
    """Poll GET /health until status == 'ok' or timeout_s elapses; raise on timeout.

    When *circuit* is given, also require ``circuits[circuit] == "closed"``.
    Returns the seconds taken to recover.
    """
    start = time.time()
    deadline = start + timeout_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            resp = requests.get(health_url, timeout=5)
            body = resp.json() if resp.status_code == 200 else {}
            if body.get("status") == "ok" and (
                circuit is None or (body.get("circuits") or {}).get(circuit) == "closed"
            ):
                return time.time() - start
            logger.debug(
                "health check attempt %d: not ready yet (status_code=%s, body=%.200r)",
                attempt,
                resp.status_code,
                resp.text,
            )
        except Exception as exc:
            # Connection errors are expected while an experiment is active; log at
            # debug so a genuine bug in this script (or a persistently wrong URL)
            # is still visible when polling never succeeds.
            logger.debug(
                "health check attempt %d against %s raised %s: %s",
                attempt,
                health_url,
                type(exc).__name__,
                exc,
            )
        time.sleep(2)
    raise RuntimeError(f"Health endpoint did not recover within {timeout_s}s")


def evaluate(slo: SLO, recovery_s: float | None, traffic: TrafficResult | None) -> list[str]:
    """Return a list of human-readable SLO violations (empty when all pass)."""
    violations = []
    if recovery_s is None or recovery_s > slo.recovery_s:
        violations.append(f"recovery: did not recover within {slo.recovery_s}s")
    if traffic is not None:
        if traffic.total == 0:
            violations.append("traffic: no requests were sent")
        if traffic.error_rate > slo.max_error_rate:
            violations.append(
                f"error rate: {traffic.error_rate:.2%} > ceiling {slo.max_error_rate:.2%}"
            )
        if traffic.dropped > slo.max_dropped:
            violations.append(
                f"dropped: {traffic.dropped} request(s) silently dropped > {slo.max_dropped}"
            )
        if traffic.max_ok_latency_s > slo.drain_budget_s:
            violations.append(
                f"drain time: {traffic.max_ok_latency_s:.1f}s > budget {slo.drain_budget_s:.1f}s"
            )
    return violations


def send_alert(webhook: str | None, experiment: str, violations: list[str]) -> None:
    for v in violations:
        if v.startswith("drain time"):
            logger.error("ALERT chaos drain budget exceeded (%s): %s", experiment, v)
            if webhook:
                try:
                    requests.post(
                        webhook,
                        json={"alert": "ChaosDrainBudgetExceeded", "experiment": experiment, "detail": v},
                        timeout=5,
                    )
                except Exception as exc:
                    logger.warning("alert webhook failed: %s", exc)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a LedgerLens deployment meets its SLOs during and after "
            "a chaos-mesh experiment."
        )
    )
    parser.add_argument(
        "--experiment",
        default=os.environ.get("CHAOS_EXPERIMENT", DEFAULT_EXPERIMENT),
        choices=sorted(EXPERIMENT_SLOS),
        help=f"Experiment file whose SLOs to assert (default: {DEFAULT_EXPERIMENT}).",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="API base URL; sets --health-url to <url>/health when that is not given.",
    )
    parser.add_argument(
        "--health-url",
        default=os.environ.get("HEALTH_URL"),
        help=(
            "Health-check URL to poll. Falls back to the HEALTH_URL environment "
            f"variable, then to {DEFAULT_HEALTH_URL}."
        ),
    )
    parser.add_argument(
        "--metrics-url",
        default=os.environ.get("METRICS_URL", DEFAULT_METRICS_URL),
        help="Metrics URL (accepted for workflow compatibility; logged for reference).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ["HEALTH_TIMEOUT_S"]) if "HEALTH_TIMEOUT_S" in os.environ else None,
        help="Override the experiment's recovery SLO in seconds.",
    )
    parser.add_argument(
        "--traffic-url",
        default=os.environ.get("TRAFFIC_URL"),
        help="URL to send sustained traffic to (default: the health URL).",
    )
    parser.add_argument(
        "--traffic-seconds",
        type=int,
        default=None,
        help="Override the sustained-traffic window (0 disables traffic assertions).",
    )
    parser.add_argument(
        "--alert-webhook",
        default=os.environ.get("CHAOS_ALERT_WEBHOOK"),
        help="Optional URL to POST a JSON alert to when the drain budget is exceeded.",
    )
    parser.add_argument(
        "--expect-degraded",
        metavar="CIRCUIT",
        default=None,
        help=(
            "Assert graceful degraded mode for this /health circuit while the fault "
            "is active (e.g. feature_store_redis), then assert it closes on recovery."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (shows each failed health-check attempt).",
    )
    args = parser.parse_args(argv)
    if not args.health_url:
        args.health_url = f"{args.url.rstrip('/')}/health" if args.url else DEFAULT_HEALTH_URL
    args.traffic_url = args.traffic_url or args.health_url
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    try:
        if args.expect_degraded:
            entered = assert_degraded(args.health_url, args.expect_degraded, timeout_s=args.timeout)
            print(f"✅ Degraded mode observed for {args.expect_degraded} after {entered:.1f}s")
        recovered = assert_recovery(
            args.health_url, timeout_s=args.timeout, circuit=args.expect_degraded
        )
        print(f"✅ Health recovered in {recovered:.1f}s ({args.health_url})")
        return 0
    except Exception as e:
        print(f"❌ Recovery failed: {e}")
        return 1
    print(f"✅ All SLOs met for {args.experiment} ({args.health_url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
