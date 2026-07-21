import pytest
import numpy as np

from detection.vector_index import create_vector_index, FaissVectorIndex


def test_faiss_flat_index():
    index = FaissVectorIndex(backend="flat", dim=64)
    
    # Add vectors
    wallets = ["GABC", "GDEF", "GHIJ"]
    vectors = np.array([
        np.random.rand(64).astype(np.float32) for _ in range(3)
    ])
    # Normalize vectors for cosine similarity
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    index.add_batch(wallets, vectors)
    
    assert index.size() == 3
    
    # Search for GABC
    results = index.search(vectors[0], k=3)
    assert len(results) == 3
    assert results[0][0] == "GABC"
    assert results[0][1] > 0.99


def test_faiss_ivf_index():
    index = FaissVectorIndex(backend="ivf", dim=64, ivf_threshold=3)
    
    # Add vectors (more than ivf_threshold)
    wallets = [f"GWALLET{i}" for i in range(10)]
    vectors = np.array([
        np.random.rand(64).astype(np.float32) for _ in range(10)
    ])
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    index.add_batch(wallets, vectors)
    
    assert index.size() == 10
    
    # Search for first wallet
    results = index.search(vectors[0], k=5)
    assert len(results) == 5
    assert results[0][0] == "GWALLET0"
    assert results[0][1] > 0.99


def test_create_vector_index():
    # Test with flat
    index = create_vector_index(backend="faiss_flat", dim=64)
    assert isinstance(index, FaissVectorIndex)
    assert index.backend == "flat"
    
    # Test with ivf
    index = create_vector_index(backend="faiss_ivf", dim=64)
    assert isinstance(index, FaissVectorIndex)
    assert index.backend == "ivf"


def test_clear_index():
    index = FaissVectorIndex(backend="flat", dim=64)
    index.add_batch(["GABC"], np.array([np.random.rand(64).astype(np.float32)]))
    assert index.size() == 1
    index.clear()
    assert index.size() == 0
