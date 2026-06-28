"""Structured JSON logging configuration using structlog."""

from __future__ import annotations

import logging
import logging.config

import structlog

from config.correlation import get_correlation_id


def _add_correlation_id(logger, method_name, event_dict):  # noqa: ANN001
    event_dict["correlation_id"] = get_correlation_id()
    return event_dict


def _add_otel_trace_id(logger, method_name, event_dict):  # noqa: ANN001
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
        else:
            event_dict["trace_id"] = "0" * 32
    except Exception:  # pragma: no cover
        event_dict["trace_id"] = "0" * 32
    return event_dict


def configure_logging(service_name: str = "ledgerlens", log_level: str = "INFO") -> None:
    """Configure structlog-based JSON logging for the whole process.

    Call this once at process startup (top of run_pipeline.py, cli.py,
    and the FastAPI lifespan handler).
    """
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_correlation_id,
        _add_otel_trace_id,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        pass_foreign_args=True,
    )

    # Add service name via a filter
    class _ServiceFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
            record.service = service_name  # type: ignore[attr-defined]
            return True

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(_ServiceFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
