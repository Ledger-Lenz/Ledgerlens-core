"""Tests for WashTradeSequenceModel, TradeSequenceEncoder, and w_seq fusion."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from detection.temporal_model import (  # noqa: E402
    TRADE_FEATURE_DIM,
    TradeSequenceEncoder,
    TransformerSequenceModel,
    WashTradeSequenceModel,
    build_sequence_model,
    load_sequence_model,
    save_sequence_model,
    trade_to_feature_vector,
)
from detection.model_inference import fuse_sequence_score  # noqa: E402
from ingestion.data_models import Asset, Trade, TradeType  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    ts: float,
    amount: float = 100.0,
    base_is_seller: bool = True,
    asset_pair: str = "XLM/USDC",
) -> Trade:
    base_asset, counter_asset = (
        Asset(code="XLM"),
        Asset(code="USDC", issuer="GABC"),
    )
    return Trade(
        id=str(ts),
        ledger_close_time=datetime.fromtimestamp(ts, tz=timezone.utc),
        base_account="GAAA",
        counter_account="GBBB",
        base_asset=base_asset,
        counter_asset=counter_asset,
        base_amount=amount,
        counter_amount=amount,
        price=1.0,
        base_is_seller=base_is_seller,
        trade_type=TradeType.ORDERBOOK,
    )


# ---------------------------------------------------------------------------
# trade_to_feature_vector unit tests
# ---------------------------------------------------------------------------


def test_trade_to_feature_vector_zero_amount():
    trade = _make_trade(ts=1_000_000, amount=0.0)
    vec = trade_to_feature_vector(trade, None, {})
    assert vec.shape == (TRADE_FEATURE_DIM,)
    assert vec[0] == pytest.approx(0.0)  # log1p(0) == 0


def test_trade_to_feature_vector_amount_100():
    trade = _make_trade(ts=1_000_000, amount=100.0)
    vec = trade_to_feature_vector(trade, None, {})
    assert vec[0] == pytest.approx(np.log1p(100.0), rel=1e-5)


def test_trade_to_feature_vector_direction():
    buyer_trade = _make_trade(ts=1_000_000, base_is_seller=False)
    seller_trade = _make_trade(ts=1_000_000, base_is_seller=True)
    vocab: dict = {}
    assert trade_to_feature_vector(buyer_trade, None, vocab)[1] == 1.0
    assert trade_to_feature_vector(seller_trade, None, vocab)[1] == 0.0


def test_trade_to_feature_vector_interarrival_no_prev():
    trade = _make_trade(ts=1_000_000)
    vec = trade_to_feature_vector(trade, None, {})
    assert vec[2] == pytest.approx(0.0)  # log1p(0) for no previous trade


def test_trade_to_feature_vector_identical_timestamps():
    """All trades with identical timestamps → log_interarrival = log1p(0) = 0."""
    t1 = _make_trade(ts=1_000_000)
    t2 = _make_trade(ts=1_000_000)
    vec = trade_to_feature_vector(t2, t1, {})
    assert vec[2] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TradeSequenceEncoder unit tests
# ---------------------------------------------------------------------------


def test_encode_batch_truncates_long_sequences():
    encoder = TradeSequenceEncoder(max_seq_len=200)
    # 300 trades at 1-second intervals
    trades = [_make_trade(ts=1_000_000 + i) for i in range(300)]
    seqs, lengths = encoder.encode_batch({"wallet_a": trades})
    assert seqs.shape == (1, 200, TRADE_FEATURE_DIM)
    assert lengths[0].item() == 200


def test_encode_batch_pads_short_sequences():
    encoder = TradeSequenceEncoder(max_seq_len=200)
    trades = [_make_trade(ts=1_000_000 + i) for i in range(10)]
    seqs, lengths = encoder.encode_batch({"wallet_a": trades})
    assert seqs.shape == (1, 200, TRADE_FEATURE_DIM)
    assert lengths[0].item() == 10
    # Padded positions should be zero
    assert (seqs[0, 10:] == 0).all()


def test_encode_batch_single_trade():
    """Wallet with 1 trade: length 1, padded to 200, valid tensor."""
    encoder = TradeSequenceEncoder(max_seq_len=200)
    trades = [_make_trade(ts=1_000_000)]
    seqs, lengths = encoder.encode_batch({"wallet_a": trades})
    assert seqs.shape == (1, 200, TRADE_FEATURE_DIM)
    assert lengths[0].item() == 1


def test_encode_batch_empty_trades():
    """Wallet with no trades: zero tensor, length 1 (pack_padded_sequence minimum)."""
    encoder = TradeSequenceEncoder(max_seq_len=200)
    seqs, lengths = encoder.encode_batch({"wallet_a": []})
    assert seqs.shape == (1, 200, TRADE_FEATURE_DIM)
    assert (seqs == 0).all()
    assert lengths[0].item() == 1


def test_encode_batch_multiple_wallets():
    encoder = TradeSequenceEncoder(max_seq_len=200)
    wallet_trades = {
        "w1": [_make_trade(ts=1_000_000 + i) for i in range(5)],
        "w2": [_make_trade(ts=2_000_000 + i) for i in range(50)],
    }
    seqs, lengths = encoder.encode_batch(wallet_trades)
    assert seqs.shape == (2, 200, TRADE_FEATURE_DIM)
    assert set(lengths.tolist()) == {5, 50}


# ---------------------------------------------------------------------------
# WashTradeSequenceModel forward pass
# ---------------------------------------------------------------------------


def test_lstm_forward_shape_and_range():
    """Input (batch=4, seq=200, feat=5) + tabular (4, 35) → output (4,) in [0,1]."""
    model = WashTradeSequenceModel(tabular_feature_dim=35)
    model.eval()
    seqs = torch.randn(4, 200, TRADE_FEATURE_DIM)
    lengths = torch.tensor([200, 150, 50, 1])
    tabular = torch.randn(4, 35)
    with torch.no_grad():
        out = model(seqs, lengths, tabular)
    assert out.shape == (4,)
    assert (out >= 0).all() and (out <= 1).all()


def test_lstm_varying_lengths_no_error():
    """pack_padded_sequence with varying lengths must not raise."""
    model = WashTradeSequenceModel(tabular_feature_dim=35)
    model.eval()
    seqs = torch.zeros(3, 200, TRADE_FEATURE_DIM)
    lengths = torch.tensor([1, 100, 200])
    tabular = torch.zeros(3, 35)
    model(seqs, lengths, tabular)  # should not raise


def test_transformer_forward_shape_and_range():
    model = TransformerSequenceModel(tabular_feature_dim=35)
    model.eval()
    seqs = torch.randn(4, 200, TRADE_FEATURE_DIM)
    lengths = torch.tensor([200, 150, 50, 1])
    tabular = torch.randn(4, 35)
    with torch.no_grad():
        out = model(seqs, lengths, tabular)
    assert out.shape == (4,)
    assert (out >= 0).all() and (out <= 1).all()


def test_build_sequence_model_factory():
    lstm_model = build_sequence_model("lstm")
    assert isinstance(lstm_model, WashTradeSequenceModel)
    tf_model = build_sequence_model("transformer")
    assert isinstance(tf_model, TransformerSequenceModel)


# ---------------------------------------------------------------------------
# Save / load round-trip
# ---------------------------------------------------------------------------


def test_save_load_sequence_model(tmp_path):
    model = WashTradeSequenceModel(tabular_feature_dim=35, lstm_hidden_dim=32)
    save_sequence_model(model, str(tmp_path))

    pt_file = tmp_path / "temporal_model.pt"
    assert pt_file.exists() and pt_file.stat().st_size > 0

    loaded = load_sequence_model(str(tmp_path), lstm_hidden_dim=32)
    assert loaded is not None

    seqs = torch.randn(2, 200, TRADE_FEATURE_DIM)
    lengths = torch.tensor([200, 100])
    tabular = torch.randn(2, 35)
    with torch.no_grad():
        out = loaded(seqs, lengths, tabular)
    assert out.shape == (2,)


def test_load_sequence_model_missing_returns_none(tmp_path):
    result = load_sequence_model(str(tmp_path))
    assert result is None


# ---------------------------------------------------------------------------
# fuse_sequence_score
# ---------------------------------------------------------------------------


def test_fuse_sequence_score_zero_weight():
    assert fuse_sequence_score(0.6, 0.9, w_seq=0.0) == pytest.approx(0.6)


def test_fuse_sequence_score_clamps_weight():
    # w_seq > 0.4 → clamped to 0.4
    result = fuse_sequence_score(0.5, 1.0, w_seq=0.9)
    assert result == pytest.approx(0.5 * 0.6 + 1.0 * 0.4)


def test_fuse_sequence_score_midpoint():
    result = fuse_sequence_score(0.4, 0.8, w_seq=0.2)
    assert result == pytest.approx(0.8 * 0.4 + 0.2 * 0.8)


def test_inference_fuse_sequence_score():
    """fuse_sequence_score imported from model_inference produces correct result."""
    result = fuse_sequence_score(0.5, 0.5, 0.3)
    assert result == pytest.approx(0.7 * 0.5 + 0.3 * 0.5)


# ---------------------------------------------------------------------------
# Integration: vocab persistence
# ---------------------------------------------------------------------------


def test_encoder_vocab_save_load(tmp_path):
    encoder = TradeSequenceEncoder(max_seq_len=10)
    trades = {"w1": [_make_trade(ts=1_000_000, asset_pair="XLM/USDC")]}
    encoder.encode_batch(trades)

    vocab_path = str(tmp_path / "vocab.json")
    encoder.save_vocab(vocab_path)

    loaded = TradeSequenceEncoder.load_vocab(vocab_path, max_seq_len=10)
    assert loaded.asset_pair_vocab == encoder.asset_pair_vocab
