import pytest
import numpy as np
from datetime import datetime, timezone

from detection.embedding_store import EmbeddingStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "embeddings.db")
    return EmbeddingStore(db_path=db_path)


def test_upsert_and_get_embedding(store):
    wallet = "GABC123"
    model_version = "gnn_v1"
    embedding = np.random.rand(64).astype(np.float32)
    timestamp = datetime.now(timezone.utc)

    store.upsert_embedding(wallet, model_version, embedding, timestamp)
    result = store.get_embedding(wallet, model_version)
    assert result is not None
    retrieved_embedding, retrieved_timestamp = result
    np.testing.assert_allclose(embedding, retrieved_embedding, rtol=1e-5)
    assert retrieved_timestamp == timestamp


def test_get_embedding_nonexistent(store):
    assert store.get_embedding("GXYZ", "gnn_v1") is None
    assert store.get_embedding("GABC123", "gnn_v2") is None


def test_get_all_embeddings(store):
    wallets = ["GABC", "GDEF", "GHIJ"]
    model_version = "gnn_v1"
    for wallet in wallets:
        embedding = np.random.rand(64).astype(np.float32)
        store.upsert_embedding(wallet, model_version, embedding)
    
    all_embeddings = list(store.get_all_embeddings(model_version))
    assert len(all_embeddings) == 3
    retrieved_wallets = [w for w, _ in all_embeddings]
    for wallet in wallets:
        assert wallet in retrieved_wallets


def test_count_embeddings(store):
    model_version = "gnn_v1"
    assert store.count_embeddings(model_version) == 0
    store.upsert_embedding("GABC", model_version, np.random.rand(64).astype(np.float32))
    assert store.count_embeddings(model_version) == 1
    store.upsert_embedding("GDEF", model_version, np.random.rand(64).astype(np.float32))
    assert store.count_embeddings(model_version) == 2


def test_delete_embedding(store):
    wallet = "GABC"
    model_version = "gnn_v1"
    embedding = np.random.rand(64).astype(np.float32)
    store.upsert_embedding(wallet, model_version, embedding)
    assert store.count_embeddings(model_version) == 1
    store.delete_embedding(wallet, model_version)
    assert store.count_embeddings(model_version) == 0
    assert store.get_embedding(wallet, model_version) is None


def test_get_latest_model_version(store):
    assert store.get_latest_model_version() is None
    
    # Add older model
    store.upsert_embedding(
        "GABC", "gnn_v1", 
        np.random.rand(64).astype(np.float32), 
        datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    assert store.get_latest_model_version() == "gnn_v1"
    
    # Add newer model
    store.upsert_embedding(
        "GABC", "gnn_v2", 
        np.random.rand(64).astype(np.float32), 
        datetime(2025, 1, 2, tzinfo=timezone.utc)
    )
    assert store.get_latest_model_version() == "gnn_v2"
