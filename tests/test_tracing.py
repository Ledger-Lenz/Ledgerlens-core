"""Tests for distributed tracing context propagation (Issue #199).

Verifies W3C TraceContext header propagation, async context preservation,
and span creation across service boundaries.  Does not require a live Jaeger
instance — uses in-memory span exporters.
"""

from __future__ import annotations

import asyncio
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _try_import_otel():
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry import trace
        return TracerProvider, InMemorySpanExporter, SimpleSpanProcessor, trace
    except ImportError:
        return None


otel = _try_import_otel()
pytestmark = pytest.mark.skipif(otel is None, reason="opentelemetry-sdk not installed")


@pytest.fixture()
def in_memory_tracer():
    """Set up an in-memory OTel tracer that captures all spans."""
    if otel is None:
        pytest.skip("opentelemetry-sdk not installed")

    TracerProvider, InMemorySpanExporter, SimpleSpanProcessor, trace = otel
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    exporter.clear()
    # Reset to default no-op provider
    trace.set_tracer_provider(trace.ProxyTracerProvider())


# ---------------------------------------------------------------------------
# Unit tests — no OTel dependency path
# ---------------------------------------------------------------------------

def test_configure_tracing_noop_when_unavailable(monkeypatch):
    """configure_tracing() should not raise when opentelemetry is absent."""
    import detection.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_OTEL_AVAILABLE", False)
    tracing_mod.configure_tracing()  # must not raise


def test_get_tracer_returns_noop_when_unavailable(monkeypatch):
    """get_tracer() returns a no-op stub when OTel is not installed."""
    import detection.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_OTEL_AVAILABLE", False)
    tracer = tracing_mod.get_tracer()
    assert tracer is not None
    # No-op span should not raise
    with tracer.start_as_current_span("test"):
        pass


def test_start_span_noop(monkeypatch):
    """start_span() runs without error when OTel is unavailable."""
    import detection.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_OTEL_AVAILABLE", False)
    with tracing_mod.start_span("test.span"):
        pass


def test_propagate_context_noop(monkeypatch):
    """propagate_context_to_headers() returns headers dict unchanged when OTel is unavailable."""
    import detection.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_OTEL_AVAILABLE", False)
    headers = {"content-type": "application/json"}
    result = tracing_mod.propagate_context_to_headers(headers)
    assert result is headers


def test_extract_context_noop(monkeypatch):
    """extract_context_from_headers() returns None when OTel is unavailable."""
    import detection.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_OTEL_AVAILABLE", False)
    result = tracing_mod.extract_context_from_headers({"traceparent": "00-abc-def-01"})
    assert result is None


# ---------------------------------------------------------------------------
# Tests that require opentelemetry-sdk
# ---------------------------------------------------------------------------

def test_start_span_creates_span(in_memory_tracer):
    """start_span() context manager creates a span captured by the exporter."""
    from detection.tracing import start_span

    with start_span("test.db.query", attributes={"table": "risk_scores"}):
        pass

    spans = in_memory_tracer.get_finished_spans()
    assert any(s.name == "test.db.query" for s in spans), (
        f"Expected 'test.db.query' span, got: {[s.name for s in spans]}"
    )


def test_start_span_attributes_set(in_memory_tracer):
    """Attributes passed to start_span are recorded on the span."""
    from detection.tracing import start_span

    with start_span("test.redis.lookup", attributes={"wallet": "GABC", "asset_pair": "XLM/USDC"}):
        pass

    spans = in_memory_tracer.get_finished_spans()
    target = next((s for s in spans if s.name == "test.redis.lookup"), None)
    assert target is not None
    assert target.attributes.get("wallet") == "GABC"
    assert target.attributes.get("asset_pair") == "XLM/USDC"


def test_nested_spans_share_trace_id(in_memory_tracer):
    """Child spans created inside a parent share the same trace ID."""
    from detection.tracing import start_span

    with start_span("parent.span"):
        with start_span("child.db"):
            pass
        with start_span("child.redis"):
            pass

    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) >= 3

    trace_ids = {s.context.trace_id for s in spans if s.context}
    assert len(trace_ids) == 1, (
        f"All spans must share one trace ID, got {len(trace_ids)} distinct IDs"
    )


def test_propagate_and_extract_traceparent(in_memory_tracer):
    """W3C traceparent injected into headers can be extracted back."""
    from detection.tracing import extract_context_from_headers, propagate_context_to_headers, start_span

    headers = {}
    with start_span("outbound.http"):
        propagate_context_to_headers(headers)

    assert "traceparent" in headers, (
        f"traceparent header not injected: {headers}"
    )
    extracted = extract_context_from_headers(headers)
    assert extracted is not None


def test_async_span_preserves_trace_id(in_memory_tracer):
    """async_span() preserves the trace ID inside an async coroutine."""
    from detection.tracing import async_span, start_span

    async def _inner():
        async with async_span("async.child"):
            pass

    async def _run():
        with start_span("async.parent"):
            await _inner()

    asyncio.get_event_loop().run_until_complete(_run())

    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) >= 2
    trace_ids = {s.context.trace_id for s in spans if s.context}
    assert len(trace_ids) == 1, "Async span broke trace context"


def test_create_task_with_context_preserves_trace(in_memory_tracer):
    """create_task_with_context preserves trace ID across asyncio task boundary."""
    from detection.tracing import create_task_with_context, start_span

    async def _task_work():
        from detection.tracing import async_span
        async with async_span("task.span"):
            pass

    async def _run():
        with start_span("task.parent"):
            task = create_task_with_context(_task_work())
            await task

    asyncio.get_event_loop().run_until_complete(_run())

    spans = in_memory_tracer.get_finished_spans()
    names = [s.name for s in spans]
    assert "task.span" in names, f"Task span missing: {names}"

    trace_ids = {s.context.trace_id for s in spans if s.context}
    assert len(trace_ids) == 1, (
        f"Task boundary broke trace context: {len(trace_ids)} distinct trace IDs"
    )


def test_outgoing_headers_contain_traceparent(in_memory_tracer):
    """HTTP headers prepared for outbound calls include traceparent."""
    from detection.tracing import propagate_context_to_headers, start_span

    outbound_headers = {"Content-Type": "application/json"}
    with start_span("horizon.http.call"):
        propagate_context_to_headers(outbound_headers)

    assert "traceparent" in outbound_headers
    # W3C traceparent format: 00-<trace-id>-<span-id>-<flags>
    tp = outbound_headers["traceparent"]
    parts = tp.split("-")
    assert len(parts) == 4, f"Malformed traceparent: {tp}"
    assert parts[0] == "00", "Version must be 00"
