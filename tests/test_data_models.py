"""Pydantic v2 contract tests for ingestion records."""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ingestion.data_models import (
    Asset,
    OrderBookEvent,
    PathPaymentOperation,
    Trade,
    TradeType,
)
from ingestion.horizon_streamer import _parse_trade

FIXTURE = Path(__file__).parent / "fixtures" / "horizon_trade.json"


def _trade_payload() -> dict:
    return {
        "id": "1",
        "paging_token": "1-0",
        "ledger_close_time": "2026-06-25T12:30:00Z",
        "base_account": "GBASE",
        "counter_account": "GCOUNTER",
        "base_asset": {"code": "XLM"},
        "counter_asset": {"code": "USDC", "issuer": "GISSUER"},
        "base_amount": "1.5e-7",
        "counter_amount": "3e-7",
        "price": "2",
        "base_is_seller": True,
        "future_field": "ignored",
    }


def test_asset_native_and_credit_consistency():
    assert Asset(code="XLM").is_native
    assert not Asset(code="USDC", issuer="GISSUER").is_native
    with pytest.raises(ValidationError, match="require an issuer"):
        Asset(code="USDC")


@pytest.mark.parametrize("field", ["id", "paging_token", "base_account", "counter_account"])
def test_trade_security_identifiers_are_strict_strings(field):
    payload = _trade_payload()
    payload[field] = 123
    with pytest.raises(ValidationError):
        Trade.model_validate(payload)


def test_trade_missing_required_field_is_rejected():
    payload = _trade_payload()
    del payload["base_asset"]
    with pytest.raises(ValidationError):
        Trade.model_validate(payload)


@pytest.mark.parametrize("field", ["base_amount", "counter_amount", "price"])
@pytest.mark.parametrize("value", [0, -1, True, "nan", "inf", "not-a-number"])
def test_trade_numeric_constraints(field, value):
    payload = _trade_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        Trade.model_validate(payload)


def test_trade_coercion_datetime_variants_and_extra_fields():
    payload = _trade_payload()
    trade = Trade.model_validate(payload)
    assert trade.base_amount == pytest.approx(1.5e-7)
    assert trade.ledger_close_time == datetime(2026, 6, 25, 12, 30, tzinfo=timezone.utc)
    assert "future_field" not in trade.model_dump()

    payload["ledger_close_time"] = "2026-06-25T13:30:00+01:00"
    offset_trade = Trade.model_validate(payload)
    assert offset_trade.ledger_close_time.astimezone(timezone.utc) == trade.ledger_close_time


def test_trade_optional_none_and_missing_fields_are_equivalent():
    payload = _trade_payload()
    missing = Trade.model_validate(payload)
    payload["transaction_hash"] = None
    explicit_none = Trade.model_validate(payload)
    assert missing.transaction_hash is explicit_none.transaction_hash is None
    assert "transaction_hash" not in explicit_none.model_dump(exclude_none=True)


def test_trade_model_dump_json_round_trip_preserves_shape():
    trade = Trade.model_validate(_trade_payload())
    dumped = trade.model_dump()
    restored = Trade.model_validate_json(trade.model_dump_json())
    assert restored == trade
    assert set(dumped) == set(Trade.model_fields)


def test_liquidity_pool_trade_requires_pool_id():
    payload = _trade_payload()
    payload["trade_type"] = TradeType.LIQUIDITY_POOL
    payload["counter_account"] = None
    with pytest.raises(ValidationError, match="liquidity_pool_id"):
        Trade.model_validate(payload)


def test_order_book_event_rules_and_offer_id_serialization():
    event = OrderBookEvent.model_validate(
        {
            "id": "10",
            "timestamp": "2026-06-25T12:30:00Z",
            "account": "GACCOUNT",
            "asset_pair": "XLM/USDC:GISSUER",
            "side": "sell",
            "amount": "0",
            "price": "0.25",
            "event_type": "cancelled",
            "offer_id": 42,
        }
    )
    assert event.offer_id == 42
    assert set(event.model_dump()) == {
        "id",
        "timestamp",
        "account",
        "asset_pair",
        "side",
        "amount",
        "price",
        "event_type",
    }
    with pytest.raises(ValidationError):
        OrderBookEvent.model_validate({**event.model_dump(), "offer_id": 0})
    with pytest.raises(ValidationError):
        OrderBookEvent.model_validate({**event.model_dump(), "side": "hold"})


def test_path_payment_decimal_validator_supports_scientific_notation():
    operation = PathPaymentOperation.model_validate(
        {
            "id": "1",
            "paging_token": "1",
            "transaction_hash": "tx",
            "ledger_close_time": "2026-06-25T12:30:00Z",
            "source_account": "GSOURCE",
            "destination_account": "GDESTINATION",
            "source_asset": {"code": "XLM"},
            "destination_asset": {"code": "USDC", "issuer": "GISSUER"},
            "source_amount": "1.5e-7",
            "destination_amount": "3e-7",
            "path": [],
            "operation_type": "path_payment_strict_send",
        }
    )
    assert operation.source_amount == Decimal("1.5e-7")


def test_real_horizon_fixture_deserializes_through_loader():
    record = json.loads(FIXTURE.read_text())
    trade = _parse_trade(record)
    assert trade.id == "123456789"
    assert trade.paging_token == "123456789-0"
    assert trade.base_asset == Asset(code="XLM")
    assert trade.counter_asset == Asset(code="USDC", issuer="GISSUER")
    assert trade.base_amount == pytest.approx(1.5e-7)
    assert trade.price == pytest.approx(2.0)


@pytest.mark.benchmark
def test_trade_batch_deserialization_under_50ms():
    payload = _trade_payload()
    started = time.perf_counter()
    trades = [Trade.model_validate(payload) for _ in range(1_000)]
    elapsed = time.perf_counter() - started
    assert len(trades) == 1_000
    assert elapsed < 0.05, f"1,000 Trade validations took {elapsed * 1_000:.1f} ms"
