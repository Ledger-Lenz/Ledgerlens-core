"""Tests for detection/cross_chain_linker.py."""

from datetime import datetime, timedelta, timezone

import pytest

from detection.cross_chain_linker import CrossChainLinker
from detection.cross_chain_correlator import CrossChainCorrelator
from detection.storage import save_bridge_transfer
from ingestion.data_models import BridgeTransfer

EVM_WALLET_A = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
EVM_WALLET_B = "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD"
NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _transfer(
    stellar_wallet: str,
    evm_wallet: str,
    direction: str = "evm_to_stellar",
    ts: datetime | None = None,
    db_path: str | None = None,
) -> BridgeTransfer:
    t = BridgeTransfer(
        chain="ethereum",
        direction=direction,
        evm_wallet=evm_wallet,
        stellar_wallet=stellar_wallet,
        amount_usd=100.0,
        token="USDC",
        tx_hash_evm="0x" + "aa" * 32,
        tx_hash_stellar=None,
        timestamp=ts or NOW,
    )
    if db_path:
        save_bridge_transfer(t, db_path=db_path)
    return t


# ---------------------------------------------------------------------------
# (a) link_wallets() returns EVM wallets from bridge_transfers table
# ---------------------------------------------------------------------------


def test_link_wallets_returns_linked_evm_wallets(tmp_path):
    """link_wallets returns the EVM wallets that bridged to/from the Stellar wallet."""
    from stellar_sdk import Keypair
    db = str(tmp_path / "test.db")
    stellar_kp = Keypair.random()
    stellar_wallet = stellar_kp.public_key

    _transfer(stellar_wallet, EVM_WALLET_A, db_path=db)
    _transfer(stellar_wallet, EVM_WALLET_B, db_path=db)

    linker = CrossChainLinker(db_path=db)
    result = linker.link_wallets(stellar_wallet)

    assert set(result) == {EVM_WALLET_A, EVM_WALLET_B}


def test_link_wallets_returns_empty_for_unknown_wallet(tmp_path):
    """No linked wallets for a Stellar address with no bridge transfers."""
    db = str(tmp_path / "test.db")
    linker = CrossChainLinker(db_path=db)
    result = linker.link_wallets("GNOBRIDGES")
    assert result == []


# ---------------------------------------------------------------------------
# (b) wallets with transfers > 90 days ago are not returned
# ---------------------------------------------------------------------------


def test_link_wallets_excludes_old_transfers(tmp_path):
    """Transfers older than 90 days are excluded from link_wallets."""
    from stellar_sdk import Keypair
    db = str(tmp_path / "test.db")
    stellar_kp = Keypair.random()
    stellar_wallet = stellar_kp.public_key

    old_ts = NOW - timedelta(days=91)
    recent_ts = NOW - timedelta(days=30)

    _transfer(stellar_wallet, EVM_WALLET_A, ts=old_ts, db_path=db)
    _transfer(stellar_wallet, EVM_WALLET_B, ts=recent_ts, db_path=db)

    linker = CrossChainLinker(db_path=db)
    result = linker.link_wallets(stellar_wallet, lookback_days=90)

    # EVM_WALLET_A is >90 days old and should be excluded
    assert EVM_WALLET_A not in result
    assert EVM_WALLET_B in result


def test_link_wallets_exact_cutoff_edge(tmp_path):
    """Transfer well within the 90-day cutoff is still returned."""
    from stellar_sdk import Keypair
    db = str(tmp_path / "test.db")
    stellar_kp = Keypair.random()
    stellar_wallet = stellar_kp.public_key

    # 85 days ago: clearly within the 90-day window regardless of clock skew
    borderline_ts = datetime.now(timezone.utc) - timedelta(days=85)
    _transfer(stellar_wallet, EVM_WALLET_A, ts=borderline_ts, db_path=db)

    linker = CrossChainLinker(db_path=db)
    result = linker.link_wallets(stellar_wallet, lookback_days=90)
    assert EVM_WALLET_A in result


# ---------------------------------------------------------------------------
# (c) get_evm_trade_pattern() returns correct round-trip frequency
# ---------------------------------------------------------------------------


def test_get_evm_trade_pattern_round_trip_frequency_all_matched(tmp_path):
    """round_trip_frequency = 1.0 when every outbound has a matching inbound."""
    from stellar_sdk import Keypair
    db = str(tmp_path / "test.db")
    stellar_kp = Keypair.random()
    stellar_wallet = stellar_kp.public_key

    # One outbound and one inbound for EVM_WALLET_A, within 24h
    t_out = NOW
    t_in = NOW + timedelta(hours=12)

    save_bridge_transfer(BridgeTransfer(
        chain="ethereum",
        direction="evm_to_stellar",
        evm_wallet=EVM_WALLET_A,
        stellar_wallet=stellar_wallet,
        amount_usd=100.0,
        token="USDC",
        tx_hash_evm="0x" + "aa" * 32,
        tx_hash_stellar=None,
        timestamp=t_out,
    ), db_path=db)

    save_bridge_transfer(BridgeTransfer(
        chain="ethereum",
        direction="stellar_to_evm",
        evm_wallet=EVM_WALLET_A,
        stellar_wallet=stellar_wallet,
        amount_usd=95.0,
        token="USDC",
        tx_hash_evm="0x" + "bb" * 32,
        tx_hash_stellar=None,
        timestamp=t_in,
    ), db_path=db)

    linker = CrossChainLinker(db_path=db)
    pattern = linker.get_evm_trade_pattern(
        evm_wallets=[EVM_WALLET_A],
        chain="ethereum",
        db_path=db,
    )
    assert pattern["round_trip_frequency"] == pytest.approx(1.0)


def test_get_evm_trade_pattern_round_trip_frequency_none_matched(tmp_path):
    """round_trip_frequency = 0.0 when no inbound transfers match any outbound."""
    from stellar_sdk import Keypair
    db = str(tmp_path / "test.db")
    stellar_kp = Keypair.random()
    stellar_wallet = stellar_kp.public_key

    # Outbound transfer, but no return
    save_bridge_transfer(BridgeTransfer(
        chain="ethereum",
        direction="evm_to_stellar",
        evm_wallet=EVM_WALLET_A,
        stellar_wallet=stellar_wallet,
        amount_usd=100.0,
        token="USDC",
        tx_hash_evm="0x" + "cc" * 32,
        tx_hash_stellar=None,
        timestamp=NOW,
    ), db_path=db)

    linker = CrossChainLinker(db_path=db)
    pattern = linker.get_evm_trade_pattern(
        evm_wallets=[EVM_WALLET_A],
        chain="ethereum",
        db_path=db,
    )
    assert pattern["round_trip_frequency"] == 0.0


def test_get_evm_trade_pattern_empty_wallets():
    """Empty wallet list returns all-zero statistics."""
    linker = CrossChainLinker()
    pattern = linker.get_evm_trade_pattern(evm_wallets=[], chain="ethereum")
    assert pattern["round_trip_frequency"] == 0.0
    assert pattern["total_evm_volume"] == 0.0
    assert pattern["unique_counterparties"] == 0


def test_get_evm_trade_pattern_volume_and_benford(tmp_path):
    """Volume and Benford MAD are computed from the provided evm_trades list."""
    db = str(tmp_path / "test.db")
    linker = CrossChainLinker(db_path=db)

    evm_trades = [
        {"wallet_address": EVM_WALLET_A, "amount_in": 100.0, "amount_out": 99.0, "counterparty": "0x111"},
        {"wallet_address": EVM_WALLET_A, "amount_in": 200.0, "amount_out": 198.0, "counterparty": "0x222"},
        {"wallet_address": EVM_WALLET_A, "amount_in": 150.0, "amount_out": 148.0, "counterparty": "0x111"},
    ]

    pattern = linker.get_evm_trade_pattern(
        evm_wallets=[EVM_WALLET_A],
        chain="ethereum",
        evm_trades=evm_trades,
        db_path=db,
    )
    assert pattern["total_evm_volume"] == pytest.approx(450.0)
    assert pattern["unique_counterparties"] == 2
    assert isinstance(pattern["benford_mad"], float)


# ---------------------------------------------------------------------------
# CrossChainCorrelator tests (issue #182)
# ---------------------------------------------------------------------------


def _bridge_transfer(
    stellar: str,
    evm: str,
    direction: str,
    ts: datetime,
    amount: float = 100.0,
) -> BridgeTransfer:
    return BridgeTransfer(
        chain="ethereum",
        direction=direction,
        evm_wallet=evm,
        stellar_wallet=stellar,
        amount_usd=amount,
        token="USDC",
        tx_hash_evm="0x" + "bb" * 32,
        tx_hash_stellar=None,
        timestamp=ts,
    )


def test_correlator_empty_transfers_returns_zero():
    correlator = CrossChainCorrelator()
    assert correlator.compute_round_trip_score("GABCD", []) == 0.0


def test_correlator_single_transfer_returns_zero():
    correlator = CrossChainCorrelator()
    transfers = [_bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", NOW)]
    assert correlator.compute_round_trip_score("GABCD", transfers) == 0.0


def test_correlator_no_matching_direction_returns_zero():
    correlator = CrossChainCorrelator()
    transfers = [
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", NOW),
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", NOW),
    ]
    assert correlator.compute_round_trip_score("GABCD", transfers) == 0.0


def test_correlator_round_trip_within_window_scores_positive():
    correlator = CrossChainCorrelator(window_hours=24)
    t0 = NOW
    t1 = t0 + timedelta(hours=2)
    transfers = [
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", t0, 100.0),
        _bridge_transfer("GABCD", EVM_WALLET_A, "evm_to_stellar", t1, 100.0),
    ]
    score = correlator.compute_round_trip_score("GABCD", transfers)
    assert score > 0.0
    assert score <= 1.0


def test_correlator_round_trip_outside_window_returns_zero():
    correlator = CrossChainCorrelator(window_hours=1)
    t0 = NOW
    t1 = t0 + timedelta(hours=2)
    transfers = [
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", t0, 100.0),
        _bridge_transfer("GABCD", EVM_WALLET_A, "evm_to_stellar", t1, 100.0),
    ]
    assert correlator.compute_round_trip_score("GABCD", transfers) == 0.0


def test_correlator_amount_mismatch_returns_zero():
    correlator = CrossChainCorrelator(amount_tolerance=0.05)
    t0 = NOW
    t1 = t0 + timedelta(hours=1)
    transfers = [
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", t0, 100.0),
        _bridge_transfer("GABCD", EVM_WALLET_A, "evm_to_stellar", t1, 50.0),
    ]
    assert correlator.compute_round_trip_score("GABCD", transfers) == 0.0


def test_correlator_multiple_round_trips():
    correlator = CrossChainCorrelator(window_hours=24)
    transfers = [
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", NOW, 100.0),
        _bridge_transfer("GABCD", EVM_WALLET_A, "evm_to_stellar", NOW + timedelta(hours=1), 100.0),
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", NOW + timedelta(hours=2), 200.0),
        _bridge_transfer("GABCD", EVM_WALLET_A, "evm_to_stellar", NOW + timedelta(hours=3), 200.0),
    ]
    score = correlator.compute_round_trip_score("GABCD", transfers)
    assert 0.0 < score <= 1.0


def test_correlator_different_evm_wallets_no_match():
    correlator = CrossChainCorrelator(window_hours=24)
    transfers = [
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", NOW, 100.0),
        _bridge_transfer("GABCD", EVM_WALLET_B, "evm_to_stellar", NOW + timedelta(hours=1), 100.0),
    ]
    assert correlator.compute_round_trip_score("GABCD", transfers) == 0.0


def test_correlator_inbound_before_outbound_returns_zero():
    correlator = CrossChainCorrelator(window_hours=24)
    t0 = NOW
    t1 = t0 - timedelta(hours=1)
    transfers = [
        _bridge_transfer("GABCD", EVM_WALLET_A, "stellar_to_evm", t0, 100.0),
        _bridge_transfer("GABCD", EVM_WALLET_A, "evm_to_stellar", t1, 100.0),
    ]
    assert correlator.compute_round_trip_score("GABCD", transfers) == 0.0
