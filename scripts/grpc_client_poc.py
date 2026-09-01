#!/usr/bin/env python3
"""Small client proof of concept for the LedgerLens gRPC scoring service.

This intentionally uses the generated protobuf bindings directly. Production
SDKs should wrap the same generated client with language-specific error,
deadline, retry, and cancellation policies.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import grpc

# Running ``python scripts/grpc_client_poc.py`` puts ``scripts/`` rather than
# the repository root on sys.path. Add the root explicitly so this example is
# usable from a clean checkout without an editable install.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from generated import scoring_pb2, scoring_pb2_grpc  # noqa: E402


def build_channel(endpoint: str, *, insecure: bool, ca_cert: Path | None) -> grpc.Channel:
    """Create a secure or explicitly insecure channel for ``endpoint``."""
    if insecure:
        return grpc.insecure_channel(endpoint)

    root_certificates = ca_cert.read_bytes() if ca_cert else None
    credentials = grpc.ssl_channel_credentials(root_certificates=root_certificates)
    return grpc.secure_channel(endpoint, credentials)


def score_wallets(
    stub: scoring_pb2_grpc.ScoringServiceStub,
    wallets: list[str],
    api_key: str,
    timeout: float,
) -> tuple[list[scoring_pb2.RiskScoreProto], list[scoring_pb2.RiskScoreProto]]:
    """Call both RPCs and return the unary and streaming responses."""
    metadata = (("x-ledgerlens-api-key", api_key),)
    unary_responses = [
        stub.ScoreWallet(scoring_pb2.ScoreRequest(wallet=wallet), metadata=metadata, timeout=timeout)
        for wallet in wallets
    ]
    stream_responses = list(
        stub.BatchScoreWallets(
            (scoring_pb2.ScoreRequest(wallet=wallet) for wallet in wallets),
            metadata=metadata,
            timeout=timeout,
        )
    )
    return unary_responses, stream_responses


def _score_to_dict(score: scoring_pb2.RiskScoreProto) -> dict[str, object]:
    """Convert a protobuf response to JSON-safe output while preserving presence."""
    result: dict[str, object] = {
        "wallet": score.wallet,
        "asset_pair": score.asset_pair,
        "score": score.score,
        "benford_flag": score.benford_flag,
        "ml_flag": score.ml_flag,
        "confidence": score.confidence,
        "timestamp": score.timestamp,
    }
    for field in ("score_lower", "score_upper", "coverage_guarantee"):
        if score.HasField(field):
            result[field] = getattr(score, field)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="localhost:50051", help="gRPC host:port")
    parser.add_argument(
        "--wallet",
        action="append",
        required=True,
        help="Wallet to score; repeat for the streaming batch call",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key; defaults to LEDGERLENS_API_KEY",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-RPC deadline in seconds")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Use plaintext transport for local development only",
    )
    parser.add_argument("--ca-cert", type=Path, help="PEM CA certificate for TLS")
    args = parser.parse_args()

    import os

    api_key = args.api_key or os.environ.get("LEDGERLENS_API_KEY")
    if not api_key:
        parser.error("--api-key or LEDGERLENS_API_KEY is required")

    if args.insecure and args.ca_cert:
        parser.error("--ca-cert cannot be combined with --insecure")

    channel = build_channel(args.endpoint, insecure=args.insecure, ca_cert=args.ca_cert)
    try:
        stub = scoring_pb2_grpc.ScoringServiceStub(channel)
        unary, streaming = score_wallets(stub, args.wallet, api_key, args.timeout)
        print(
            json.dumps(
                {
                    "endpoint": args.endpoint,
                    "unary": [_score_to_dict(score) for score in unary],
                    "streaming": [_score_to_dict(score) for score in streaming],
                },
                indent=2,
            )
        )
    finally:
        channel.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
