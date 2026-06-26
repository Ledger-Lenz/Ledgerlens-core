"""Unit tests for FLClient with mocked HTTP transport."""

import pytest
import numpy as np
from unittest.mock import Mock, patch, MagicMock

from ledgerlens_fl_client.client import FLClient
from ledgerlens_fl_client.models import RoundResult, ClientStatus
from ledgerlens_fl_client.adapter import DataAdapter
import pandas as pd


class MockDataAdapter(DataAdapter):
    def trade_batches(self):
        np.random.seed(42)
        X = np.random.randn(50, 20)
        y = np.random.randint(0, 2, 50)
        df = pd.DataFrame(X, columns=[f"f{i}" for i in range(20)])
        df["label"] = y
        yield df


def test_client_init_with_defaults():
    """FLClient initializes with default parameters."""
    adapter = MockDataAdapter()
    client = FLClient(
        server_url="http://test-server:8001",
        api_key="test-key",
        data_adapter=adapter,
    )
    
    assert client.operator_id is not None
    assert client.dp_epsilon == 1.0
    assert client.dp_delta == 1e-5
    assert client.gradient_clip_threshold == 10.0
    assert client.noise_multiplier == 0.0
    assert client.ensemble_weights["random_forest"] == 0.25
    assert client.ensemble_weights["xgboost"] == 0.50
    assert client.ensemble_weights["lightgbm"] == 0.25


def test_client_init_with_custom_operator_id():
    """FLClient uses provided operator_id."""
    adapter = MockDataAdapter()
    client = FLClient(
        server_url="http://test:8001",
        api_key="key",
        data_adapter=adapter,
        operator_id="custom-id",
    )
    assert client.operator_id == "custom-id"


def test_client_status_before_any_rounds():
    """Client status shows zero rounds before training."""
    adapter = MockDataAdapter()
    client = FLClient(
        server_url="http://test:8001",
        api_key="key",
        data_adapter=adapter,
        operator_id="test-op",
    )
    
    status = client.status()
    assert status.operator_id == "test-op"
    assert status.rounds_completed == 0
    assert status.has_models is False
    assert len(status.public_key_der_b64) > 0


@patch("ledgerlens_fl_client.client.get_public_dataset")
@patch("ledgerlens_fl_client.client.FLProtocol")
def test_train_round_calls_register(mock_protocol_cls, mock_get_pub_ds):
    """train_round() registers with server on first call."""
    mock_protocol = Mock()
    mock_protocol.register = Mock(return_value={"status": "registered"})
    mock_protocol.fetch_global_model = Mock(return_value=Mock(
        round_id="test-round-id",
        global_soft_labels=None,
    ))
    mock_protocol.submit_update = Mock(return_value={
        "accepted": True,
        "reason": "ok",
        "pending_valid": 1,
        "quorum": 3,
    })
    mock_protocol_cls.return_value = mock_protocol
    
    mock_get_pub_ds.return_value = np.random.randn(100, 20)
    
    adapter = MockDataAdapter()
    client = FLClient(
        server_url="http://test:8001",
        api_key="key",
        data_adapter=adapter,
        operator_id="test-op",
    )
    
    # Mock _load_private_data to return synthetic data
    with patch.object(client, '_load_private_data', return_value=(np.random.randn(50, 20), np.random.randint(0, 2, 50))):
        result = client.train_round()
    
    mock_protocol.register.assert_called_once()
    assert result.accepted is True


@patch("ledgerlens_fl_client.client.get_public_dataset")
@patch("ledgerlens_fl_client.client.FLProtocol")
def test_train_round_returns_round_result(mock_protocol_cls, mock_get_pub_ds):
    """train_round() returns RoundResult with expected fields."""
    mock_protocol = Mock()
    mock_protocol.register = Mock()
    mock_protocol.fetch_global_model = Mock(return_value=Mock(
        round_id="round-123",
        global_soft_labels=None,
    ))
    mock_protocol.submit_update = Mock(return_value={
        "accepted": True,
        "reason": "ok",
        "pending_valid": 2,
        "quorum": 3,
    })
    mock_protocol_cls.return_value = mock_protocol
    
    mock_get_pub_ds.return_value = np.random.randn(50, 20)
    
    adapter = MockDataAdapter()
    client = FLClient(
        server_url="http://test:8001",
        api_key="key",
        data_adapter=adapter,
    )
    
    with patch.object(client, '_load_private_data', return_value=(np.random.randn(50, 20), np.random.randint(0, 2, 50))):
        result = client.train_round()
    
    assert isinstance(result, RoundResult)
    assert result.round_id == "round-123"
    assert result.accepted is True
    assert result.reason == "ok"
    assert result.n_valid_pending == 2
    assert result.quorum == 3
    assert result.n_samples > 0


@patch("ledgerlens_fl_client.client.get_public_dataset")
@patch("ledgerlens_fl_client.client.FLProtocol")
def test_train_round_rejects_update(mock_protocol_cls, mock_get_pub_ds):
    """train_round() handles rejected updates correctly."""
    mock_protocol = Mock()
    mock_protocol.register = Mock()
    mock_protocol.fetch_global_model = Mock(return_value=Mock(
        round_id="test-round",
        global_soft_labels=None,
    ))
    mock_protocol.submit_update = Mock(return_value={
        "accepted": False,
        "reason": "cosine_sim=0.05 < threshold",
        "pending_valid": 2,
        "quorum": 3,
    })
    mock_protocol_cls.return_value = mock_protocol
    mock_get_pub_ds.return_value = np.random.randn(50, 20)
    
    adapter = MockDataAdapter()
    client = FLClient(
        server_url="http://test:8001",
        api_key="key",
        data_adapter=adapter,
    )
    
    with patch.object(client, '_load_private_data', return_value=(np.random.randn(50, 20), np.random.randint(0, 2, 50))):
        result = client.train_round()
    
    assert result.accepted is False
    assert "cosine_sim" in result.reason


@patch("ledgerlens_fl_client.client.get_public_dataset")
@patch("ledgerlens_fl_client.client.FLProtocol")
def test_dp_noise_is_applied(mock_protocol_cls, mock_get_pub_ds):
    """DP noise is injected into soft labels."""
    mock_protocol = Mock()
    mock_protocol.register = Mock()
    mock_protocol.fetch_global_model = Mock(return_value=Mock(
        round_id="test",
        global_soft_labels=None,
    ))
    
    def capture_submit(participant_id, soft_labels, n_samples, signature_b64):
        noise_std = np.std(soft_labels)
        assert noise_std > 0.001, "DP noise should be present"
        return {"accepted": True, "reason": "ok", "pending_valid": 1, "quorum": 1}
    
    mock_protocol.submit_update = Mock(side_effect=capture_submit)
    mock_protocol_cls.return_value = mock_protocol
    # Public dataset should be 2D: (n_samples, n_features)
    mock_get_pub_ds.return_value = np.random.randn(50, 20)
    
    adapter = MockDataAdapter()
    client = FLClient(
        server_url="http://test:8001",
        api_key="key",
        data_adapter=adapter,
        dp_epsilon=0.1,
        gradient_clip_threshold=10.0,
    )
    
    with patch.object(client, '_load_private_data', return_value=(np.random.randn(50, 20), np.random.randint(0, 2, 50))):
        client.train_round()