"""Integration tests for FLClient against real FL aggregation server.

These tests require the monorepo's FederatedAggregationServer to be available
for testing. They verify end-to-end protocol compatibility.
"""

import pytest
import numpy as np
import threading
import time
from pathlib import Path

from ledgerlens_fl_client import FLClient, DataAdapter
import pandas as pd


class SyntheticDataAdapter(DataAdapter):
    """Adapter yielding synthetic trade data for integration tests."""
    
    def __init__(self, n_samples: int = 100, n_features: int = 50, seed: int = 42):
        self.n_samples = n_samples
        self.n_features = n_features
        self.seed = seed
    
    def trade_batches(self):
        np.random.seed(self.seed)
        X = np.random.randn(self.n_samples, self.n_features)
        y = np.random.randint(0, 2, self.n_samples)
        
        df = pd.DataFrame(X, columns=[f"feature_{i}" for i in range(self.n_features)])
        df["label"] = y
        yield df


@pytest.mark.integration
@pytest.mark.skip(reason="Requires monorepo FL server dependencies")
def test_single_client_train_round():
    """Single client can complete a train round against real server."""
    from detection.federated.server import FederatedAggregationServer, federated_app
    import detection.federated.server as fed_server_mod
    import uvicorn
    
    db_path = "test_integration.db"
    server = FederatedAggregationServer(
        min_participants=1,
        gradient_clip_threshold=100.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=1.0,
        dp_max_epsilon=100.0,
        db_path=db_path,
    )
    fed_server_mod._server_instance = server
    
    def run_server():
        uvicorn.run(federated_app, host="127.0.0.1", port=8765, log_level="error")
    
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(2)
    
    try:
        adapter = SyntheticDataAdapter(n_samples=50, n_features=50)
        client = FLClient(
            server_url="http://127.0.0.1:8765",
            api_key="test-key",
            data_adapter=adapter,
            operator_id="integration-test-op",
        )
        
        result = client.train_round()
        
        assert result.accepted is True
        assert result.round_id is not None
        assert result.n_samples == 50
        
    finally:
        Path(db_path).unlink(missing_ok=True)


@pytest.mark.integration
@pytest.mark.skip(reason="Requires monorepo FL server dependencies")
def test_multiple_clients_two_rounds():
    """Multiple clients can complete multiple federated rounds."""
    from detection.federated.server import FederatedAggregationServer, federated_app
    import detection.federated.server as fed_server_mod
    import uvicorn
    
    db_path = "test_multi_round.db"
    server = FederatedAggregationServer(
        min_participants=2,
        gradient_clip_threshold=100.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=1.0,
        dp_max_epsilon=100.0,
        db_path=db_path,
    )
    fed_server_mod._server_instance = server
    
    def run_server():
        uvicorn.run(federated_app, host="127.0.0.1", port=8766, log_level="error")
    
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(2)
    
    try:
        clients = [
            FLClient(
                server_url="http://127.0.0.1:8766",
                api_key="test-key",
                data_adapter=SyntheticDataAdapter(n_samples=50, n_features=50, seed=i),
                operator_id=f"client-{i}",
            )
            for i in range(3)
        ]
        
        for round_num in range(2):
            results = []
            for client in clients:
                result = client.train_round()
                results.append(result)
            
            assert all(r.accepted for r in results), f"Round {round_num + 1} had rejections"
        
        for client in clients:
            status = client.status()
            assert status.rounds_completed == 2
            
    finally:
        Path(db_path).unlink(missing_ok=True)


@pytest.mark.integration
@pytest.mark.skip(reason="Requires monorepo FL server dependencies")
def test_audit_records_created():
    """Audit records are written for each completed round."""
    from detection.federated.server import FederatedAggregationServer, federated_app
    import detection.federated.server as fed_server_mod
    from detection.federated.audit import get_audit_records
    import uvicorn
    
    db_path = "test_audit.db"
    server = FederatedAggregationServer(
        min_participants=1,
        gradient_clip_threshold=100.0,
        gradient_outlier_threshold=-2.0,
        dp_epsilon=1.0,
        dp_max_epsilon=100.0,
        db_path=db_path,
    )
    fed_server_mod._server_instance = server
    
    def run_server():
        uvicorn.run(federated_app, host="127.0.0.1", port=8767, log_level="error")
    
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(2)
    
    try:
        adapter = SyntheticDataAdapter(n_samples=50, n_features=50)
        client = FLClient(
            server_url="http://127.0.0.1:8767",
            api_key="test-key",
            data_adapter=adapter,
            operator_id="audit-test-op",
        )
        
        client.train_round()
        
        records = get_audit_records(db_path=db_path)
        assert len(records) >= 1, "Expected at least one audit record"
        
        record = records[0]
        assert "round_id" in record
        assert "cumulative_epsilon" in record
        assert "participants" in record
        
    finally:
        Path(db_path).unlink(missing_ok=True)