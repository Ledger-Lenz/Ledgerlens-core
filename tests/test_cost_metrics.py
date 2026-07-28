"""Unit tests for cost metric exporter (config/cost_exporter.py).

Verifies that cost coefficient gauges are correctly initialized from
config/settings.py at application startup.

Stability notes
---------------
- ``_initialized`` global in cost_exporter is reset before every test via
  the ``reset_cost_exporter`` autouse fixture so tests are fully independent
  of execution order.
- All Settings mutations go through ``monkeypatch`` so they are automatically
  rolled back after each test.
- Environment variable manipulation uses ``monkeypatch.setenv`` /
  ``monkeypatch.delenv`` — never raw ``os.environ`` — to guarantee restoration
  even on unexpected failure.
- The Prometheus ``REGISTRY`` retains gauge objects for the process lifetime;
  tests read current *values* rather than re-registering gauges, which avoids
  duplicate-registration errors.
"""

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

import config.cost_exporter as cost_exporter_module
from config import settings as settings_module
from config.cost_exporter import init_cost_metrics


# ---------------------------------------------------------------------------
# Shared fixture — reset module-level _initialized flag between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_cost_exporter():
    """Reset the cost_exporter ``_initialized`` flag before and after each test.

    Without this, the first test that calls ``init_cost_metrics()`` sets
    ``_initialized = True`` and every subsequent call is silently skipped,
    making tests order-dependent and masking real failures.
    """
    cost_exporter_module._initialized = False
    yield
    cost_exporter_module._initialized = False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_init_cost_metrics_sets_gauges_from_settings(monkeypatch):
    """Cost gauges are set to values from settings.py."""
    monkeypatch.setattr(settings_module.settings, "cost_per_vcpu_hour_usd", 0.123)
    monkeypatch.setattr(settings_module.settings, "cost_per_gb_memory_hour_usd", 0.456)
    monkeypatch.setattr(settings_module.settings, "cost_per_gb_storage_month_usd", 0.789)

    init_cost_metrics()

    vcpu_gauge = REGISTRY.get_sample_value("ledgerlens_cost_per_vcpu_hour_usd")
    memory_gauge = REGISTRY.get_sample_value("ledgerlens_cost_per_gb_memory_hour_usd")
    storage_gauge = REGISTRY.get_sample_value("ledgerlens_cost_per_gb_storage_month_usd")

    assert vcpu_gauge == pytest.approx(0.123), \
        f"Expected vCPU cost gauge = 0.123, got {vcpu_gauge}"
    assert memory_gauge == pytest.approx(0.456), \
        f"Expected memory cost gauge = 0.456, got {memory_gauge}"
    assert storage_gauge == pytest.approx(0.789), \
        f"Expected storage cost gauge = 0.789, got {storage_gauge}"


def test_init_cost_metrics_is_idempotent(monkeypatch):
    """Calling init_cost_metrics() multiple times is safe — second call is a no-op."""
    monkeypatch.setattr(settings_module.settings, "cost_per_vcpu_hour_usd", 0.050)

    init_cost_metrics()
    first_value = REGISTRY.get_sample_value("ledgerlens_cost_per_vcpu_hour_usd")

    # Mutate settings *after* the first init; the second call must be a no-op.
    monkeypatch.setattr(settings_module.settings, "cost_per_vcpu_hour_usd", 0.999)
    init_cost_metrics()
    second_value = REGISTRY.get_sample_value("ledgerlens_cost_per_vcpu_hour_usd")

    assert first_value == second_value, \
        "Repeated init_cost_metrics() calls must be no-ops; gauge must not change"


def test_cost_gauges_are_exposed_at_metrics_endpoint():
    """Cost coefficient gauges appear in the Prometheus text exposition output.

    Uses prometheus_client.generate_latest() directly — no HTTP server needed.
    The ``client`` fixture that was previously referenced here did not exist in
    conftest.py, making the test always error. This version calls the library
    function the metrics endpoint itself calls internally.
    """
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST  # noqa: F401

    init_cost_metrics()

    text = generate_latest().decode("utf-8")

    assert "ledgerlens_cost_per_vcpu_hour_usd" in text, \
        "vCPU cost gauge missing from Prometheus output"
    assert "ledgerlens_cost_per_gb_memory_hour_usd" in text, \
        "Memory cost gauge missing from Prometheus output"
    assert "ledgerlens_cost_per_gb_storage_month_usd" in text, \
        "Storage cost gauge missing from Prometheus output"


def test_cost_gauges_with_default_values():
    """Default cost values loaded from settings are non-negative and plausible."""
    init_cost_metrics()

    vcpu_cost = REGISTRY.get_sample_value("ledgerlens_cost_per_vcpu_hour_usd")
    memory_cost = REGISTRY.get_sample_value("ledgerlens_cost_per_gb_memory_hour_usd")
    storage_cost = REGISTRY.get_sample_value("ledgerlens_cost_per_gb_storage_month_usd")

    assert vcpu_cost is not None, "vCPU cost gauge not initialized"
    assert vcpu_cost >= 0, f"vCPU cost must be non-negative, got {vcpu_cost}"
    assert vcpu_cost < 1.0, f"vCPU cost suspiciously high: {vcpu_cost} (sanity check)"

    assert memory_cost is not None, "Memory cost gauge not initialized"
    assert memory_cost >= 0, f"Memory cost must be non-negative, got {memory_cost}"
    assert memory_cost < 1.0, f"Memory cost suspiciously high: {memory_cost} (sanity check)"

    assert storage_cost is not None, "Storage cost gauge not initialized"
    assert storage_cost >= 0, f"Storage cost must be non-negative, got {storage_cost}"
    assert storage_cost < 1.0, f"Storage cost suspiciously high: {storage_cost} (sanity check)"


def test_negative_cost_coefficient_rejected_at_settings_validation(monkeypatch):
    """Pydantic rejects negative cost coefficients when constructing a Settings instance.

    Uses monkeypatch.setenv so the environment variable is restored automatically
    after the test — no manual ``finally`` block required.
    """
    from config.settings import Settings

    monkeypatch.setenv("COST_PER_VCPU_HOUR_USD", "-0.01")

    with pytest.raises(ValidationError, match="Cost coefficients must be non-negative"):
        Settings()


def test_capacity_projection_window_validation(monkeypatch):
    """Capacity projection window must be >= 1 day."""
    from config.settings import Settings

    monkeypatch.setenv("CAPACITY_PROJECTION_WINDOW_DAYS", "0")

    with pytest.raises(ValidationError, match="Capacity projection days must be >= 1"):
        Settings()


def test_capacity_projection_lead_time_validation(monkeypatch):
    """Capacity projection lead time must be >= 1 day."""
    from config.settings import Settings

    monkeypatch.setenv("CAPACITY_PROJECTION_LEAD_TIME_DAYS", "-5")

    with pytest.raises(ValidationError, match="Capacity projection days must be >= 1"):
        Settings()
