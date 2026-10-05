"""gRPC Internal Scoring Service for Low-Latency Score Delivery (Issue #338).

Streaming flow control (#971)
-----------------------------
Streaming responses are produced on a worker thread into a bounded per-client
buffer (``settings.grpc_stream_buffer_size`` messages).  When the client reads
slower than scores are produced the buffer fills and the producer blocks, so
server memory per stream is capped at the buffer size rather than growing
without bound.  If the buffer stays full for
``settings.grpc_slow_client_timeout_seconds`` the client is considered stuck:
the RPC is cancelled with ``RESOURCE_EXHAUSTED`` and
``ledgerlens_grpc_backpressure_disconnects_total`` is incremented.  Buffer
depth is exported as ``ledgerlens_grpc_stream_buffer_occupancy``.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable, Iterator

import grpc

from api import policy
from config.settings import settings
from detection import storage
from generated import scoring_pb2, scoring_pb2_grpc

logger = logging.getLogger("ledgerlens.grpc_scoring_service")


def mask_wallet(wallet: str) -> str:
    """Format wallet as GABC1234...WXYZ (first 8 chars, '...', last 4 chars)."""
    if not wallet:
        return ""
    if len(wallet) <= 12:
        return wallet
    return f"{wallet[:8]}...{wallet[-4:]}"


_GRPC_STATUS = {
    policy.UNAUTHENTICATED: grpc.StatusCode.UNAUTHENTICATED,
    policy.FORBIDDEN: grpc.StatusCode.PERMISSION_DENIED,
    policy.RATE_LIMITED: grpc.StatusCode.RESOURCE_EXHAUSTED,
}


def _authenticate(context: grpc.ServicerContext, required_scope: str = "read:scores") -> dict:
    """Enforce auth / scope / rate limit via the shared policy layer (#969)."""
    metadata = dict(context.invocation_metadata())
    api_key = metadata.get("x-ledgerlens-api-key", "")
    admin_key = metadata.get("x-ledgerlens-admin-key", "") or api_key
    if not api_key and not admin_key:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing x-ledgerlens-api-key metadata")

    decision = policy.enforce(required_scope, admin_key=admin_key, api_key=api_key)
    if not decision.allowed:
        context.abort(_GRPC_STATUS[decision.status], decision.detail)
    return decision.key_meta


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


class _StreamAbort(Exception):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


def _stream_with_backpressure(
    produce: Callable[[], Iterator], context: grpc.ServicerContext
) -> Iterator:
    """Relay *produce()* to the client through a bounded buffer.

    The producer runs on its own thread and blocks when the buffer is full;
    a client that keeps the buffer full past the slow-client timeout is
    disconnected with RESOURCE_EXHAUSTED.
    """
    from api.metrics import grpc_backpressure_disconnects_total, grpc_stream_buffer_occupancy

    buf: queue.Queue = queue.Queue(maxsize=settings.grpc_stream_buffer_size)
    slow_timeout = settings.grpc_slow_client_timeout_seconds
    stop = threading.Event()
    finished = threading.Event()
    state: dict = {"error": None, "slow": False}

    def _put(item) -> bool:
        deadline = time.monotonic() + slow_timeout
        while not stop.is_set():
            try:
                buf.put(item, timeout=min(0.05, max(deadline - time.monotonic(), 0)))
                grpc_stream_buffer_occupancy.observe(buf.qsize())
                return True
            except queue.Full:
                if time.monotonic() >= deadline:
                    state["slow"] = True
                    return False
        return False

    def _run() -> None:
        try:
            for item in produce():
                if not _put(item):
                    break
        except Exception as exc:  # re-raised on the RPC thread
            state["error"] = exc
        finally:
            finished.set()
            if state["slow"]:
                grpc_backpressure_disconnects_total.inc()
                logger.warning(
                    "gRPC slow client disconnected peer=%s buffer=%d timeout=%.1fs",
                    context.peer(),
                    buf.maxsize,
                    slow_timeout,
                )
                context.cancel()

    threading.Thread(target=_run, name="grpc-stream-producer", daemon=True).start()
    try:
        while True:
            try:
                item = buf.get(timeout=0.05)
            except queue.Empty:
                if finished.is_set() and buf.empty():
                    break
                continue
            yield item
    finally:
        stop.set()

    if state["slow"]:
        context.abort(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            f"Client consumed too slowly (buffer full for {slow_timeout}s)",
        )
    error = state["error"]
    if isinstance(error, _StreamAbort):
        context.abort(error.code, error.details)
    if error is not None:
        raise error


class ScoringServicer(scoring_pb2_grpc.ScoringServiceServicer):
    """gRPC Servicer implementing ScoringService."""

    def ScoreWallet(self, request: scoring_pb2.ScoreRequest, context: grpc.ServicerContext) -> scoring_pb2.RiskScoreProto:
        _authenticate(context, required_scope="read:scores")
        scores = storage.get_latest_scores(request.wallet, asset_pair=request.asset_pair or None)
        if not scores:
            context.abort(grpc.StatusCode.NOT_FOUND, f"No score for {mask_wallet(request.wallet)}")
        return _to_proto(scores[0])

    def BatchScoreWallets(
        self,
        request_iterator: Iterator[scoring_pb2.ScoreRequest],
        context: grpc.ServicerContext,
    ):
        _authenticate(context, required_scope="read:scores")
        max_batch = settings.grpc_max_batch_wallets

        def produce():
            count = 0
            for request in request_iterator:
                count += 1
                if count > max_batch:
                    raise _StreamAbort(
                        grpc.StatusCode.RESOURCE_EXHAUSTED,
                        f"Batch size exceeds maximum limit of {max_batch} wallets",
                    )
                scores = storage.get_latest_scores(
                    request.wallet, asset_pair=request.asset_pair or None
                )
                if scores:
                    yield _to_proto(scores[0])

        yield from _stream_with_backpressure(produce, context)


class AuthInterceptor(grpc.ServerInterceptor):
    """gRPC Server Interceptor for API key and scope validation.

    Intercepts all unary and streaming RPC calls to validate
    authentication before they reach the servicer.
    """

    def __init__(self, required_scope: str = "read:scores"):
        self.required_scope = required_scope

    def intercept_service(self, continuation, handler_call_details):
        """Validate auth metadata before the RPC handler is invoked."""
        # Extract method name from the handler call details
        method_name = handler_call_details.method
        if method_name.endswith("/ScoreWallet") or method_name.endswith("/BatchScoreWallets"):
            # Authentication will be performed by the servicer methods themselves
            # via _authenticate(), so we just pass through here.
            # The interceptor is kept as an extension point for future
            # pre-validation (e.g., IP allowlisting, rate limiting headers).
            pass
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
