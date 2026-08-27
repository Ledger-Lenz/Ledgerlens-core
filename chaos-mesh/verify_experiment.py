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


def assert_recovery(health_url: str, timeout_s: int = 60) -> None:
    """Poll GET /health until status == 'ok' or timeout_s elapses; raise on timeout."""
    deadline = time.time() + timeout_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return
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
        assert_recovery(args.health_url, timeout_s=args.timeout)
        print(f"✅ Health recovered ({args.health_url})")
        return 0
    except Exception as e:
        print(f"❌ Recovery failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
