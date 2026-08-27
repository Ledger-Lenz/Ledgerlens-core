import argparse
import os
import time

import requests

DEFAULT_HEALTH_URL = "http://localhost:8000/health"
DEFAULT_METRICS_URL = "http://localhost:8000/metrics"

# Kept for backwards compatibility with callers importing these names directly.
HEALTH_URL = os.environ.get("HEALTH_URL", DEFAULT_HEALTH_URL)
METRICS_URL = os.environ.get("METRICS_URL", DEFAULT_METRICS_URL)


def assert_recovery(health_url: str, timeout_s: int = 60) -> None:
    """Poll GET /health until status == 'ok' or timeout_s elapses; raise on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = requests.get(health_url, timeout=5)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return
        except Exception:
            pass
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
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        assert_recovery(args.health_url, timeout_s=args.timeout)
        print(f"✅ Health recovered ({args.health_url})")
        return 0
    except Exception as e:
        print(f"❌ Recovery failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
