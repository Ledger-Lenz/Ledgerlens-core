"""Test fixtures for FL client tests."""

import pytest
import numpy as np
import pandas as pd

from ledgerlens_fl_client.adapter import DataAdapter


class MockDataAdapter(DataAdapter):
    """Mock data adapter for testing."""
    
    def __init__(self, n_samples: int = 100, n_features: int = 50):
        self.n_samples = n_samples
        self.n_features = n_features
    
    def trade_batches(self):
        """Yield synthetic trade data."""
        np.random.seed(42)
        X = np.random.randn(self.n_samples, self.n_features)
        y = np.random.randint(0, 2, self.n_samples)
        
        df = pd.DataFrame(X, columns=[f"feature_{i}" for i in range(self.n_features)])
        df["label"] = y
        yield df


@pytest.fixture
def mock_data_adapter():
    """Return a mock data adapter with synthetic data."""
    return MockDataAdapter(n_samples=100, n_features=50)


@pytest.fixture
def temp_csv_dir(tmp_path):
    """Create a temporary directory with CSV files."""
    np.random.seed(42)
    
    for i in range(3):
        n_samples = 50
        X = np.random.randn(n_samples, 10)
        y = np.random.randint(0, 2, n_samples)
        
        df = pd.DataFrame(X, columns=[f"feature_{j}" for j in range(10)])
        df["label"] = y
        df.to_csv(tmp_path / f"data_{i}.csv", index=False)
    
    return tmp_path