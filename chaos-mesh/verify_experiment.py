"""Post-experiment recovery check for chaos-mesh experiments.

Usage
-----
    python chaos-mesh/verify_experiment.py --health-url https://ledgerlens.staging.example/health

    # Local default (http://localhost:8000/health), e.g. against a port-forward
    python chaos-mesh/verify_experiment.py

    # Environment variable instead of the flag
    HEALTH_URL=https://ledgerlens.staging.example/health python chaos-mesh/verify_experiment.py

Workflow
    This script is the "verify" step of the chaos-testing loop:
    apply an experiment YAML in this directory (deploy) -> watch the system
    while the fault is injected (observe) -> run this script once the
    experiment's ``duration`` has elapsed (verify).  It polls ``GET /health``
    every 2s until the endpoint returns HTTP 200 with ``{"status": "ok"}`` or
    ``--timeout`` seconds (default 60, ``HEALTH_TIMEOUT_S``) pass.

    Connection errors during polling are expected while the fault is active and
    are logged at DEBUG (use ``-v`` to see them); only a failure to recover
    before the timeout is treated as an error.

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
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_HEALTH_URL = "http://localhost:8000/health"
DEFAULT_METRICS_URL = "http://localhost:8000/metrics"

# Kept for backwards compatibility with callers importing these names directly.
HEALTH_URL = os.environ.get("HEALTH_URL", DEFAULT_HEALTH_URL)
METRICS_URL = os.environ.get("METRICS_URL", DEFAULT_METRICS_URL)


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


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a LedgerLens deployment recovers after a chaos-mesh "
            "experiment by polling its /health endpoint."
        )
    )
    parser.add_argument(
        "--health-url",
        default=os.environ.get("HEALTH_URL", DEFAULT_HEALTH_URL),
        help=(
            "Health-check URL to poll. Falls back to the HEALTH_URL environment "
            f"variable, then to {DEFAULT_HEALTH_URL}."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("HEALTH_TIMEOUT_S", "60")),
        help="Seconds to keep polling before declaring the recovery failed (default: 60).",
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
    return parser.parse_args(argv)


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


if __name__ == "__main__":
    raise SystemExit(main())
