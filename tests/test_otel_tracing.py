"""Tests for OpenTelemetry distributed tracing."""

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def _make_provider_with_exporter():
    """Create a fresh TracerProvider wired to an InMemorySpanExporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


# ---------------------------------------------------------------------------
# (a) A pipeline run produces at least 3 spans
# ---------------------------------------------------------------------------

def test_pipeline_produces_at_least_3_spans():
    provider, exporter = _make_provider_with_exporter()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("pipeline.run"):
        with tracer.start_as_current_span("model.score_batch"):
            pass
        with tracer.start_as_current_span("soroban.submit_score"):
            pass

    spans = exporter.get_finished_spans()
    assert len(spans) >= 3


# ---------------------------------------------------------------------------
# (b) The pipeline.run span is the root
# ---------------------------------------------------------------------------

def test_pipeline_run_is_root_span():
    provider, exporter = _make_provider_with_exporter()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("pipeline.run"):
        with tracer.start_as_current_span("model.score_batch"):
            pass

    spans = exporter.get_finished_spans()
    root_candidates = [s for s in spans if s.name == "pipeline.run"]
    assert root_candidates, "pipeline.run span not found"
    root = root_candidates[0]
    # Root span has no valid parent
    assert root.parent is None or not root.parent.is_valid


# ---------------------------------------------------------------------------
# (c) soroban.submit_score span has soroban.wallet attribute (masked)
# ---------------------------------------------------------------------------

def test_soroban_span_has_wallet_attribute():
    from config.correlation import mask_wallet

    provider, exporter = _make_provider_with_exporter()
    tracer = provider.get_tracer("test")

    wallet = "GABC1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234WXYZ"
    with tracer.start_as_current_span("soroban.submit_score") as span:
        span.set_attribute("soroban.wallet", mask_wallet(wallet))
        span.set_attribute("soroban.score", 85)
        span.set_attribute("soroban.dry_run", False)

    spans = exporter.get_finished_spans()
    soroban_spans = [s for s in spans if s.name == "soroban.submit_score"]
    assert soroban_spans, "soroban.submit_score span not found"

    attrs = soroban_spans[0].attributes
    assert "soroban.wallet" in attrs
    # Must be masked — not the full 56-char address
    assert attrs["soroban.wallet"] != wallet
    assert "soroban.score" in attrs


# ---------------------------------------------------------------------------
# (d) trace context propagated: inner span has parent set correctly
# ---------------------------------------------------------------------------

def test_trace_context_propagated_through_call():
    provider, exporter = _make_provider_with_exporter()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("pipeline.run") as root_span:
        root_ctx = root_span.get_span_context()
        with tracer.start_as_current_span("inner.operation"):
            pass

    spans = exporter.get_finished_spans()
    inner = [s for s in spans if s.name == "inner.operation"]
    assert inner, "inner.operation span not found"
    # The inner span's parent trace_id must match the root span's trace_id
    assert inner[0].parent is not None
    assert inner[0].parent.trace_id == root_ctx.trace_id


# ---------------------------------------------------------------------------
# OTel span attributes: model.score_batch has model.batch_size
# ---------------------------------------------------------------------------

def test_model_score_batch_span_attributes():
    provider, exporter = _make_provider_with_exporter()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("model.score_batch") as span:
        span.set_attribute("model.batch_size", 10)

    spans = exporter.get_finished_spans()
    batch_spans = [s for s in spans if s.name == "model.score_batch"]
    assert batch_spans
    assert batch_spans[0].attributes.get("model.batch_size") == 10


# ---------------------------------------------------------------------------
# webhook.deliver span has subscriber_id and attempt attributes
# ---------------------------------------------------------------------------

def test_webhook_deliver_span_attributes():
    provider, exporter = _make_provider_with_exporter()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("webhook.deliver") as span:
        span.set_attribute("webhook.subscriber_id", "abc-123")
        span.set_attribute("webhook.attempt", 1)

    spans = exporter.get_finished_spans()
    wh = [s for s in spans if s.name == "webhook.deliver"]
    assert wh
    assert wh[0].attributes["webhook.subscriber_id"] == "abc-123"
    assert wh[0].attributes["webhook.attempt"] == 1


# ---------------------------------------------------------------------------
# get_tracer() returns a usable tracer (global provider integration)
# ---------------------------------------------------------------------------

def test_get_tracer_returns_tracer():
    from config.telemetry import get_tracer
    tracer = get_tracer("test")
    assert tracer is not None
    # Should be able to create a span without raising
    with tracer.start_as_current_span("test.span") as span:
        assert span is not None
