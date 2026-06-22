"""Tests for EVM address validation (checksum, malformed rejection, config validation)."""

import pytest
from web3 import Web3


# ---------------------------------------------------------------------------
# (a) Non-checksummed EVM address is converted on ingest
# ---------------------------------------------------------------------------


def test_non_checksummed_address_converted_on_ingest():
    """EVMTradeLoader.pool_addresses converts lowercase addresses to checksum form."""
    from ingestion.evm_loader import EVMTradeLoader

    lowercase = "0xab5801a7d398351b8be11c439e05c5b3259aec9b"
    checksummed = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"

    loader = EVMTradeLoader(
        chain="ethereum",
        rpc_url="https://mock.example.com",
        pool_addresses=[lowercase],
    )
    assert loader.pool_addresses == [checksummed]
    assert Web3.is_checksum_address(loader.pool_addresses[0])


def test_mixed_case_address_accepted():
    """A correctly-checksummed address passes validation unchanged."""
    from ingestion.evm_loader import EVMTradeLoader

    checksummed = "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD"
    loader = EVMTradeLoader(
        chain="ethereum",
        rpc_url="https://mock.example.com",
        pool_addresses=[checksummed],
    )
    assert loader.pool_addresses == [checksummed]


# ---------------------------------------------------------------------------
# (b) Malformed address raises ValueError
# ---------------------------------------------------------------------------


def test_malformed_address_raises_value_error():
    """An address that is not a 42-char hex string raises ValueError on ingest."""
    from ingestion.evm_loader import EVMTradeLoader

    with pytest.raises(ValueError, match="Malformed"):
        EVMTradeLoader(
            chain="ethereum",
            rpc_url="https://mock.example.com",
            pool_addresses=["not-an-address"],
        )


def test_too_short_address_raises_value_error():
    """A hex string shorter than 42 characters raises ValueError."""
    from ingestion.evm_loader import EVMTradeLoader

    with pytest.raises(ValueError):
        EVMTradeLoader(
            chain="ethereum",
            rpc_url="https://mock.example.com",
            pool_addresses=["0xABCDEF"],
        )


def test_address_without_0x_prefix_raises():
    """An address lacking the '0x' prefix raises ValueError."""
    from ingestion.evm_loader import EVMTradeLoader

    with pytest.raises(ValueError):
        EVMTradeLoader(
            chain="ethereum",
            rpc_url="https://mock.example.com",
            pool_addresses=["ab5801a7d398351b8be11c439e05c5b3259aec9b"],
        )


# ---------------------------------------------------------------------------
# (c) EVM_POOL_ADDRESSES with invalid address fails at Settings.__post_init__()
# ---------------------------------------------------------------------------


def test_settings_rejects_invalid_pool_address(monkeypatch):
    """Settings raises ValueError at construction when EVM_POOL_ADDRESSES has bad data."""
    monkeypatch.setenv("EVM_POOL_ADDRESSES", "not-a-valid-address")
    from config.settings import Settings
    with pytest.raises(ValueError):
        Settings()


def test_settings_rejects_non_checksummed_pool_address(monkeypatch):
    """Settings raises ValueError when EVM_POOL_ADDRESSES contains a lowercase address."""
    monkeypatch.setenv(
        "EVM_POOL_ADDRESSES",
        "0xab5801a7d398351b8be11c439e05c5b3259aec9b",  # not checksummed
    )
    from config.settings import Settings
    with pytest.raises(ValueError, match="non-checksummed"):
        Settings()


def test_settings_accepts_valid_checksummed_pool_address(monkeypatch):
    """Settings accepts a valid checksummed EVM address in EVM_POOL_ADDRESSES."""
    monkeypatch.setenv(
        "EVM_POOL_ADDRESSES",
        "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B",
    )
    from config.settings import Settings
    s = Settings()
    assert "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B" in s.evm_pool_addresses


def test_settings_accepts_empty_pool_addresses(monkeypatch):
    """Empty EVM_POOL_ADDRESSES (default) is valid — no validation error."""
    monkeypatch.delenv("EVM_POOL_ADDRESSES", raising=False)
    from config.settings import Settings
    s = Settings()
    assert s.evm_pool_addresses == ()


# ---------------------------------------------------------------------------
# EVM address checksum utility
# ---------------------------------------------------------------------------


def test_web3_checksum_round_trip():
    """to_checksum_address is idempotent on already-checksummed addresses."""
    addr = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
    assert Web3.to_checksum_address(addr) == addr
    assert Web3.is_checksum_address(addr)


def test_web3_checksum_upcases_lowercase():
    """to_checksum_address produces a valid checksum from all-lowercase."""
    lower = "0xab5801a7d398351b8be11c439e05c5b3259aec9b"
    checksummed = Web3.to_checksum_address(lower)
    assert Web3.is_checksum_address(checksummed)
    assert checksummed != lower
