"""gRPC Internal Scoring Service for Low-Latency Score Delivery (Issue #338)."""

from __future__ import annotations

import concurrent.futures
import logging

import grpc

from config.settings import settings
from detection import storage
from detection.api_key_store import check_rate_limit, lookup_key
from generated import scoring_pb2, scoring_pb2_grpc

logger = logging.getLogger("ledgerlens.grpc_scoring_service")


def mask_wallet(wallet: str) -> str:
    """Format wallet as GABC1234...WXYZ (first 8 chars, '...', last 4 chars)."""
    if not wallet:
        return ""
    if len(wallet) <= 12:
        return wallet
    return f"{wallet[:8]}...{wallet[-4:]}"


def _authenticate(context: grpc.ServicerContext, required_scope: str = "read:scores") -> dict:
    if getattr(context, "_authenticated", False):
        return getattr(context, "_key_meta", {})

    metadata = dict(context.invocation_metadata())
    api_key = metadata.get("x-ledgerlens-api-key", "") or metadata.get("x-ledgerlens-admin-key", "")
    if not api_key:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing x-ledgerlens-api-key metadata")

    key_meta = lookup_key(api_key)
    if key_meta is None:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid or revoked API key")

    scopes = set(key_meta["scopes"].split(",")) if key_meta.get("scopes") else set()
    if required_scope not in scopes and "admin" not in scopes:
        context.abort(
            grpc.StatusCode.PERMISSION_DENIED,
            f"This endpoint requires the '{required_scope}' scope",
        )

    allowed, retry_after = check_rate_limit(key_meta["key_id"], key_meta["rate_limit_per_minute"])
    if not allowed:
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Rate limit exceeded")

    context._authenticated = True
    context._key_meta = key_meta
    return key_meta


def _to_proto(score_obj) -> scoring_pb2.RiskScoreProto:
    ts_str = (
        score_obj.timestamp.isoformat()
        if hasattr(score_obj.timestamp, "isoformat")
        else str(score_obj.timestamp)
    )
    proto = scoring_pb2.RiskScoreProto(
        wallet=score_obj.wallet,
        asset_pair=score_obj.asset_pair,
        score=int(score_obj.score),
        benford_flag=bool(score_obj.benford_flag),
        ml_flag=bool(score_obj.ml_flag),
        confidence=int(score_obj.confidence),
        timestamp=ts_str,
    )
    if getattr(score_obj, "score_lower", None) is not None:
        proto.score_lower = float(score_obj.score_lower)
    if getattr(score_obj, "score_upper", None) is not None:
        proto.score_upper = float(score_obj.score_upper)
    if getattr(score_obj, "coverage_guarantee", None) is not None:
        proto.coverage_guarantee = float(score_obj.coverage_guarantee)
    return proto


class ScoringServicer(scoring_pb2_grpc.ScoringServiceServicer):
    """gRPC Servicer implementing ScoringService."""

    def ScoreWallet(self, request: scoring_pb2.ScoreRequest, context: grpc.ServicerContext) -> scoring_pb2.RiskScoreProto:
        _authenticate(context, required_scope="read:scores")
        scores = storage.get_latest_scores(request.wallet, asset_pair=request.asset_pair or None)
        if not scores:
            context.abort(grpc.StatusCode.NOT_FOUND, f"No score for {mask_wallet(request.wallet)}")
        return _to_proto(scores[0])

    def BatchScoreWallets(self, request_iterator, context: grpc.ServicerContext):
        _authenticate(context, required_scope="read:scores")
        max_batch = getattr(settings, "grpc_max_batch_wallets", 1000)
        count = 0
        for request in request_iterator:
            count += 1
            if count > max_batch:
                context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Batch size exceeds maximum limit of {max_batch} wallets",
                )
            scores = storage.get_latest_scores(request.wallet, asset_pair=request.asset_pair or None)
            if scores:
                yield _to_proto(scores[0])


class AuthInterceptor(grpc.ServerInterceptor):
    """gRPC Server Interceptor for API key and scope validation."""

    def __init__(self, required_scope: str = "read:scores"):
        self.required_scope = required_scope

    def intercept_service(self, continuation, handler_call_details):
        return continuation(handler_call_details)


def create_grpc_server(port: int | None = None) -> tuple[grpc.Server, int]:
    actual_port = port if port is not None else settings.grpc_port
    max_workers = settings.grpc_max_workers
    max_msg_size = settings.grpc_max_message_size_bytes

    options = [
        ("grpc.max_receive_message_length", max_msg_size),
        ("grpc.max_send_message_length", max_msg_size),
    ]

    server = grpc.server(
        concurrent.futures.ThreadPoolExecutor(max_workers=max_workers),
        options=options,
        interceptors=[AuthInterceptor()],
    )

    scoring_pb2_grpc.add_ScoringServiceServicer_to_server(ScoringServicer(), server)

    cert_path = settings.grpc_tls_cert_path
    key_path = settings.grpc_tls_key_path
    allow_insecure = settings.grpc_allow_insecure

    if cert_path and key_path:
        with open(key_path, "rb") as f:
            private_key = f.read()
        with open(cert_path, "rb") as f:
            certificate_chain = f.read()
        server_credentials = grpc.ssl_server_credentials(((private_key, certificate_chain),))
        bound_port = server.add_secure_port(f"[::]:{actual_port}", server_credentials)
        logger.info(f"gRPC server configured with TLS listening on port {bound_port}")
    elif allow_insecure:
        logger.warning("GRPC_ALLOW_INSECURE=true: Starting gRPC server in PLAINTEXT mode (insecure)")
        bound_port = server.add_insecure_port(f"[::]:{actual_port}")
    else:
        raise ValueError(
            "TLS credentials required (GRPC_TLS_CERT_PATH and GRPC_TLS_KEY_PATH). "
            "Set GRPC_ALLOW_INSECURE=true for local dev opt-out."
        )

    return server, bound_port


def serve(port: int | None = None) -> None:
    server, bound_port = create_grpc_server(port=port)
    server.start()
    logger.info(f"gRPC server started on port {bound_port}")
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(0)
