"""Tests for #169 – pydantic_settings BaseSettings validation (fail-fast startup checks)."""

import pytest
from pydantic import ValidationError

from config.settings import Settings


# ---------------------------------------------------------------------------
# Port range validation
# ---------------------------------------------------------------------------

def test_invalid_port_too_high(monkeypatch):
    monkeypatch.setenv("FEDERATED_SERVER_PORT", "99999")
    with pytest.raises((ValidationError, ValueError), match="out of range"):
        Settings()


def test_invalid_port_zero(monkeypatch):
    monkeypatch.setenv("FEDERATED_SERVER_PORT", "0")
    with pytest.raises((ValidationError, ValueError), match="out of range"):
        Settings()


def test_valid_port_boundary(monkeypatch):
    monkeypatch.setenv("FEDERATED_SERVER_PORT", "65535")
    s = Settings()
    assert s.federated_server_port == 65535


# ---------------------------------------------------------------------------
# Invalid URL format
# ---------------------------------------------------------------------------

def test_invalid_horizon_url(monkeypatch):
    monkeypatch.setenv("HORIZON_URL", "not-a-url")
    with pytest.raises((ValidationError, ValueError), match="valid URL"):
        Settings()


def test_invalid_soroban_rpc_url(monkeypatch):
    monkeypatch.setenv("SOROBAN_RPC_URL", "ftp://wrong-scheme.example.com")
    with pytest.raises((ValidationError, ValueError), match="valid URL"):
        Settings()


def test_invalid_redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "tcp://localhost:6379")
    with pytest.raises((ValidationError, ValueError), match="valid URL"):
        Settings()


# ---------------------------------------------------------------------------
# RISK_SCORE_THRESHOLD out of range
# ---------------------------------------------------------------------------

def test_risk_score_threshold_above_100(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "101")
    with pytest.raises((ValidationError, ValueError), match="0-100"):
        Settings()


def test_risk_score_threshold_negative(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "-1")
    with pytest.raises((ValidationError, ValueError), match="0-100"):
        Settings()


def test_risk_score_threshold_boundary_values(monkeypatch):
    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "0")
    s = Settings()
    assert s.risk_score_threshold == 0

    monkeypatch.setenv("RISK_SCORE_THRESHOLD", "100")
    s = Settings()
    assert s.risk_score_threshold == 100


# ---------------------------------------------------------------------------
# Positive-integer fields
# ---------------------------------------------------------------------------

def test_poll_interval_must_be_positive(monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    with pytest.raises((ValidationError, ValueError)):
        Settings()


def test_evm_lookback_blocks_must_be_positive(monkeypatch):
    monkeypatch.setenv("EVM_LOOKBACK_BLOCKS", "-1")
    with pytest.raises((ValidationError, ValueError)):
        Settings()


def test_streamer_queue_configuration_is_validated(monkeypatch):
    monkeypatch.setenv("STREAMER_QUEUE_MAXSIZE", "0")
    with pytest.raises((ValidationError, ValueError)):
        Settings()

    monkeypatch.setenv("STREAMER_QUEUE_MAXSIZE", "10")
    monkeypatch.setenv("STREAMER_OVERFLOW_STRATEGY", "overwrite")
    with pytest.raises((ValidationError, ValueError), match="block"):
        Settings()

    monkeypatch.setenv("STREAMER_OVERFLOW_STRATEGY", "block")
    monkeypatch.setenv("STREAMER_HIGH_WATER_RATIO", "1.1")
    with pytest.raises((ValidationError, ValueError), match="\\(0, 1\\]"):
        Settings()


# ---------------------------------------------------------------------------
# NETWORK enum validation
# ---------------------------------------------------------------------------

def test_invalid_network_value(monkeypatch):
    monkeypatch.setenv("NETWORK", "regtest")
    with pytest.raises((ValidationError, ValueError), match="testnet.*mainnet|mainnet.*testnet"):
        Settings()


def test_network_mainnet_accepted(monkeypatch):
    monkeypatch.setenv("NETWORK", "mainnet")
    s = Settings()
    assert s.network == "mainnet"


# ---------------------------------------------------------------------------
# Fail-fast: error is raised at import / construction time, not deferred
# ---------------------------------------------------------------------------

def test_startup_fails_fast_on_bad_config(monkeypatch):
    """Misconfiguration raises within Settings() — no deferred error."""
    monkeypatch.setenv("HORIZON_URL", "bad-url")
    with pytest.raises((ValidationError, ValueError)):
        Settings()


# ---------------------------------------------------------------------------
# cli config validate command
# ---------------------------------------------------------------------------

def test_cli_config_validate_exits_zero(monkeypatch):
    """config validate succeeds when env is valid."""
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "✅" in result.output


def test_cli_config_validate_masks_secrets(monkeypatch):
    """config validate prints *** for secret fields."""
    monkeypatch.setenv("LEDGERLENS_SERVICE_SECRET_KEY", "super-secret")
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "super-secret" not in result.output
    assert "***" in result.output


def test_cli_config_validate_exits_nonzero_on_bad_config(monkeypatch):
    """config validate exits 1 when config is invalid."""
    monkeypatch.setenv("HORIZON_URL", "not-a-url")
    from typer.testing import CliRunner
    from cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code != 0
