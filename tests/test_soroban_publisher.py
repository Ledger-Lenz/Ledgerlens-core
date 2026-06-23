"""Tests for the Soroban on-chain score publisher.

All tests mock the Stellar SDK — no live Horizon or Soroban RPC calls.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── Mock stellar_sdk before importing the module under test ─────────────

sys.modules.pop("detection.soroban_publisher", None)
sys.modules.pop("stellar_sdk", None)
sys.modules.pop("stellar_sdk.operation", None)
sys.modules.pop("stellar_sdk.soroban_rpc", None)

_mock_stellar_sdk = MagicMock()
_mock_stellar_sdk.operation = MagicMock()
sys.modules["stellar_sdk"] = _mock_stellar_sdk
sys.modules["stellar_sdk.operation"] = _mock_stellar_sdk.operation
sys.modules["stellar_sdk.soroban_rpc"] = MagicMock()

from detection.risk_score import RiskScore  # noqa: E402
from detection.soroban_publisher import (  # noqa: E402
    SorobanCircuitOpenError,
    SorobanPublisher,
    SorobanSubmissionError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTRACT_ID = "CA3CQ7C6YHK6K6C6J6C6K6C6K6C6K6C6K6C6K6C6K6C6K6C6K6C6K6C6"
SECRET_KEY = "SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # fake
RPC_URL = "https://soroban-testnet.stellar.org"
PASSPHRASE = "Test SDF Network ; September 2015"


def _make_score(  # noqa: PLR0913
    wallet: str = "GABCDEF123",
    asset_pair: str = "XLM/USDC",
    score: int = 85,
    benford_flag: bool = True,
    ml_flag: bool = True,
    confidence: int = 90,
) -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=benford_flag,
        ml_flag=ml_flag,
        confidence=confidence,
        timestamp=datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc),
    )


def _mock_soroban_server(send_status="PENDING", tx_status="SUCCESS", send_error=None, tx_error=None):
    """Build a mock SorobanServer with controllable responses."""
    server = MagicMock()
    account = MagicMock()
    account.sequence = 12345
    server.load_account.return_value = account

    sim_result = MagicMock()
    sim_result.error = None
    sim_result.min_resource_fee = "1000"
    server.simulate_transaction.return_value = sim_result

    send_result = MagicMock()
    send_result.status = send_status
    send_result.hash = "abc123def456abc123def456abc123def456abc123def456abc123def456abc123"
    send_result.error = send_error
    server.send_transaction.return_value = send_result

    tx_result = MagicMock()
    tx_result.status = tx_status
    tx_result.error = tx_error
    server.get_transaction.return_value = tx_result

    return server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def publisher():
    return SorobanPublisher(
        contract_id=CONTRACT_ID,
        secret_key=SECRET_KEY,
        soroban_rpc_url=RPC_URL,
        network_passphrase=PASSPHRASE,
    )


# ---------------------------------------------------------------------------
# Successful submission
# ---------------------------------------------------------------------------


def test_submit_score_success(publisher):
    """A successful submission returns a non-empty transaction hash."""
    server = _mock_soroban_server()

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            tx_hash = publisher.submit_score(_make_score())

    assert tx_hash is not None
    assert len(tx_hash) > 0
    assert isinstance(tx_hash, str)
    server.send_transaction.assert_called_once()
    server.get_transaction.assert_called_once()


# ---------------------------------------------------------------------------
# tx_bad_seq retry
# ---------------------------------------------------------------------------


def test_tx_bad_seq_triggers_one_retry(publisher):
    """tx_bad_seq triggers one retry; second failure raises SorobanSubmissionError."""
    server = _mock_soroban_server()

    call_count = [0]

    def load_account_side_effect(account_id):
        call_count[0] += 1
        account = MagicMock()
        account.sequence = 12345 + call_count[0]
        return account

    server.load_account.side_effect = load_account_side_effect

    send_result_1 = MagicMock()
    send_result_1.status = "ERROR"
    send_result_1.error = "tx_bad_seq"
    send_result_1.hash = None

    send_result_2 = MagicMock()
    send_result_2.status = "ERROR"
    send_result_2.error = "tx_bad_seq"
    send_result_2.hash = None

    server.send_transaction.side_effect = [send_result_1, send_result_2]

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            with pytest.raises(SorobanSubmissionError):
                publisher.submit_score(_make_score())

    # Two calls: initial and retry
    assert server.send_transaction.call_count == 2


# ---------------------------------------------------------------------------
# INSUFFICIENT_FEE retry
# ---------------------------------------------------------------------------


def test_insufficient_fee_triggers_retry(publisher):
    """INSUFFICIENT_FEE triggers one retry with 1.5x fee."""
    server = _mock_soroban_server()

    send_result_1 = MagicMock()
    send_result_1.status = "ERROR"
    send_result_1.error = "INSUFFICIENT_FEE"
    send_result_1.hash = None

    send_result_2 = MagicMock()
    send_result_2.status = "PENDING"
    send_result_2.hash = "retry_hash_abcdef1234567890abcdef1234567890abcdef12"

    tx_result = MagicMock()
    tx_result.status = "SUCCESS"
    server.get_transaction.return_value = tx_result

    server.send_transaction.side_effect = [send_result_1, send_result_2]

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            tx_hash = publisher.submit_score(_make_score())

    assert tx_hash == "retry_hash_abcdef1234567890abcdef1234567890abcdef12"
    assert server.send_transaction.call_count == 2


# ---------------------------------------------------------------------------
# Auth failure — no retry
# ---------------------------------------------------------------------------


def test_auth_failure_raises_immediately(publisher):
    """auth_failed raises SorobanSubmissionError immediately without retry."""
    server = _mock_soroban_server()

    sim_result = MagicMock()
    sim_result.error = "AuthFailed: unauthorized caller"
    sim_result.min_resource_fee = "1000"
    server.simulate_transaction.return_value = sim_result

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            with pytest.raises(SorobanSubmissionError, match="auth"):
                publisher.submit_score(_make_score())

    # send_transaction should NOT be called — error at simulation stage
    server.send_transaction.assert_not_called()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_5_failures(publisher):
    """Circuit breaker opens after 5 consecutive failures within the window."""
    server = _mock_soroban_server()

    send_result = MagicMock()
    send_result.status = "ERROR"
    send_result.error = "simulation error"
    send_result.hash = None
    server.send_transaction.return_value = send_result

    sim_result = MagicMock()
    sim_result.error = "execution error"
    sim_result.min_resource_fee = "1000"
    server.simulate_transaction.return_value = sim_result

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            # First 5 failures — each raises SorobanSubmissionError
            for _ in range(5):
                with pytest.raises(SorobanSubmissionError):
                    publisher.submit_score(_make_score())

            # 6th attempt — circuit should be open
            with pytest.raises(SorobanCircuitOpenError):
                publisher.submit_score(_make_score())

    assert len(publisher._failure_timestamps) >= 5


# ---------------------------------------------------------------------------
# Circuit breaker resets after cooldown
# ---------------------------------------------------------------------------


def test_circuit_breaker_resets_after_cooldown(publisher):
    """Circuit breaker resets after the cooldown period."""
    # Set a very short cooldown for testing
    publisher._circuit_reset_seconds = 0  # immediately reset
    publisher._failure_timestamps = [time.time() - 3600] * 5  # old failures

    # Should NOT raise — failures are old enough to reset
    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        # Check that _check_circuit does not raise
        publisher._check_circuit()  # should silently reset


# ---------------------------------------------------------------------------
# dry_run=True
# ---------------------------------------------------------------------------


def test_dry_run_does_not_call_send(publisher):
    """dry_run=True must not sign or submit any transaction."""
    with patch("detection.soroban_publisher.SorobanServer") as MockServer:
        result = publisher.submit_score(_make_score(), dry_run=True)

    assert result is None
    MockServer.assert_not_called()


# ---------------------------------------------------------------------------
# Secret key must not appear in logs
# ---------------------------------------------------------------------------


def test_secret_key_not_in_logs(publisher, caplog):
    """service_secret_key does not appear in any log message."""
    caplog.set_level(logging.INFO)

    server = _mock_soroban_server()
    sim_result = MagicMock()
    sim_result.error = None
    sim_result.min_resource_fee = "1000"
    server.simulate_transaction.return_value = sim_result

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            publisher.submit_score(_make_score())

    for record in caplog.records:
        assert SECRET_KEY not in record.getMessage()


# ---------------------------------------------------------------------------
# on_chain_submissions table written for success and failure
# ---------------------------------------------------------------------------


def test_submission_logged_on_success(publisher):
    """on_chain_submissions table is written on successful submission."""
    server = _mock_soroban_server()

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            with patch("detection.soroban_publisher.save_submission") as mock_save:
                publisher.submit_score(_make_score())

    mock_save.assert_called_once()
    args, kwargs = mock_save.call_args
    assert args[3] == "submitted"
    assert "tx_hash" in kwargs


def test_submission_logged_on_failure(publisher):
    """on_chain_submissions table is written on submission failure."""
    server = _mock_soroban_server()
    sim_result = MagicMock()
    sim_result.error = "execution error"
    sim_result.min_resource_fee = "1000"
    server.simulate_transaction.return_value = sim_result

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            with patch("detection.soroban_publisher.save_submission") as mock_save:
                with pytest.raises(SorobanSubmissionError):
                    publisher.submit_score(_make_score())

    mock_save.assert_called_once()
    args, kwargs = mock_save.call_args
    assert args[3] == "failed"


# ---------------------------------------------------------------------------
# submit_batch returns proper results dict
# ---------------------------------------------------------------------------


def test_submit_batch_returns_dict(publisher):
    """submit_batch returns a dict mapping keys to results."""
    scores = [_make_score(wallet=f"G{i:04d}") for i in range(3)]

    server = _mock_soroban_server()

    with patch.object(publisher, "_keypair") as mock_kp:
        mock_kp.public_key = "GABC123"
        with patch("detection.soroban_publisher.SorobanServer", return_value=server):
            results = publisher.submit_batch(scores)

    assert isinstance(results, dict)
    assert len(results) == 3
    for key, value in results.items():
        assert isinstance(key, str)
        assert ":" in key
        assert isinstance(value, str)
        assert len(value) > 0
