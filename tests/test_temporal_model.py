"""Tests for the LSTM temporal anomaly detection model, sequence builder, and registry integration."""

import os
import tempfile
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest
import torch

from detection.model_registry import get_current_version, load_latest_model, save_versioned_model
from detection.risk_score import temporal_risk_adjustment
from detection.temporal_dataset import (
    cluster_score_correlation,
    generate_synthetic_sequence,
)
from detection.temporal_model import (
    TemporalAnomalyLSTM,
    predict_temporal_risk,
    train_temporal_model,
)


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    fd, path = tempfile.mkstemp()
    yield path
    os.close(fd)
    if os.path.exists(path):
        os.remove(path)


def test_cluster_score_correlation():
    """Verify that cluster_score_correlation correctly identifies coordinated rising trajectories."""
    dates = [datetime.now(timezone.utc).date() - timedelta(days=i) for i in range(30)]
    dates.reverse()

    # Highly correlated wallets (W1, W2, W3 all rising together)
    data_corr = []
    for i, date in enumerate(dates):
        base_val = 20.0 + i * 1.5
        data_corr.append({"wallet": "W1", "score": base_val, "timestamp": date.isoformat()})
        data_corr.append({"wallet": "W2", "score": base_val + 2, "timestamp": date.isoformat()})
        data_corr.append({"wallet": "W3", "score": base_val - 1, "timestamp": date.isoformat()})

    df_corr = pd.DataFrame(data_corr)
    corr_high = cluster_score_correlation(["W1", "W2", "W3"], df_corr)
    assert corr_high > 0.85

    # Uncorrelated/noisy wallets
    np.random.seed(42)
    data_uncorr = []
    for date in dates:
        data_uncorr.append({"wallet": "W1", "score": np.random.uniform(10, 50), "timestamp": date.isoformat()})
        data_uncorr.append({"wallet": "W2", "score": np.random.uniform(10, 50), "timestamp": date.isoformat()})
        data_uncorr.append({"wallet": "W3", "score": np.random.uniform(10, 50), "timestamp": date.isoformat()})

    df_uncorr = pd.DataFrame(data_uncorr)
    corr_low = cluster_score_correlation(["W1", "W2", "W3"], df_uncorr)
    assert corr_low < 0.5


def test_temporal_risk_adjustment_fallback():
    """Verify fallback and blending logic of temporal_risk_adjustment."""
    # Under 7 days: fallback to snapshot_score
    assert temporal_risk_adjustment(snapshot_score=50, temporal_score=0.9, history_days=5) == 50
    assert temporal_risk_adjustment(snapshot_score=35, temporal_score=None, history_days=10) == 35

    # Over 7 days: correct blending (0.7 * snapshot_score + 0.3 * (temporal_score * 100))
    # 0.7 * 50 + 0.3 * 90 = 35 + 27 = 62
    assert temporal_risk_adjustment(snapshot_score=50, temporal_score=0.9, history_days=10) == 62


def test_lstm_ramp_up_detection_and_fpr():
    """Train on synthetic data and verify recall >= 90% and FPR < 5%."""
    np.random.seed(42)
    torch.manual_seed(42)

    # Generate synthetic training dataset
    X_train = []
    y_train = []

    # 100 positive, 100 negative examples
    for _ in range(100):
        X_train.append(generate_synthetic_sequence(label=1, sequence_length=30))
        y_train.append(1.0)
        X_train.append(generate_synthetic_sequence(label=0, sequence_length=30))
        y_train.append(0.0)

    X_train = np.array(X_train, dtype=np.float32)
    y_train = np.array(y_train, dtype=np.float32)

    # Train model
    model = train_temporal_model(X_train, y_train, epochs=20, batch_size=16, lr=0.005)

    # Evaluate on test set
    X_test_pos = []
    X_test_neg = []
    for _ in range(100):
        X_test_pos.append(generate_synthetic_sequence(label=1, sequence_length=30))
        X_test_neg.append(generate_synthetic_sequence(label=0, sequence_length=30))

    pos_preds = np.array([predict_temporal_risk(model, seq) for seq in X_test_pos])
    neg_preds = np.array([predict_temporal_risk(model, seq) for seq in X_test_neg])

    recall = np.mean(pos_preds >= 0.5)
    fpr = np.mean(neg_preds >= 0.5)

    assert recall >= 0.90, f"Recall was {recall * 100:.1f}%, expected >= 90%"
    assert fpr < 0.05, f"False Positive Rate was {fpr * 100:.1f}%, expected < 5%"


def test_temporal_model_registry_integration(tmp_path):
    """Verify that TemporalAnomalyLSTM integrates with model registry and signatures verify."""
    model_dir = str(tmp_path)
    version = "temp0001"

    # Define simple model and save
    model = TemporalAnomalyLSTM(input_size=5)
    save_versioned_model(model, "temporal_lstm", version, model_dir)

    # Verify latest pointer was written
    current = get_current_version("temporal_lstm", model_dir)
    assert current == version

    # Load versioned model back and verify types
    loaded = load_latest_model("temporal_lstm", model_dir)
    assert isinstance(loaded, TemporalAnomalyLSTM)
