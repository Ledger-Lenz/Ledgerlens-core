"""Cost coefficient exporter for Prometheus.

Exposes cost configuration as Prometheus gauges so recording rules can
reference them without hardcoding values. The gauges are set once at
startup from config/settings.py and remain static unless the process
is restarted with new environment variables.

Usage:
    from config.cost_exporter import init_cost_metrics
    init_cost_metrics()

This registers three gauges:
- ledgerlens_cost_per_vcpu_hour_usd
- ledgerlens_cost_per_gb_memory_hour_usd  
- ledgerlens_cost_per_gb_storage_month_usd

which are scraped by Prometheus at GET /metrics alongside other metrics.
"""

from prometheus_client import Gauge

from config.settings import settings

# Cost coefficient gauges (static values set at startup)
_cost_per_vcpu_hour_gauge = Gauge(
    "ledgerlens_cost_per_vcpu_hour_usd",
    "Cost per vCPU-hour in USD (operator-configurable coefficient)",
)

_cost_per_gb_memory_hour_gauge = Gauge(
    "ledgerlens_cost_per_gb_memory_hour_usd",
    "Cost per GB memory-hour in USD (operator-configurable coefficient)",
)

_cost_per_gb_storage_month_gauge = Gauge(
    "ledgerlens_cost_per_gb_storage_month_usd",
    "Cost per GB storage per month in USD (operator-configurable coefficient)",
)

_initialized = False


def init_cost_metrics() -> None:
    """Set cost coefficient gauges from config/settings.py.
    
    This should be called once at application startup (e.g., in api/main.py
    startup event or run_pipeline.py main). Subsequent calls are no-ops.
    
    Raises:
        ValueError: if settings validation failed (cost coefficients are negative)
    """
    global _initialized
    if _initialized:
        return
    
    # Set gauges from validated settings
    _cost_per_vcpu_hour_gauge.set(settings.cost_per_vcpu_hour_usd)
    _cost_per_gb_memory_hour_gauge.set(settings.cost_per_gb_memory_hour_usd)
    _cost_per_gb_storage_month_gauge.set(settings.cost_per_gb_storage_month_usd)
    
    _initialized = True
