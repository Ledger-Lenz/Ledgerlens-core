"""Tests for detection/cross_chain_correlator.py."""

from datetime import datetime, timedelta, timezone

import pytest

from detection.cross_chain_correlator import (
    CrossChainCorrelator,
    _parse_timestamp,
)


NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _parse_timestamp
# ---------------------------------------------------------------------------

def test_parse_datetime_naive():
    """Naive datetimes are converted to UTC-aware."""
    dt = datetime(2026, 1, 1, 12, 0, 0)
    result = _parse_timestamp(dt)
    assert result.tzinfo == timezone.utc


def test_parse_datetime_aware():
    """Already-aware datetimes are returned as-is."""
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = _parse_timestamp(dt)
    assert result is dt  # same object, no copy needed


def test_parse_iso_string():
    result = _parse_timestamp("2026-01-01T12:00:00+00:00")
    assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_string_naive():
    """ISO string without timezone gets UTC."""
    result = _parse_timestamp("2026-01-01T12:00:00")
    assert result.tzinfo == timezone.utc
    assert result.hour == 12


def test_parse_invalid_string_raises():
    with pytest.raises(ValueError):
        _parse_timestamp("not-a-date")


def test_parse_wrong_type_raises():
    with pytest.raises(TypeError, match="timestamp must be datetime or str"):
        _parse_timestamp(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CrossChainCorrelator
# ---------------------------------------------------------------------------

def test_init_defaults():
    c = CrossChainCorrelator()
    assert c._window == timedelta(hours=24)
    assert c._amount_tolerance == 0.05


def test_init_custom():
    c = CrossChainCorrelator(window_hours=48, amount_tolerance=0.10)
    assert c._window == timedelta(hours=48)
    assert c._amount_tolerance == 0.10
