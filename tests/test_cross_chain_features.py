"""Tests for cross-chain feature computation in detection/feature_engineering.py."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from detection.cross_chain_linker import CrossChainLinker
from detection.feature_engineering import (
    CROSS_CHAIN_FEATURE_NAMES,
    FEATURE_NAMES,
    build_cross_chain_features,
    build_feature_vector,
)
from detection.storage import save_bridge_transfer
from ingestion.data_models import BridgeTransfer

EVM_WALLET = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _stellar_wallet() -> str:
    from stellar_sdk import Keypair
    return Keypair.random().public_key


def _minimal_trades(account: str = "A") -> pd.DataFrame:
    base = pd.Timestamp("2026-06-12T00:00:00Z")
    return pd.DataFrame([
        {
            "ledger_close_time": base,
            "base_account": account,
            "counter_account": "B",
            "base_amount": 100.0,
            "counter_amount": 50.0,
            "base_asset": {"code": "XLM", "issuer": None},
            "counter_asset": {"code": "USDC", "issuer": "GISSUER"},
            "trade_type": "orderbook",
        }
    ])


# ---------------------------------------------------------------------------
# (a) build_cross_chain_features() returns all 6 feature names
# ---------------------------------------------------------------------------


def test_build_cross_chain_features_returns_all_six_names(tmp_path):
    """build_cross_chain_features always returns a dict with all 6 feature keys."""
    db = str(tmp_path / "test.db")
    wallet = _stellar_wallet()

    save_bridge_transfer(BridgeTransfer(
        chain="ethereum",
        direction="evm_to_stellar",
        evm_wallet=EVM_WALLET,
        stellar_wallet=wallet,
        amount_usd=500.0,
        token="USDC",
        tx_hash_evm="0x" + "aa" * 32,
        tx_hash_stellar=None,
        timestamp=NOW,
    ), db_path=db)

    linker = CrossChainLinker(db_path=db)
    features = build_cross_chain_features(wallet, linker)

    assert set(features.keys()) == set(CROSS_CHAIN_FEATURE_NAMES)
    for name in CROSS_CHAIN_FEATURE_NAMES:
        assert isinstance(features[name], float), f"{name} should be float"


def test_cross_chain_feature_names_in_global_feature_names():
    """All 6 cross-chain feature names appear in the global FEATURE_NAMES list."""
    for name in CROSS_CHAIN_FEATURE_NAMES:
        assert name in FEATURE_NAMES, f"Missing from FEATURE_NAMES: {name}"


# ---------------------------------------------------------------------------
# (b) has_evm_link = 0 for wallet with no bridge transfers
# ---------------------------------------------------------------------------


def test_has_evm_link_zero_for_wallet_with_no_bridge_transfers(tmp_path):
    """has_evm_link = 0.0 when the wallet has no known EVM counterparts."""
    db = str(tmp_path / "test.db")
    linker = CrossChainLinker(db_path=db)
    features = build_cross_chain_features("GNOBRIDGES", linker)

    assert features["has_evm_link"] == 0.0
    assert features["evm_round_trip_frequency"] == 0.0
    assert features["evm_benford_mad_30d"] == 0.0
    assert features["bridge_volume_ratio"] == 0.0


def test_build_feature_vector_cross_chain_zeros_without_linker():
    """When no cross_chain_linker is provided, all cross-chain features are 0."""
    trades = _minimal_trades("A")
    trades["ledger_close_time"] = pd.to_datetime(trades["ledger_close_time"], utc=True)
    as_of = pd.Timestamp("2026-06-12T00:00:00Z")

    features = build_feature_vector(trades, "A", as_of)

    for name in CROSS_CHAIN_FEATURE_NAMES:
        assert features[name] == 0.0, f"{name} should be 0.0 without linker"


# ---------------------------------------------------------------------------
# (c) evm_round_trip_frequency = 1.0 for every bridge-out with matching bridge-in
# ---------------------------------------------------------------------------


def test_evm_round_trip_frequency_is_one_when_all_matched(tmp_path):
    """evm_round_trip_frequency = 1.0 when every outbound has an inbound within 24h."""
    db = str(tmp_path / "test.db")
    wallet = _stellar_wallet()

    # Outbound bridge
    save_bridge_transfer(BridgeTransfer(
        chain="ethereum",
        direction="evm_to_stellar",
        evm_wallet=EVM_WALLET,
        stellar_wallet=wallet,
        amount_usd=100.0,
        token="USDC",
        tx_hash_evm="0x" + "aa" * 32,
        tx_hash_stellar=None,
        timestamp=NOW,
    ), db_path=db)

    # Inbound bridge within 24h
    save_bridge_transfer(BridgeTransfer(
        chain="ethereum",
        direction="stellar_to_evm",
        evm_wallet=EVM_WALLET,
        stellar_wallet=wallet,
        amount_usd=98.0,
        token="USDC",
        tx_hash_evm="0x" + "bb" * 32,
        tx_hash_stellar=None,
        timestamp=NOW + timedelta(hours=6),
    ), db_path=db)

    linker = CrossChainLinker(db_path=db)
    features = build_cross_chain_features(wallet, linker)

    assert features["has_evm_link"] == 1.0
    assert features["evm_round_trip_frequency"] == pytest.approx(1.0)


def test_build_feature_vector_includes_cross_chain_features_with_linker(tmp_path):
    """build_feature_vector includes cross-chain features when linker is provided."""
    db = str(tmp_path / "test.db")
    wallet = _stellar_wallet()

    save_bridge_transfer(BridgeTransfer(
        chain="ethereum",
        direction="evm_to_stellar",
        evm_wallet=EVM_WALLET,
        stellar_wallet=wallet,
        amount_usd=100.0,
        token="USDC",
        tx_hash_evm="0x" + "cc" * 32,
        tx_hash_stellar=None,
        timestamp=NOW,
    ), db_path=db)

    trades = _minimal_trades(wallet)
    trades["ledger_close_time"] = pd.to_datetime(trades["ledger_close_time"], utc=True)
    as_of = pd.Timestamp("2026-06-20T12:00:00Z")

    linker = CrossChainLinker(db_path=db)
    features = build_feature_vector(trades, wallet, as_of, cross_chain_linker=linker)

    assert set(CROSS_CHAIN_FEATURE_NAMES).issubset(set(features.keys()))
    assert features["has_evm_link"] == 1.0
    assert set(features.keys()) == set(FEATURE_NAMES)
