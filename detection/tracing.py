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
import random
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Callable, Generator

from config.settings import settings

logger = logging.getLogger("ledgerlens.tracing")

_OTEL_AVAILABLE = False
try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.propagate import extract, inject
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.trace.sampling import Sampler, SamplingResult, Decision
    from opentelemetry.trace import StatusCode

    _OTEL_AVAILABLE = True
except ImportError:
    logger.warning(
        "opentelemetry packages not installed — tracing is disabled. "
        "Install with: pip install opentelemetry-sdk opentelemetry-exporter-otlp"
    )


class TailSamplingSpanProcessor(SpanProcessor):
    def __init__(
        self,
        wrapped_processor: SpanProcessor,
        baseline_ratio: float,
        buffer_timeout_seconds: float,
        max_buffered_traces: int,
        slow_threshold_ms: float = 2000.0,
    ):
        self._wrapped = wrapped_processor
        self._baseline_ratio = baseline_ratio
        self._buffer_timeout = buffer_timeout_seconds
        self._max_buffered = max_buffered_traces
        self._slow_threshold_ms = slow_threshold_ms

        self._lock = threading.Lock()
        self._traces: dict[str, _BufferedTrace] = {}
        self._shutdown = False

        # Start background flush thread
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="tail-sampling-flush",
        )
        self._flush_thread.start()

    def _evaluate_trace(self, buffered: _BufferedTrace) -> tuple[bool, str]:
        """Evaluate whether to keep a trace and return (keep, reason)."""
        # Check for error spans
        if buffered.has_error:
            return True, "error"

        # Check for slow spans
        if buffered.max_latency_ms > self._slow_threshold_ms:
            return True, "slow"

        # Check for circuit-open soroban.submit_score spans
        if buffered.has_circuit_open:
            return True, "circuit_open"

        # Baseline probabilistic sampling
        if random.random() < self._baseline_ratio:
            return True, "baseline"

        return False, "dropped"

    def _flush_trace(self, trace_id: str):
        """Flush a single trace by either exporting or discarding it."""
        with self._lock:
            buffered = self._traces.pop(trace_id, None)
        if not buffered:
            return

        keep, reason = self._evaluate_trace(buffered)
        if keep:
            # Add sampling reason attribute to all spans
            for span in buffered.spans:
                span.set_attribute("ledgerlens.sampling.reason", reason)
                self._wrapped.on_end(span)

    def _flush_loop(self):
        """Background thread to flush expired traces."""
        while not self._shutdown:
            time.sleep(1.0)
            now = time.monotonic()
            with self._lock:
                expired_traces = [
                    trace_id
                    for trace_id, buffered in self._traces.items()
                    if now - buffered.start_time > self._buffer_timeout
                ]
            for trace_id in expired_traces:
                self._flush_trace(trace_id)

    def on_start(self, span, parent_context=None):
        self._wrapped.on_start(span, parent_context)

    def on_end(self, span):
        if self._shutdown:
            return

        trace_id = format(span.get_span_context().trace_id, "032x")
        is_root = span.parent is None
        span_duration = (span.end_time - span.start_time) / 1_000_000.0 if (span.end_time and span.start_time) else 0.0
        has_error = span.status.status_code == StatusCode.ERROR
        is_soroban_span = span.name == "soroban.submit_score"
        circuit_state = span.attributes.get("circuit_state", "closed") if span.attributes else "closed"

        with self._lock:
            if trace_id not in self._traces:
                if len(self._traces) >= self._max_buffered:
                    # If buffer is full, drop oldest trace
                    oldest_id = next(iter(self._traces.keys()))
                    del self._traces[oldest_id]
                self._traces[trace_id] = _BufferedTrace(start_time=time.monotonic())
            buffered = self._traces[trace_id]
            buffered.spans.append(span)
            buffered.max_latency_ms = max(buffered.max_latency_ms, span_duration)
            buffered.has_error = buffered.has_error or has_error
            if is_soroban_span and circuit_state not in ("closed",):
                buffered.has_circuit_open = True

        if is_root:
            self._flush_trace(trace_id)

    def shutdown(self):
        self._shutdown = True
        if self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30_000):
        self._wrapped.force_flush(timeout_millis)


class _BufferedTrace:
    __slots__ = ("start_time", "spans", "max_latency_ms", "has_error", "has_circuit_open")
    def __init__(self, start_time: float):
        self.start_time = start_time
        self.spans: list = []
        self.max_latency_ms: float = 0.0
        self.has_error: bool = False
        self.has_circuit_open: bool = False


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
    processors = []
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        otlp_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        processors.append(BatchSpanProcessor(otlp_exporter))
        logger.info("OpenTelemetry OTLP exporter configured → %s", endpoint)
    except Exception as exc:
        logger.warning("Failed to configure OTLP exporter (%s) — falling back to console", exc)
        console_export = True

    if console_export:
        processors.append(BatchSpanProcessor(ConsoleSpanExporter()))

    # Wrap with tail sampling if enabled
    if settings.trace_sampling_strategy == "tail":
        for i in range(len(processors)):
            processors[i] = TailSamplingSpanProcessor(
                wrapped_processor=processors[i],
                baseline_ratio=settings.trace_tail_baseline_ratio,
                buffer_timeout_seconds=settings.trace_tail_buffer_timeout_seconds,
                max_buffered_traces=settings.trace_tail_max_buffered_traces,
            )

    for processor in processors:
        provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)
    logger.info("OpenTelemetry tracing configured for service '%s' (sampling: %s)", svc, settings.trace_sampling_strategy)


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
