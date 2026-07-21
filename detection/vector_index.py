"""
Vector index implementation for approximate nearest neighbor search on wallet embeddings.

Backends:
- faiss_flat: Exact search using FAISS IndexFlatIP (best for small datasets)
- faiss_ivf: Approximate search using FAISS IndexIVFFlat (best for large datasets)
- pgvector: PostgreSQL-based search (requires Postgres and pgvector extension, optional)
"""
from __future__ import annotations

import logging
from typing import List, Tuple, Optional, Protocol

import numpy as np

from config.settings import settings

logger = logging.getLogger(__name__)

try:
    import faiss
except ImportError:
    faiss = None
    logger.warning("FAISS not installed; vector index will not be available")


class VectorIndex(Protocol):
    """Protocol defining the vector index interface."""

    def add(self, wallet: str, vector: np.ndarray) -> None:
        ...

    def add_batch(self, wallets: List[str], vectors: np.ndarray) -> None:
        ...

    def search(self, vector: np.ndarray, k: int) -> List[Tuple[str, float]]:
        ...

    def size(self) -> int:
        ...

    def clear(self) -> None:
        ...


class FaissVectorIndex:
    """FAISS-based vector index implementation."""

    def __init__(
        self,
        dim: Optional[int] = None,
        backend: Optional[str] = None,
        ivf_threshold: Optional[int] = None,
    ):
        if faiss is None:
            raise RuntimeError("FAISS is not installed. Install with: pip install faiss-cpu")

        self.dim = dim or settings.vector_index_dim
        self.backend = backend or settings.vector_index_backend
        self.ivf_threshold = ivf_threshold or settings.vector_index_ivf_threshold

        self._index: Optional[faiss.Index] = None
        self._wallet_list: List[str] = []
        self._wallet_to_idx: dict = {}

        self._initialize_index()

    def _initialize_index(self) -> None:
        if self.backend == "faiss_flat":
            self._index = faiss.IndexFlatIP(self.dim)
        elif self.backend == "faiss_ivf":
            # Use IVF with nlist=100 (a reasonable default)
            quantizer = faiss.IndexFlatIP(self.dim)
            self._index = faiss.IndexIVFFlat(quantizer, self.dim, 100)
            self._index.nprobe = 10  # Search 10 out of 100 clusters
        else:
            raise ValueError(f"Unknown backend: {self.backend}")

    def add(self, wallet: str, vector: np.ndarray) -> None:
        self.add_batch([wallet], vector.reshape(1, -1))

    def add_batch(self, wallets: List[str], vectors: np.ndarray) -> None:
        if vectors.ndim != 2:
            raise ValueError("Vectors must be a 2D array")
        if vectors.shape[1] != self.dim:
            raise ValueError(f"Vector dimension mismatch: expected {self.dim}, got {vectors.shape[1]}")

        # Normalize vectors for inner product (which then equals cosine similarity)
        vectors = vectors.astype(np.float32)
        faiss.normalize_L2(vectors)

        # Check if we need to switch to IVF
        if (
            self.backend == "faiss_flat"
            and len(self._wallet_list) + len(wallets) > self.ivf_threshold
        ):
            logger.info(f"Switching from flat to IVF index (threshold: {self.ivf_threshold})")
            self.backend = "faiss_ivf"
            # Rebuild the index
            all_wallets = self._wallet_list + wallets
            all_vectors = (
                np.vstack([self._get_all_vectors(), vectors]) if self._wallet_list else vectors
            )
            self.clear()
            self._initialize_index()
            self._wallet_list = all_wallets
            self._wallet_to_idx = {w: i for i, w in enumerate(all_wallets)}
            if not self._index.is_trained:
                self._index.train(all_vectors)
            self._index.add(all_vectors)
            return

        # Add the new vectors
        start_idx = len(self._wallet_list)
        for i, wallet in enumerate(wallets):
            self._wallet_list.append(wallet)
            self._wallet_to_idx[wallet] = start_idx + i

        if not self._index.is_trained and isinstance(self._index, faiss.IndexIVFFlat):
            self._index.train(vectors)
        self._index.add(vectors)

    def _get_all_vectors(self) -> np.ndarray:
        if self.backend == "faiss_flat" and hasattr(self._index, "xb"):
            return faiss.vector_to_array(self._index.xb).reshape(-1, self.dim)
        # For IVF, we can't easily get all vectors, so we just return an empty array
        # (this method is only used when switching from flat to IVF, which only happens
        # when the index was originally flat)
        return np.empty((0, self.dim), dtype=np.float32)

    def search(self, vector: np.ndarray, k: int) -> List[Tuple[str, float]]:
        if self.size() == 0:
            return []

        k = min(k, self.size())

        # Normalize the query vector
        query = vector.reshape(1, -1).astype(np.float32)
        faiss.normalize_L2(query)

        distances, indices = self._index.search(query, k)

        results: List[Tuple[str, float]] = []
        for i in range(k):
            idx = indices[0][i]
            if 0 <= idx < len(self._wallet_list):
                wallet = self._wallet_list[idx]
                similarity = float(distances[0][i])
                results.append((wallet, similarity))

        return results

    def size(self) -> int:
        return len(self._wallet_list)

    def clear(self) -> None:
        self._initialize_index()
        self._wallet_list = []
        self._wallet_to_idx = {}


def create_vector_index(
    backend: Optional[str] = None,
    dim: Optional[int] = None,
    ivf_threshold: Optional[int] = None,
) -> VectorIndex:
    """Create a vector index based on settings."""
    backend = backend or settings.vector_index_backend

    if backend in ("faiss_flat", "faiss_ivf"):
        return FaissVectorIndex(dim=dim, backend=backend, ivf_threshold=ivf_threshold)
    elif backend == "pgvector":
        raise NotImplementedError("pgvector backend is not yet implemented")
    else:
        raise ValueError(f"Unknown backend: {backend}")
