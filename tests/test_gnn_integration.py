import pytest

pytest.importorskip("torch")
pytest.importorskip("torch_geometric")

from detection.model_training import train_ensemble
from detection.model_inference import load_models
from detection.feature_engineering import FEATURE_NAMES
from ingestion.synthetic_data import generate_synthetic_dataset


def test_train_ensemble_with_gnn(tmp_path):
    df = generate_synthetic_dataset(n_wallets=60, n_rings=3)
    result = train_ensemble(df, use_gnn=True, model_dir=str(tmp_path))
    assert (tmp_path / "gnn_model.pt").exists()
    assert "gnn_wash_ring_probability" in FEATURE_NAMES
    assert "gnn_neighbor_avg_score" in FEATURE_NAMES
    assert result is not None


def test_load_models_includes_gnn(tmp_path):
    df = generate_synthetic_dataset(n_wallets=30, n_rings=1)
    train_ensemble(df, use_gnn=True, model_dir=str(tmp_path))
    models = load_models(str(tmp_path))
    assert "gnn" in models
