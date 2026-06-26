import os
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from detection.oracle_node import OracleNode
from detection.oracle_coordinator import OracleCoordinator

@pytest.fixture
def mock_nodes():
    nodes = []
    env_vars = {}
    for i in range(5):
        priv = Ed25519PrivateKey.generate()
        key_hex = priv.private_bytes(
            encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.Raw,
            format=__import__("cryptography").hazmat.primitives.serialization.PrivateFormat.Raw,
            encryption_algorithm=__import__("cryptography").hazmat.primitives.serialization.NoEncryption()
        ).hex()
        env_vars[f"TEST_NODE_{i}"] = key_hex
    
    with mock.patch.dict(os.environ, env_vars):
        for i in range(5):
            nodes.append(OracleNode(f"oracle-{i}", f"TEST_NODE_{i}"))
        yield nodes

def test_collect_signatures_short_circuit(mock_nodes):
    coordinator = OracleCoordinator(mock_nodes, threshold=3)
    
    # We want to track how many times sign_score_submission is called
    call_counts = []
    
    original_sign = OracleNode.sign_score_submission
    def mock_sign(self, wallet, asset, score, timestamp):
        call_counts.append(self.name)
        return original_sign(self, wallet, asset, score, timestamp)
    
    with mock.patch.object(OracleNode, "sign_score_submission", new=mock_sign):
        quorum = coordinator.collect_signatures("wallet", "XLM-USDC", 90, 1672531200)
        
    assert quorum.is_valid_quorum
    assert quorum.signers_count == 3
    assert len(call_counts) == 3

def test_quorum_failure_tolerance(mock_nodes):
    # Mock 2 nodes failing
    coordinator = OracleCoordinator(mock_nodes, threshold=3)
    
    original_sign = OracleNode.sign_score_submission
    def mock_sign_fail(self, wallet, asset, score, timestamp):
        if self.name in ["oracle-0", "oracle-1"]:
            raise Exception("Failed to sign")
        return original_sign(self, wallet, asset, score, timestamp)
    
    with mock.patch.object(OracleNode, "sign_score_submission", new=mock_sign_fail):
        quorum = coordinator.collect_signatures("wallet", "XLM-USDC", 90, 1672531200)
        
    assert quorum.is_valid_quorum
    assert quorum.signers_count == 3
    assert len(quorum.signatures) == 3

def test_quorum_not_reached(mock_nodes):
    # Mock 3 nodes failing
    coordinator = OracleCoordinator(mock_nodes, threshold=3)
    
    original_sign = OracleNode.sign_score_submission
    def mock_sign_fail(self, wallet, asset, score, timestamp):
        if self.name in ["oracle-0", "oracle-1", "oracle-2"]:
            raise Exception("Failed to sign")
        return original_sign(self, wallet, asset, score, timestamp)
    
    with mock.patch.object(OracleNode, "sign_score_submission", new=mock_sign_fail):
        quorum = coordinator.collect_signatures("wallet", "XLM-USDC", 90, 1672531200)
        
    assert not quorum.is_valid_quorum
    assert quorum.signers_count == 2
    assert len(quorum.signatures) == 2

def test_submit_with_quorum_returns_false_on_failure(mock_nodes):
    coordinator = OracleCoordinator(mock_nodes, threshold=3)
    
    original_sign = OracleNode.sign_score_submission
    def mock_sign_fail(self, wallet, asset, score, timestamp):
        raise Exception("Failed to sign")
    
    mock_publisher = mock.Mock()
    with mock.patch.object(OracleNode, "sign_score_submission", new=mock_sign_fail):
        success = coordinator.submit_with_quorum("wallet", "XLM-USDC", 90, 1672531200, mock_publisher)
        
    assert not success
    mock_publisher.submit_with_quorum.assert_not_called()

def test_submit_with_quorum_success(mock_nodes):
    coordinator = OracleCoordinator(mock_nodes, threshold=3)
    mock_publisher = mock.Mock()
    mock_publisher.submit_with_quorum.return_value = True
    
    success = coordinator.submit_with_quorum("wallet", "XLM-USDC", 90, 1672531200, mock_publisher)
    
    assert success
    mock_publisher.submit_with_quorum.assert_called_once()
