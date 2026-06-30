"""Prometheus metrics for the LedgerLens ingestion pipeline.

This module owns **all** prometheus_client metric objects used by the
ingestion layer.  Import :func:`get_metrics` (or use
``IngestionMetricsCollector.instance()``) from any ingestion module to
obtain the singleton collector.

Design principles
-----------------
- **Single registry**: all metrics live on the default Prometheus
  ``REGISTRY``, so ``generate_latest()`` returns them without extra
  configuration.
- **No PII in labels**: label values must only contain structural data
  (endpoint paths, status codes, error classes).  Wallet addresses,
  transaction hashes, and API keys must never appear as label values — they
  would create unbounded cardinality and risk leaking sensitive data.
- **Lazy import guard**: the module guards ``from prometheus_client import …``
  so that environments with ``METRICS_ENABLED=False`` in ``config.settings``
  can still load the package without the library being installed (a
  ``ImportError`` from ``prometheus_client`` is caught and a no-op collector
  is returned instead).
- **Endpoint normalisation**: :func:`_normalise_endpoint` strips query
  parameters and replaces dynamic path segments (Stellar addresses, numeric
  IDs) with stable placeholders *before* the value is used as a Prometheus
  label.  Without this, every unique wallet address in a Horizon URL would
  create a distinct label combination, exhausting Prometheus memory.

Usage example::

    from ingestion.metrics import get_metrics

    metrics = get_metrics()
    metrics.events_received_total.labels(source="horizon_sse").inc()
    metrics.http_request_duration_seconds.labels(endpoint="/trades").observe(0.12)
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger("ledgerlens.ingestion.metrics")

# ---------------------------------------------------------------------------
# Endpoint normalisation
# ---------------------------------------------------------------------------

# Stellar account IDs: 56 chars, start with G, base-32 alphabet (A-Z, 2-7).
# We match these greedily in URL paths so a wallet in a path segment like
# /accounts/GABC.../transactions becomes /accounts/{account_id}/transactions.
_STELLAR_ADDR_RE = re.compile(r"/G[A-Z2-7]{55}(?=/|$)")

# Numeric IDs (10+ digits) are Horizon paging tokens / ledger sequence numbers.
_NUMERIC_ID_RE = re.compile(r"/\d{10,}(?=/|$)")

# Short hex strings used as tx / offer / liquidity-pool IDs (32–64 hex chars).
_HEX_ID_RE = re.compile(r"/[0-9a-fA-F]{32,64}(?=/|$)")


def _normalise_endpoint(url: str) -> str:
    """Return a Prometheus-safe label value for a Horizon request URL.

    Strips the query string and replaces dynamic path segments with stable
    placeholders so that high-cardinality values (wallet addresses, paging
    tokens, transaction hashes) never appear as Prometheus label values.

    Replacements applied in order:

    1. Query string stripped (``?cursor=…`` etc.)
    2. Stellar G-addresses → ``{account_id}``
    3. Long numeric IDs (10+ digits) → ``{id}``
    4. Hex IDs (32–64 hex chars) → ``{id}``

    Examples::

        >>> _normalise_endpoint("https://horizon.stellar.org/accounts/GABC...XYZ/transactions?limit=200")
        '/accounts/{account_id}/transactions'
        >>> _normalise_endpoint("https://horizon.stellar.org/trades?cursor=12345678901234-0")
        '/trades'
        >>> _normalise_endpoint("/ledger/0005432100/transactions")
        '/ledger/{id}/transactions'

    Args:
        url: Full URL or path string from an outgoing HTTP request.

    Returns:
        Normalised path string safe for use as a Prometheus label value.
    """
    try:
        path = urlparse(url).path or url
    except Exception:
        path = url

    path = _STELLAR_ADDR_RE.sub("/{account_id}", path)
    path = _NUMERIC_ID_RE.sub("/{id}", path)
    path = _HEX_ID_RE.sub("/{id}", path)
    return path or "/"


# ---------------------------------------------------------------------------
# No-op fallback collector
# ---------------------------------------------------------------------------


class _NoOpMetric:
    """Silent no-op stand-in for a prometheus_client metric object.

    Used when ``METRICS_ENABLED=False`` or when ``prometheus_client`` is not
    installed.  All metric operations (``inc``, ``set``, ``observe``,
    ``labels``) are accepted and silently ignored so instrumented code does
    not need conditional logic around every metric call.
    """

    def labels(self, **_kwargs) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def dec(self, amount: float = 1) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, value: float) -> None:
        pass


class _NoOpCollector:
    """No-op :class:`IngestionMetricsCollector` returned when metrics are disabled."""

    events_received_total = _NoOpMetric()
    events_queued_total = _NoOpMetric()
    events_dropped_total = _NoOpMetric()
    sse_reconnects_total = _NoOpMetric()
    queue_depth = _NoOpMetric()
    queue_depth_peak = _NoOpMetric()
    http_requests_total = _NoOpMetric()
    http_request_duration_seconds = _NoOpMetric()
    http_rate_limit_hits_total = _NoOpMetric()
    http_retries_total = _NoOpMetric()
    ledger_close_to_score_seconds = _NoOpMetric()
    dlq_entries_total = _NoOpMetric()
    dlq_depth = _NoOpMetric()


# ---------------------------------------------------------------------------
# IngestionMetricsCollector
# ---------------------------------------------------------------------------


class IngestionMetricsCollector:
    """Singleton owner of all Prometheus metric objects for the ingestion layer.

    Obtain the singleton via :meth:`instance` or the module-level helper
    :func:`get_metrics`.  Never instantiate directly — doing so would attempt
    to register duplicate metric names with the default Prometheus registry and
    raise a ``ValueError``.

    Metric groups
    -------------
    **Streamer** (``horizon_streamer.py``)
      - ``ledgerlens_ingestion_events_received_total`` — every SSE trade event
        received from Horizon, labelled by ``source``.
      - ``ledgerlens_ingestion_events_queued_total`` — events successfully
        placed on the bounded queue.
      - ``ledgerlens_ingestion_events_dropped_total`` — events discarded due
        to queue overflow, labelled by ``source`` and ``reason``.
      - ``ledgerlens_ingestion_sse_reconnects_total`` — SSE reconnection count.
      - ``ledgerlens_ingestion_queue_depth`` — instantaneous queue size.
      - ``ledgerlens_ingestion_queue_depth_peak`` — high-water mark since last
        reset.

    **HTTP client** (``http_client.py``)
      - ``ledgerlens_http_requests_total`` — every outgoing Horizon request,
        labelled by normalised ``endpoint``, ``method``, and ``status_code``.
      - ``ledgerlens_http_request_duration_seconds`` — request latency
        histogram with pre-configured buckets.
      - ``ledgerlens_http_rate_limit_hits_total`` — HTTP 429 responses.
      - ``ledgerlens_http_retries_total`` — retry attempts labelled by
        ``reason`` (``"5xx"``, ``"429"``, ``"timeout"``).

    **Pipeline latency**
      - ``ledgerlens_ledger_close_to_score_seconds`` — end-to-end latency from
        Horizon ledger close timestamp to ``RiskScore`` written to SQLite.

    **Dead-letter queue**
      - ``ledgerlens_dlq_entries_total`` — records sent to the DLQ.
      - ``ledgerlens_dlq_depth`` — current DLQ depth gauge.

    Security
    --------
    Label values must never include wallet addresses, transaction hashes, or
    API keys.  Use :func:`_normalise_endpoint` for all URL-derived labels.
    """

    _instance: "IngestionMetricsCollector | None" = None

    def __init__(self) -> None:
        # Late import so that environments without prometheus_client (or with
        # METRICS_ENABLED=False) can import this module without error.
        from prometheus_client import Counter, Gauge, Histogram  # noqa: PLC0415

        # ── Streamer metrics ──────────────────────────────────────────────
        self.events_received_total = Counter(
            "ledgerlens_ingestion_events_received_total",
            "Total trade events received from Horizon SSE",
            ["source"],
        )
        self.events_queued_total = Counter(
            "ledgerlens_ingestion_events_queued_total",
            "Total trade events successfully queued for processing",
            ["source"],
        )
        self.events_dropped_total = Counter(
            "ledgerlens_ingestion_events_dropped_total",
            "Total trade events dropped due to queue overflow",
            ["source", "reason"],
        )
        self.sse_reconnects_total = Counter(
            "ledgerlens_ingestion_sse_reconnects_total",
            "Total SSE stream reconnections",
        )
        self.queue_depth = Gauge(
            "ledgerlens_ingestion_queue_depth",
            "Current trade queue depth",
            ["source"],
        )
        self.queue_depth_peak = Gauge(
            "ledgerlens_ingestion_queue_depth_peak",
            "Peak queue depth since last reset",
            ["source"],
        )

        # ── HTTP client metrics ───────────────────────────────────────────
        self.http_requests_total = Counter(
            "ledgerlens_http_requests_total",
            "Total HTTP requests to Horizon",
            ["endpoint", "method", "status_code"],
        )
        self.http_request_duration_seconds = Histogram(
            "ledgerlens_http_request_duration_seconds",
            "HTTP request latency in seconds",
            ["endpoint"],
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
        )
        self.http_rate_limit_hits_total = Counter(
            "ledgerlens_http_rate_limit_hits_total",
            "Total HTTP 429 responses received from Horizon",
        )
        self.http_retries_total = Counter(
            "ledgerlens_http_retries_total",
            "Total retry attempts for failed HTTP requests",
            ["reason"],
        )

        # ── Pipeline latency ──────────────────────────────────────────────
        self.ledger_close_to_score_seconds = Histogram(
            "ledgerlens_ledger_close_to_score_seconds",
            "Time from Horizon ledger close to RiskScore written",
            buckets=[1, 5, 10, 30, 60, 120, 300],
        )

        # ── Dead-letter queue ─────────────────────────────────────────────
        self.dlq_entries_total = Counter(
            "ledgerlens_dlq_entries_total",
            "Total records sent to the dead-letter queue",
            ["error_class"],
        )
        self.dlq_depth = Gauge(
            "ledgerlens_dlq_depth",
            "Current number of pending DLQ entries",
        )

    @classmethod
    def instance(cls) -> "IngestionMetricsCollector":
        """Return the module-level singleton, creating it on first call.

        Thread-safe for typical single-process usage. In tests that share
        the Prometheus default registry across test cases, call
        :func:`reset_for_testing` to clear the singleton between tests.
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_testing(cls) -> None:
        """Destroy the singleton so the next :meth:`instance` call re-creates it.

        Only for use in test suites that need a fresh registry per test.
        Production code must never call this.
        """
        cls._instance = None


def get_metrics() -> "IngestionMetricsCollector | _NoOpCollector":
    """Return the active metrics collector.

    When ``config.settings.metrics_enabled`` is ``False``, or when
    ``prometheus_client`` is not installed, returns a :class:`_NoOpCollector`
    so instrumented code paths are never forced to add ``if metrics_enabled``
    guards around every ``metrics.foo.inc()`` call.

    Returns:
        :class:`IngestionMetricsCollector` when metrics are enabled and
        ``prometheus_client`` is available, otherwise :class:`_NoOpCollector`.
    """
    try:
        from config.settings import settings  # noqa: PLC0415

        if not getattr(settings, "metrics_enabled", True):
            return _NoOpCollector()
    except Exception:
        pass

    try:
        return IngestionMetricsCollector.instance()
    except ImportError:
        logger.warning(
            "prometheus_client is not installed; metrics collection disabled. "
            "Install with: pip install prometheus-client==0.20.0"
        )
        return _NoOpCollector()
    except Exception as exc:
        logger.warning("Failed to initialise metrics collector: %s", exc)
        return _NoOpCollector()
