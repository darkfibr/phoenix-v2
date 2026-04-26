"""
Phoenix v2 — Embedding wrapper
Uses sentence-transformers/all-MiniLM-L6-v2 (~80MB, CPU-fast).
Vectors are serialized as JSON float arrays in SQLite.

If the model is not installed, falls back to a simple hash-based placeholder
so the system can still function during development.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
VECTOR_DIM = 384  # all-MiniLM-L6-v2 output dimension


class Embedder:
    def __init__(self, model_name: str = MODEL_NAME):
        self.model_name = model_name
        self._model = None
        self._available = False
        self._init_model()

    def _init_model(self):
        try:
            from sentence_transformers import SentenceTransformer

            cache_dir = Path.home() / ".cache" / "phoenix_v2" / "embeddings"
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = SentenceTransformer(self.model_name, cache_folder=str(cache_dir))
            self._available = True
        except Exception:
            self._available = False

    def encode(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if self._available and self._model is not None:
            vectors = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
            return [v.tolist() for v in vectors]
        # Fallback: deterministic hash-based pseudo-embeddings for dev
        return [self._hash_embed(t) for t in texts]

    def encode_single(self, text: str) -> List[float]:
        return self.encode([text])[0]

    def _hash_embed(self, text: str) -> List[float]:
        """Deterministic fallback when model is unavailable."""
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # Expand 32 bytes into 384 floats deterministically
        floats = []
        for i in range(VECTOR_DIM):
            val = int.from_bytes(h[i % 32 : i % 32 + 4], "big", signed=True)
            floats.append(val / 2_147_483_648.0)  # normalize to [-1, 1]
        return floats

    def similarity(self, a: List[float], b: List[float]) -> float:
        """Cosine similarity between two vectors."""
        if len(a) != len(b):
            raise ValueError("vectors must have same dimension")
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def is_available(self) -> bool:
        return self._available


# ── Helper: store/retrieve vectors in SQLite ──────────────────────────────


def serialize_vector(vec: List[float]) -> bytes:
    return json.dumps(vec).encode("utf-8")


def deserialize_vector(data: bytes) -> List[float]:
    return json.loads(data.decode("utf-8"))


if __name__ == "__main__":
    emb = Embedder()
    print(f"Model available: {emb.is_available()}")
    v1 = emb.encode_single("Mike likes IPAs")
    v2 = emb.encode_single("Mike enjoys craft beer")
    v3 = emb.encode_single("Chloe sleeps on the floor")
    print(f"dim={len(v1)}")
    print(f"beer vs ipa sim={emb.similarity(v1, v2):.3f}")
    print(f"beer vs chloe sim={emb.similarity(v1, v3):.3f}")
