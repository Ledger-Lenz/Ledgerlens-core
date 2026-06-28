from datetime import datetime, timezone

import httpx
import pytest

import config.settings as settings_module
from ingestion.data_models import OrderBookEvent
from ingestion.operations_loader import _parse_event, load_order_book_events_for_pair


def test_load_order_book_events_for_pair_maps_offer_operations(monkeypatch):
    old_horizon_url = settings_module.settings.horizon_url
    object.__setattr__(settings_module.settings, "horizon_url", "https://horizon.example.test")
    records = [
        {
            "id": "1",
            "type": "manage_sell_offer",
            "created_at": "2026-06-12T00:00:00Z",
            "source_account": "A",
            "selling_asset_type": "native",
            "buying_asset_code": "USDC",
            "buying_asset_issuer": "ISSUER",
            "amount": "0",
            "offer_id": "123",
            "price": "0.1",
        },
        {
            "id": "2",
            "type": "manage_sell_offer",
            "created_at": "2026-06-12T00:01:00Z",
            "source_account": "B",
            "selling_asset_type": "native",
            "buying_asset_code": "USDC",
            "buying_asset_issuer": "ISSUER",
            "amount": "10",
            "offer_id": "456",
            "price": "0.1",
        },
        {
            "id": "3",
            "type": "manage_buy_offer",
            "created_at": "2026-06-12T00:02:00Z",
            "source_account": "C",
            "selling_asset_code": "USDC",
            "selling_asset_issuer": "ISSUER",
            "buying_asset_type": "native",
            "amount": "5",
            "offer_id": "0",
            "price": {"n": "1", "d": "10"},
        },
        {
            "id": "4",
            "type": "create_passive_sell_offer",
            "created_at": "2026-06-12T00:03:00Z",
            "source_account": "D",
            "selling_asset_type": "native",
            "buying_asset_code": "USDC",
            "buying_asset_issuer": "ISSUER",
            "amount": "7",
            "offer_id": "0",
            "price": "0.1",
        },
        {
            "id": "5",
            "type": "payment",
            "created_at": "2026-06-12T00:04:00Z",
            "source_account": "E",
            "amount": "1",
        },
    ]

    def handler(request):
        assert request.url.path == "/operations"
        assert request.url.params["order"] == "desc"
        return httpx.Response(200, json={"_embedded": {"records": records}})

    monkeypatch.setattr(
        "ingestion.operations_loader.get_with_retry",
        lambda client_arg, url, params: handler(httpx.Request("GET", url, params=params)),
    )

    try:
        events = load_order_book_events_for_pair(
            None,
            "USDC:ISSUER",
            datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

        assert all(isinstance(event, OrderBookEvent) for event in events)
        assert [event.event_type for event in events] == [
        "cancelled",
        "updated",
        "created",
        "created",
    ]
        assert [event.side for event in events] == ["sell", "sell", "buy", "sell"]
    finally:
        object.__setattr__(settings_module.settings, "horizon_url", old_horizon_url)


def test_load_order_book_events_for_pair_filters_by_since_and_pair(monkeypatch):
    old_horizon_url = settings_module.settings.horizon_url
    object.__setattr__(settings_module.settings, "horizon_url", "https://horizon.example.test")
    records = [
        {
            "id": "old",
            "type": "manage_sell_offer",
            "created_at": "2026-05-01T00:00:00Z",
            "source_account": "A",
            "selling_asset_type": "native",
            "buying_asset_code": "USDC",
            "buying_asset_issuer": "ISSUER",
            "amount": "1",
            "offer_id": "0",
            "price": "0.1",
        },
        {
            "id": "other-pair",
            "type": "manage_sell_offer",
            "created_at": "2026-06-12T00:00:00Z",
            "source_account": "B",
            "selling_asset_type": "native",
            "buying_asset_code": "BTC",
            "buying_asset_issuer": "ISSUER",
            "amount": "1",
            "offer_id": "0",
            "price": "0.1",
        },
    ]

    def handler(request):
        return httpx.Response(200, json={"_embedded": {"records": records}})

    monkeypatch.setattr(
        "ingestion.operations_loader.get_with_retry",
        lambda client_arg, url, params: handler(httpx.Request("GET", url, params=params)),
    )

    try:
        events = load_order_book_events_for_pair(
            "XLM",
            "USDC:ISSUER",
            datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

        assert events == []
    finally:
        object.__setattr__(settings_module.settings, "horizon_url", old_horizon_url)


def test_parse_event_rejects_non_positive_offer_id():
    record = {
        "id": "1",
        "type": "manage_sell_offer",
        "created_at": "2026-06-12T00:00:00Z",
        "source_account": "GACCOUNT",
        "selling_asset_type": "native",
        "buying_asset_code": "USDC",
        "buying_asset_issuer": "GISSUER",
        "amount": "1",
        "offer_id": "-1",
        "price": "0.1",
    }
    with pytest.raises(ValueError, match="positive integer"):
        _parse_event(record)
