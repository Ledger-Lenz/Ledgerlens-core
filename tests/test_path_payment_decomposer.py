"""Tests for PathPaymentDecomposer, PathPaymentLoader, and path_payment_frequency."""

import time
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from ingestion.data_models import Asset, PathPaymentOperation, Trade, TradeEffect
from ingestion.path_payment_loader import PathPaymentDecomposer

NOW = datetime(2026, 6, 25, 0, 0, 0, tzinfo=timezone.utc)
XLM = Asset(code="XLM", issuer=None)
USDC = Asset(code="USDC", issuer="GABC")
BTC = Asset(code="BTC", issuer="GXYZ")
ETH = Asset(code="ETH", issuer="GUVW")

SRC = "GAAA"
DST = "GBBB"


def _op(path: list[Asset], op_id: str = "123456") -> PathPaymentOperation:
    return PathPaymentOperation(
        id=op_id,
        paging_token=op_id,
        transaction_hash="txhash",
        ledger_close_time=NOW,
        source_account=SRC,
        destination_account=DST,
        source_asset=XLM,
        destination_asset=USDC,
        source_amount=Decimal("100"),
        destination_amount=Decimal("95"),
        path=path,
        operation_type="path_payment_strict_send",
    )


def _effect(sold: Asset, sold_amt: str, bought: Asset, bought_amt: str) -> TradeEffect:
    return TradeEffect(
        id="eff",
        account=SRC,
        sold_asset_type="native" if sold.issuer is None else "credit_alphanum4",
        sold_asset_code=sold.code if sold.issuer is not None else None,
        sold_asset_issuer=sold.issuer,
        sold_amount=Decimal(sold_amt),
        bought_asset_type="native" if bought.issuer is None else "credit_alphanum4",
        bought_asset_code=bought.code if bought.issuer is not None else None,
        bought_asset_issuer=bought.issuer,
        bought_amount=Decimal(bought_amt),
    )


# ── Unit: PathPaymentDecomposer ──────────────────────────────────────────────

def test_two_hop_payment_produces_two_trades():
    op = _op(path=[BTC])  # XLM → BTC → USDC
    effects = [
        _effect(XLM, "100", BTC, "0.005"),
        _effect(BTC, "0.005", USDC, "95"),
    ]
    trades = PathPaymentDecomposer().decompose(op, effects)
    assert len(trades) == 2
    assert trades[0].base_asset == XLM
    assert trades[0].counter_asset == BTC
    assert trades[0].hop_index == 0
    assert trades[1].base_asset == BTC
    assert trades[1].counter_asset == USDC
    assert trades[1].hop_index == 1


def test_three_hop_payment_produces_three_trades():
    op = _op(path=[BTC, ETH])  # XLM → BTC → ETH → USDC
    effects = [
        _effect(XLM, "100", BTC, "0.005"),
        _effect(BTC, "0.005", ETH, "0.1"),
        _effect(ETH, "0.1", USDC, "95"),
    ]
    trades = PathPaymentDecomposer().decompose(op, effects)
    assert len(trades) == 3
    for i, t in enumerate(trades):
        assert t.hop_index == i
        assert t.path_payment_id == "123456"
        assert t.base_account == SRC
        assert t.counter_account == DST


def test_direct_payment_empty_path_produces_one_trade():
    op = _op(path=[])  # XLM → USDC directly
    effects = [_effect(XLM, "100", USDC, "95")]
    trades = PathPaymentDecomposer().decompose(op, effects)
    assert len(trades) == 1
    assert trades[0].hop_index == 0
    assert trades[0].base_asset == XLM
    assert trades[0].counter_asset == USDC


def test_effects_count_mismatch_returns_empty_with_warning(caplog):
    import logging
    op = _op(path=[BTC])  # expects 2 effects
    effects = [_effect(XLM, "100", BTC, "0.005")]  # only 1
    with caplog.at_level(logging.WARNING, logger="ledgerlens.path_payment_loader"):
        trades = PathPaymentDecomposer().decompose(op, effects)
    assert trades == []
    assert any("expected" in r.message for r in caplog.records)


def test_effects_asset_mismatch_returns_empty_with_warning(caplog):
    import logging
    op = _op(path=[BTC])
    effects = [
        _effect(ETH, "100", BTC, "0.005"),  # wrong sold_asset (ETH instead of XLM)
        _effect(BTC, "0.005", USDC, "95"),
    ]
    with caplog.at_level(logging.WARNING, logger="ledgerlens.path_payment_loader"):
        trades = PathPaymentDecomposer().decompose(op, effects)
    assert trades == []


def test_non_positive_amount_returns_empty(caplog):
    import logging
    op = _op(path=[])
    effects = [_effect(XLM, "0", USDC, "95")]
    with caplog.at_level(logging.WARNING, logger="ledgerlens.path_payment_loader"):
        trades = PathPaymentDecomposer().decompose(op, effects)
    assert trades == []


def test_amount_exceeds_bound_returns_empty(caplog):
    import logging
    op = _op(path=[])
    effects = [_effect(XLM, "1e16", USDC, "95")]
    with caplog.at_level(logging.WARNING, logger="ledgerlens.path_payment_loader"):
        trades = PathPaymentDecomposer().decompose(op, effects)
    assert trades == []


def test_no_effects_falls_back_to_approximate_decomposition():
    op = _op(path=[BTC])  # XLM → BTC → USDC
    trades = PathPaymentDecomposer().decompose(op, effects=[])
    assert len(trades) == 2
    assert trades[0].hop_index == 0
    assert trades[1].hop_index == 1


def test_path_payment_id_on_all_hops():
    op = _op(path=[BTC], op_id="999")
    effects = [
        _effect(XLM, "100", BTC, "0.005"),
        _effect(BTC, "0.005", USDC, "95"),
    ]
    trades = PathPaymentDecomposer().decompose(op, effects)
    assert all(t.path_payment_id == "999" for t in trades)


def test_hop_index_is_bounded():
    """hop_index must stay within [0, len(path)]."""
    op = _op(path=[BTC])
    effects = [
        _effect(XLM, "100", BTC, "0.005"),
        _effect(BTC, "0.005", USDC, "95"),
    ]
    trades = PathPaymentDecomposer().decompose(op, effects)
    for i, t in enumerate(trades):
        assert 0 <= t.hop_index <= len(op.path)


def test_repeated_asset_in_path():
    """A→B→A via USDC: XLM→USDC→XLM should produce 2 valid trades."""
    op_data = PathPaymentOperation(
        id="111",
        paging_token="111",
        transaction_hash="tx",
        ledger_close_time=NOW,
        source_account=SRC,
        destination_account=DST,
        source_asset=XLM,
        destination_asset=XLM,
        source_amount=Decimal("100"),
        destination_amount=Decimal("99"),
        path=[USDC],
        operation_type="path_payment_strict_send",
    )
    effects = [
        _effect(XLM, "100", USDC, "95"),
        _effect(USDC, "95", XLM, "99"),
    ]
    trades = PathPaymentDecomposer().decompose(op_data, effects)
    assert len(trades) == 2
    assert trades[0].counter_asset == USDC
    assert trades[1].counter_asset == XLM


# ── Unit: path_payment_frequency feature ─────────────────────────────────────

def test_path_payment_frequency_zero_when_no_path_payments():
    from detection.feature_engineering import path_payment_features
    from ingestion.data_models import PathPayment

    pp = [PathPayment(
        id="1", transaction_hash="tx", timestamp=NOW,
        source_account=SRC, destination_account=DST,
        source_asset=XLM, destination_asset=USDC,
        source_amount=100.0, destination_amount=95.0,
        path=[], strict_send=True,
    )]
    trades_df = pd.DataFrame({"path_payment_id": [None, None, None]})
    feats = path_payment_features(pp, SRC, trades_df)
    assert feats["path_payment_frequency"] == 0.0


def test_path_payment_frequency_one_when_all_path_payments():
    from detection.feature_engineering import path_payment_features
    from ingestion.data_models import PathPayment

    pp = [PathPayment(
        id="1", transaction_hash="tx", timestamp=NOW,
        source_account=SRC, destination_account=DST,
        source_asset=XLM, destination_asset=USDC,
        source_amount=100.0, destination_amount=95.0,
        path=[], strict_send=True,
    )]
    trades_df = pd.DataFrame({"path_payment_id": ["1", "1", "1"]})
    feats = path_payment_features(pp, SRC, trades_df)
    assert feats["path_payment_frequency"] == pytest.approx(1.0)


def test_path_payment_frequency_mixed():
    from detection.feature_engineering import path_payment_features
    from ingestion.data_models import PathPayment

    pp = [PathPayment(
        id="1", transaction_hash="tx", timestamp=NOW,
        source_account=SRC, destination_account=DST,
        source_asset=XLM, destination_asset=USDC,
        source_amount=100.0, destination_amount=95.0,
        path=[], strict_send=True,
    )]
    trades_df = pd.DataFrame({"path_payment_id": ["1", None, "1", None]})
    feats = path_payment_features(pp, SRC, trades_df)
    assert feats["path_payment_frequency"] == pytest.approx(0.5)


def test_path_payment_frequency_zero_when_no_path_payments_passed():
    from detection.feature_engineering import path_payment_features
    feats = path_payment_features(None, SRC)
    assert feats["path_payment_frequency"] == 0.0


# ── Integration: mock Horizon endpoints ──────────────────────────────────────

def test_loader_decomposes_3_hop_payment():
    """Mock Horizon /operations + /effects; assert 3 hop trades with correct indexes."""
    from unittest.mock import MagicMock, patch
    from ingestion.path_payment_loader import PathPaymentLoader

    op_record = {
        "id": "123456789",
        "paging_token": "123456789",
        "transaction_hash": "txhash",
        "created_at": "2026-06-25T00:00:00Z",
        "source_account": SRC,
        "from": SRC,
        "to": DST,
        "type": "path_payment_strict_send",
        "source_asset_type": "native",
        "asset_type": "credit_alphanum4",
        "asset_code": "USDC",
        "asset_issuer": "GABC",
        "source_amount": "100.0000000",
        "amount": "95.0000000",
        "path": [
            {"asset_type": "credit_alphanum4", "asset_code": "BTC", "asset_issuer": "GXYZ"},
            {"asset_type": "credit_alphanum4", "asset_code": "ETH", "asset_issuer": "GUVW"},
        ],
    }

    effects_records = [
        {"id": "e1", "type": "trade", "account": SRC,
         "sold_asset_type": "native", "sold_amount": "100.0000000",
         "bought_asset_type": "credit_alphanum4", "bought_asset_code": "BTC",
         "bought_asset_issuer": "GXYZ", "bought_amount": "0.0050000"},
        {"id": "e2", "type": "trade", "account": SRC,
         "sold_asset_type": "credit_alphanum4", "sold_asset_code": "BTC",
         "sold_asset_issuer": "GXYZ", "sold_amount": "0.0050000",
         "bought_asset_type": "credit_alphanum4", "bought_asset_code": "ETH",
         "bought_asset_issuer": "GUVW", "bought_amount": "0.1000000"},
        {"id": "e3", "type": "trade", "account": SRC,
         "sold_asset_type": "credit_alphanum4", "sold_asset_code": "ETH",
         "sold_asset_issuer": "GUVW", "sold_amount": "0.1000000",
         "bought_asset_type": "credit_alphanum4", "bought_asset_code": "USDC",
         "bought_asset_issuer": "GABC", "bought_amount": "95.0000000"},
    ]

    ops_mock = MagicMock()
    ops_mock.json.return_value = {"_embedded": {"records": [op_record]}}

    effects_mock = MagicMock()
    effects_mock.json.return_value = {"_embedded": {"records": effects_records}}

    def _fake_get_with_retry(client, url, params=None, **kwargs):
        if "/effects" in url:
            return effects_mock
        return ops_mock

    with patch("ingestion.path_payment_loader.get_with_retry", side_effect=_fake_get_with_retry):
        loader = PathPaymentLoader()
        trades = loader.load_hop_trades(SRC, since=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert len(trades) == 3
    assert [t.hop_index for t in trades] == [0, 1, 2]
    assert trades[0].base_asset.code == "XLM"
    assert trades[1].base_asset.code == "BTC"
    assert trades[2].base_asset.code == "ETH"
    assert trades[2].counter_asset.code == "USDC"
    assert all(t.path_payment_id == "123456789" for t in trades)


# ── Performance benchmark ─────────────────────────────────────────────────────

def test_decompose_1000_payments_with_3_hops_under_1_second():
    decomposer = PathPaymentDecomposer()
    ops = [_op(path=[BTC, ETH], op_id=str(i)) for i in range(1000)]
    effects_per_op = [
        _effect(XLM, "100", BTC, "0.005"),
        _effect(BTC, "0.005", ETH, "0.1"),
        _effect(ETH, "0.1", USDC, "95"),
    ]

    start = time.monotonic()
    for op in ops:
        decomposer.decompose(op, effects_per_op)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"1000 decompositions took {elapsed:.2f}s (limit 1s)"
