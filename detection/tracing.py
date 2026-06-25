"""Distributed tracing context propagation for LedgerLens.

Wraps OpenTelemetry SDK with W3C TraceContext (traceparent / tracestate) header
propagation across all async and HTTP boundaries.  Configures an OTLP exporter
targeting Jaeger by default; any OTLP-compatible collector works.

Usage
-----
Call `configure_tracing()` once at application startup (e.g. in the FastAPI
lifespan function).  Use `get_tracer()` to obtain a named tracer wherever
you need spans.  Use `async_span()` to safely propagate context across
``asyncio.create_task()`` boundaries.

Environment variables
---------------------
OTEL_EXPORTER_OTLP_ENDPOINT   OTLP gRPC endpoint (default: http://localhost:4317)
OTEL_SERVICE_NAME              Service name attached to all spans (default: ledgerlens)
OTEL_TRACES_SAMPLER            Sampler type: always_on | always_off | traceidratio
                               (default: always_on)
OTEL_PROPAGATORS               Must include tracecontext (default: tracecontext,baggage)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Callable, Generator

logger = logging.getLogger("ledgerlens.tracing")

_OTEL_AVAILABLE = False
try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract, inject
    from opentelemetry.propagators.b3 import B3MultiFormat  # noqa: F401 (optional)
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    _OTEL_AVAILABLE = True
except ImportError:
    logger.warning(
        "opentelemetry packages not installed — tracing is disabled. "
        "Install with: pip install opentelemetry-sdk opentelemetry-exporter-otlp"
    )


def configure_tracing(
    service_name: str | None = None,
    otlp_endpoint: str | None = None,
    console_export: bool = False,
) -> None:
    """Set up OpenTelemetry with OTLP exporter targeting Jaeger (or any OTLP collector).

    Safe to call multiple times — subsequent calls are no-ops if a real provider
    is already configured.

    Args:
        service_name: Override for OTEL_SERVICE_NAME env var.
        otlp_endpoint: Override for OTEL_EXPORTER_OTLP_ENDPOINT env var.
        console_export: If True, also emit spans to stdout (useful for dev).
    """
    if not _OTEL_AVAILABLE:
        return

    # Avoid reconfiguring if already set up
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return

    svc = service_name or os.getenv("OTEL_SERVICE_NAME", "ledgerlens")
    endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({SERVICE_NAME: svc})
    provider = TracerProvider(resource=resource)

    # OTLP → Jaeger (or any collector)
    try:
        otlp_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info("OpenTelemetry OTLP exporter configured → %s", endpoint)
    except Exception as exc:
        logger.warning("Failed to configure OTLP exporter (%s) — falling back to console", exc)
        console_export = True

    if console_export:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    logger.info("OpenTelemetry tracing configured for service '%s'", svc)


def get_tracer(name: str = "ledgerlens") -> Any:
    """Return a named OpenTelemetry tracer, or a no-op stub if OTel is unavailable."""
    if not _OTEL_AVAILABLE:
        return _NoOpTracer()
    return trace.get_tracer(name)


@contextmanager
def start_span(
    name: str,
    tracer_name: str = "ledgerlens",
    attributes: dict | None = None,
) -> Generator:
    """Context manager that starts a new span and sets optional attributes.

    Example::

        with start_span("redis.feature_lookup", attributes={"wallet": wallet}):
            state = feature_store.get_state(wallet, asset_pair)
    """
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(name) as span:
        if attributes and _OTEL_AVAILABLE:
            for k, v in attributes.items():
                span.set_attribute(k, str(v))
        yield span


@asynccontextmanager
async def async_span(
    name: str,
    tracer_name: str = "ledgerlens",
    attributes: dict | None = None,
) -> AsyncGenerator:
    """Async context manager that starts a span inside an asyncio coroutine.

    Example::

        async with async_span("model.inference"):
            result = await run_model(features)
    """
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(name) as span:
        if attributes and _OTEL_AVAILABLE:
            for k, v in attributes.items():
                span.set_attribute(k, str(v))
        yield span


def propagate_context_to_headers(headers: dict) -> dict:
    """Inject the current W3C traceparent / tracestate into `headers` in-place.

    Call before making outbound HTTP requests so downstream services can
    attach their spans to the same trace.

    Args:
        headers: Mutable dict that will receive ``traceparent`` / ``tracestate``.

    Returns:
        The same `headers` dict (mutated in place).
    """
    if not _OTEL_AVAILABLE:
        return headers
    inject(headers)
    return headers


def extract_context_from_headers(headers: dict):
    """Extract W3C trace context from inbound request headers.

    Returns an OTel Context object that can be passed to
    ``trace.use_span()`` or stored for later re-attachment.
    """
    if not _OTEL_AVAILABLE:
        return None
    return extract(headers)


def task_with_context(coro_fn: Callable) -> Callable:
    """Decorator that propagates the current OTel context into an asyncio task.

    Without this, ``asyncio.create_task()`` severs the trace because each task
    gets a fresh context by default.

    Example::

        @task_with_context
        async def _score_wallet(wallet, features):
            async with async_span("model.inference"):
                ...

        asyncio.create_task(_score_wallet(wallet, features))
    """
    @functools.wraps(coro_fn)
    async def _wrapper(*args, **kwargs):
        if not _OTEL_AVAILABLE:
            return await coro_fn(*args, **kwargs)
        # Capture the current context at task-creation time and attach it
        ctx = otel_context.get_current()
        token = otel_context.attach(ctx)
        try:
            return await coro_fn(*args, **kwargs)
        finally:
            otel_context.detach(token)
    return _wrapper


def create_task_with_context(coro) -> asyncio.Task:
    """Create an asyncio task while preserving the current OTel trace context.

    Drop-in replacement for ``asyncio.create_task()`` at instrumented callsites.

    Example::

        task = create_task_with_context(score_wallet(wallet, features))
    """
    if not _OTEL_AVAILABLE:
        return asyncio.create_task(coro)

    ctx = otel_context.get_current()

    async def _with_context():
        token = otel_context.attach(ctx)
        try:
            return await coro
        finally:
            otel_context.detach(token)

    return asyncio.create_task(_with_context())


# ---------------------------------------------------------------------------
# No-op stub (used when opentelemetry is not installed)
# ---------------------------------------------------------------------------

class _NoOpSpan:
    def set_attribute(self, key, value): pass
    def record_exception(self, exc): pass
    def set_status(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _NoOpTracer:
    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()

    def start_span(self, name, **kwargs):
        return _NoOpSpan()
