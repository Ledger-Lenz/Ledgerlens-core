"""Fault-injection tests for out-of-order and late events in the rolling window.

Verifies the window-tolerance contract documented in
:mod:`detection.rolling_window`: out-of-order trades within tolerance are
late-merged in chronological order, trades beyond tolerance are rejected, and
both are counted by ``ledgerlens_rolling_window_late_events_total``.
"""

from datetime import datetime, timedelta, timezone

import pytest

from detection import rolling_window
from detection.rolling_window import WINDOW_TOLERANCE_HOURS, WalletWindow
from ingestion.data_models import Asset, Trade


def _trade(ts: datetime, idx: int) -> Trade:
    return Trade(
        id=f"t-{idx}",
        ledger_close_time=ts,
        base_account="GWALLET",
        counter_account="GCOUNTER",
        base_asset=Asset(code="XLM"),
        counter_asset=Asset(
            code="USDC", issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
        ),
        base_amount=100.0,
        counter_amount=200.0,
        price=2.0,
        base_is_seller=True,
    )


def _late_count(reason: str) -> float:
    counter = rolling_window._get_late_events_counter()
    if counter is None:
        pytest.skip("prometheus_client not installed")
    return counter.labels(reason=reason)._value.get()


def _ids(window: WalletWindow) -> list[str]:
    return [t.id for t in window.get(hours=WINDOW_TOLERANCE_HOURS)]


def test_out_of_order_within_tolerance_is_late_merged():
    now = datetime.now(timezone.utc)
    window = WalletWindow()
    before = _late_count("out_of_order")

    assert window.add(_trade(now - timedelta(minutes=10), 1))
    assert window.add(_trade(now - timedelta(minutes=1), 3))
    assert window.add(_trade(now - timedelta(minutes=5), 2))  # delayed delivery

    assert _ids(window) == ["t-1", "t-2", "t-3"]
    assert [t.id for t in window.get(hours=1)] == ["t-1", "t-2", "t-3"]
    assert _late_count("out_of_order") == before + 1


def test_shuffled_burst_ends_chronological():
    now = datetime.now(timezone.utc)
    window = WalletWindow()
    offsets = [3, 17, 1, 9, 12, 5, 20, 2]
    for i in offsets:
        assert window.add(_trade(now - timedelta(hours=i), i))

    times = [t.ledger_close_time for t in window.get(hours=WINDOW_TOLERANCE_HOURS)]
    assert times == sorted(times)
    assert len(times) == len(offsets)


def test_event_beyond_tolerance_is_rejected_and_counted():
    now = datetime.now(timezone.utc)
    window = WalletWindow()
    before = _late_count("beyond_window")

    assert window.add(_trade(now - timedelta(minutes=1), 1))
    assert not window.add(_trade(now - timedelta(hours=WINDOW_TOLERANCE_HOURS, minutes=5), 2))

    assert _ids(window) == ["t-1"]
    assert _late_count("beyond_window") == before + 1


def test_late_event_into_empty_window_is_rejected():
    window = WalletWindow()
    stale = datetime.now(timezone.utc) - timedelta(days=3)

    assert not window.add(_trade(stale, 1))
    assert window.to_dict()["trades"] == []


def test_in_order_events_are_not_counted_as_late():
    now = datetime.now(timezone.utc)
    window = WalletWindow()
    before = (_late_count("out_of_order"), _late_count("beyond_window"))

    for i in range(5):
        window.add(_trade(now - timedelta(minutes=5 - i), i))

    assert (_late_count("out_of_order"), _late_count("beyond_window")) == before
