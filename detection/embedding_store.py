"""
SQLite-based embedding store implementation for wallet embeddings.

Schema:
  - wallet: TEXT (primary key)
  - model_version: TEXT (primary key)
  - embedding: BLOB (numpy float32 array, stored as bytes)
  - computed_at: TIMESTAMP
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from datetime import datetime
from collections.abc import Iterator

import numpy as np

from config.settings import settings


class EmbeddingStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.embedding_store_path
        self._ensure_db_exists()

    def _ensure_db_exists(self):
        """Create the database and table if they don't exist."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wallet_embeddings (
                    wallet TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (wallet, model_version)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_wallet_embeddings_version
                ON wallet_embeddings (model_version)
            """)

    def upsert_embedding(
        self,
        wallet: str,
        model_version: str,
        embedding: np.ndarray,
        computed_at: datetime | None = None,
    ) -> None:
        """Insert or update a wallet's embedding.

        Args:
            wallet: Wallet address.
            model_version: Model version string.
            embedding: Float32 numpy array.
            computed_at: Optional explicit timestamp; defaults to now.
        """
        embedding_bytes = embedding.astype(np.float32).tobytes()
        if computed_at is None:
            computed_at = datetime.now()
        computed_at_str = computed_at.isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO wallet_embeddings (wallet, model_version, embedding, computed_at)
                VALUES (?, ?, ?, ?)
            """, (wallet, model_version, embedding_bytes, computed_at_str))

    def get_embedding(self, wallet: str, model_version: str) -> tuple[np.ndarray, datetime] | None:
        """Retrieve a wallet's embedding and its computed timestamp, or None if not found."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT embedding, computed_at
                FROM wallet_embeddings
                WHERE wallet = ? AND model_version = ?
            """, (wallet, model_version))
            row = cursor.fetchone()
            if row:
                embedding_bytes, computed_at_str = row
                embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                computed_at = datetime.fromisoformat(computed_at_str)
                return embedding, computed_at
            return None

    def get_all_embeddings(self, model_version: str) -> Iterator[tuple[str, np.ndarray]]:
        """Iterate over all embeddings for a given model version."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT wallet, embedding
                FROM wallet_embeddings
                WHERE model_version = ?
            """, (model_version,))
            for row in cursor:
                wallet, embedding_bytes = row
                embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                yield wallet, embedding

    def count_embeddings(self, model_version: str) -> int:
        """Count the number of embeddings for a given model version."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT COUNT(*)
                FROM wallet_embeddings
                WHERE model_version = ?
            """, (model_version,))
            return cursor.fetchone()[0]

    def delete_embedding(self, wallet: str, model_version: str) -> None:
        """Delete an embedding."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM wallet_embeddings WHERE wallet = ? AND model_version = ?",
                (wallet, model_version),
            )

    def get_latest_model_version(self) -> str | None:
        """Get the latest model version (most recent computed_at timestamp)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT model_version FROM wallet_embeddings ORDER BY computed_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
            return row[0] if row else None
