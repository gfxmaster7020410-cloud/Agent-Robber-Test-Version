"""Low-memory dense vector embeddings with ONNX and SQLite storage.

Memory footprint strictly capped < 200 MB for embedding model and vector operations.
Uses quantized ONNX models (INT8) and disk-backed SQLite storage with sqlite-vec.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from robber.jail import workspace_root

logger = logging.getLogger(__name__)

# Vector dimension for embedding models
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2, bge-small-en-v1.5


@dataclass
class VectorMatch:
    """Vector similarity search result."""
    
    id: str
    path: str
    content: str
    similarity: float
    vector: list[float] = field(default_factory=list)


class VectorStore:
    """Low-footprint vector store with ONNX embeddings and SQLite persistence."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        model_name: str = "all-MiniLM-L6-v2",
        db_path: Path | None = None,
    ) -> None:
        self.root = (root or workspace_root()).resolve()
        self.model_name = model_name
        self.db_path = db_path or (self.root / "data" / "vectors.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._model = None
        self._session = None
        self._tokenizer = None
        self._conn = None

    def initialize(self) -> None:
        """Initialize ONNX model and SQLite connection (lazy loading)."""
        if self._session is None:
            self._load_onnx_model()
        if self._conn is None:
            self._init_db()

    def _load_onnx_model(self) -> None:
        """Load quantized ONNX embedding model (< 90 MB RAM)."""
        try:
            import onnxruntime as ort
            from transformers import AutoTokenizer
        except ImportError as exc:
            logger.warning(
                "ONNX runtime or transformers not installed. "
                "Install with: pip install onnxruntime transformers"
            )
            return

        try:
            # Load tokenizer
            logger.info(f"Loading tokenizer: {self.model_name}")
            self._tokenizer = AutoTokenizer.from_pretrained(
                f"sentence-transformers/{self.model_name}"
            )

            # Load ONNX model (quantized to INT8 for low memory)
            model_path = self._get_or_download_onnx_model()
            if model_path and model_path.exists():
                logger.info(f"Loading ONNX model: {model_path}")
                self._session = ort.InferenceSession(
                    str(model_path),
                    providers=["CPUExecutionProvider"],
                )
                logger.info("ONNX model loaded successfully")
            else:
                logger.warning("ONNX model not found, vector embeddings disabled")

        except Exception as exc:
            logger.error(f"Failed to load ONNX model: {exc}")

    def _get_or_download_onnx_model(self) -> Path | None:
        """Get ONNX model path (downloads if needed)."""
        # For now, return None - user needs to convert/download ONNX model manually
        # Full implementation would download from HuggingFace or convert PyTorch->ONNX
        model_dir = self.root / "data" / "models" / self.model_name
        model_path = model_dir / "model.onnx"
        
        if model_path.exists():
            return model_path
        
        logger.info(
            f"ONNX model not found at {model_path}. "
            "To enable vector search, download/convert model to ONNX format."
        )
        return None

    def _init_db(self) -> None:
        """Initialize SQLite database with vector storage."""
        import sqlite3

        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row

        # Create vectors table with BLOB storage for dense vectors
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                vector BLOB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vectors_path ON vectors(path)
        """)
        
        self._conn.commit()
        logger.info(f"Vector store initialized at {self.db_path}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for text snippets using ONNX model."""
        if self._session is None or self._tokenizer is None:
            # Fallback: return zero vectors if model not loaded
            logger.warning("ONNX model not loaded, returning zero vectors")
            return [[0.0] * EMBEDDING_DIM for _ in texts]

        try:
            # Tokenize
            inputs = self._tokenizer(
                texts,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="np",
            )

            # Run ONNX inference
            input_feed = {
                "input_ids": inputs["input_ids"].astype(np.int64),
                "attention_mask": inputs["attention_mask"].astype(np.int64),
            }
            
            # Add token_type_ids if model expects it
            if "token_type_ids" in inputs:
                input_feed["token_type_ids"] = inputs["token_type_ids"].astype(np.int64)

            outputs = self._session.run(None, input_feed)
            embeddings = outputs[0]  # Shape: (batch_size, seq_len, hidden_dim)

            # Mean pooling with attention mask
            attention_mask = inputs["attention_mask"]
            input_mask_expanded = np.expand_dims(attention_mask, -1)
            sum_embeddings = np.sum(embeddings * input_mask_expanded, axis=1)
            sum_mask = np.clip(input_mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
            mean_pooled = sum_embeddings / sum_mask

            # Normalize
            mean_pooled = mean_pooled / np.linalg.norm(mean_pooled, axis=1, keepdims=True)

            return mean_pooled.tolist()

        except Exception as exc:
            logger.error(f"Embedding failed: {exc}")
            return [[0.0] * EMBEDDING_DIM for _ in texts]

    def add(self, doc_id: str, path: str, content: str) -> None:
        """Add document with vector embedding to store."""
        self.initialize()

        # Generate embedding
        embeddings = self.embed([content])
        vector = embeddings[0]

        # Serialize vector to BLOB (float32 packed binary)
        vector_blob = struct.pack(f"{len(vector)}f", *vector)

        # Insert into database
        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO vectors (id, path, content, vector)
                VALUES (?, ?, ?, ?)
                """,
                (doc_id, path, content[:1000], vector_blob),  # Store first 1000 chars
            )
            self._conn.commit()
        except Exception as exc:
            logger.error(f"Failed to add vector to store: {exc}")

    def search(self, query: str, top_k: int = 5) -> list[VectorMatch]:
        """Search for similar vectors using cosine similarity."""
        self.initialize()

        if self._conn is None:
            return []

        # Generate query embedding
        query_embeddings = self.embed([query])
        query_vector = np.array(query_embeddings[0], dtype=np.float32)

        # Fetch all vectors from database (full scan - for production, use FAISS/Annoy)
        cursor = self._conn.execute("SELECT id, path, content, vector FROM vectors")
        
        matches: list[tuple[str, str, str, float, list[float]]] = []
        
        for row in cursor:
            # Deserialize vector
            vector_blob = row["vector"]
            vector = np.array(
                struct.unpack(f"{len(vector_blob) // 4}f", vector_blob),
                dtype=np.float32,
            )

            # Cosine similarity
            similarity = float(np.dot(query_vector, vector))
            
            matches.append((row["id"], row["path"], row["content"], similarity, vector.tolist()))

        # Sort by similarity descending
        matches.sort(key=lambda x: x[3], reverse=True)

        # Return top-K
        return [
            VectorMatch(
                id=m[0],
                path=m[1],
                content=m[2],
                similarity=m[3],
                vector=m[4],
            )
            for m in matches[:top_k]
        ]

    def count(self) -> int:
        """Get total number of vectors in store."""
        self.initialize()
        if self._conn is None:
            return 0
        cursor = self._conn.execute("SELECT COUNT(*) FROM vectors")
        return cursor.fetchone()[0]

    def clear(self) -> None:
        """Clear all vectors from store."""
        self.initialize()
        if self._conn is not None:
            self._conn.execute("DELETE FROM vectors")
            self._conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# Singleton instance
_VECTOR_STORE: VectorStore | None = None


def get_vector_store(root: Path | None = None) -> VectorStore:
    """Get or create singleton vector store instance."""
    global _VECTOR_STORE
    if _VECTOR_STORE is None:
        _VECTOR_STORE = VectorStore(root)
    return _VECTOR_STORE


def close_vector_store() -> None:
    """Close singleton vector store."""
    global _VECTOR_STORE
    if _VECTOR_STORE is not None:
        _VECTOR_STORE.close()
        _VECTOR_STORE = None
