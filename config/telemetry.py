"""OpenTelemetry SDK initialisation with graceful degradation."""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger("ledgerlens.telemetry")

_tracer_provider: TracerProvider | None = None


def init_telemetry(service_name: str = "ledgerlens") -> None:
    """Initialise the OTel SDK.

    Uses the OTLP gRPC exporter when OTEL_EXPORTER_OTLP_ENDPOINT is set,
    falling back to ConsoleSpanExporter otherwise. If the OTLP endpoint is
    unreachable, a WARNING is logged and no exception is raised.
    """
    global _tracer_provider

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            kwargs: dict = {"endpoint": endpoint}

            cert = os.getenv("OTEL_EXPORTER_OTLP_CERTIFICATE")
            client_key = os.getenv("OTEL_EXPORTER_OTLP_CLIENT_KEY")
            client_cert = os.getenv("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE")
            if cert and client_key and client_cert:
                with open(cert, "rb") as f:
                    root_cert = f.read()
                with open(client_key, "rb") as f:
                    private_key = f.read()
                with open(client_cert, "rb") as f:
                    certificate_chain = f.read()
                import grpc

                credentials = grpc.ssl_channel_credentials(
                    root_certificates=root_cert,
                    private_key=private_key,
                    certificate_chain=certificate_chain,
                )
                kwargs["credentials"] = credentials
                kwargs["insecure"] = False
            else:
                kwargs["insecure"] = True

            exporter = OTLPSpanExporter(**kwargs)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("OTel tracing: OTLP exporter -> %s", endpoint)
        except Exception as exc:
            logger.warning("OTel OTLP exporter unavailable (%s); falling back to console", exc)
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("OTel tracing: ConsoleSpanExporter (set OTEL_EXPORTER_OTLP_ENDPOINT to use OTLP)")

    trace.set_tracer_provider(provider)
    _tracer_provider = provider


def get_tracer(name: str = "ledgerlens") -> trace.Tracer:
    """Return a tracer for the given instrumentation scope."""
    return trace.get_tracer(name)
