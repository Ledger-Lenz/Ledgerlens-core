#!/usr/bin/env python3
"""generate_traffic.py

Generates synthetic load against the LedgerLens API.
Fires HTTP requests sequentially at a steady rate to simulate background user activity.
"""

from __future__ import annotations

import argparse
import sys
import time
import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthetic Load Generator")
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="Base URL of the LedgerLens API under test",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="How long to generate traffic in seconds",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Optional API key for request authentication",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"📈 Starting load generator against {args.url} (duration: {args.duration}s)")
    
    headers = {}
    if args.api_key:
        headers["X-LedgerLens-Api-Key"] = args.api_key
        
    start_time = time.monotonic()
    end_time = start_time + args.duration
    
    requests_sent = 0
    errors = 0
    
    # Target endpoints
    endpoints = [
        "/health",
        "/v1/scores",
    ]
    
    try:
        while time.monotonic() < end_time:
            # Alternate endpoints
            ep = endpoints[requests_sent % len(endpoints)]
            target_url = f"{args.url.rstrip('/')}{ep}"
            
            try:
                # Generous timeout so slow connections don't hang the generator loop
                resp = requests.get(target_url, headers=headers, timeout=2)
                requests_sent += 1
                if resp.status_code >= 500:
                    errors += 1
            except Exception:
                errors += 1
                requests_sent += 1
            
            # Control the rate to ~5 requests per second
            time.sleep(0.2)
            
    except KeyboardInterrupt:
        print("\nStopping load generator...")
        
    duration_actual = time.monotonic() - start_time
    print(f"📊 Traffic Summary: Sent {requests_sent} requests over {duration_actual:.1f}s. Observed {errors} errors/failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
