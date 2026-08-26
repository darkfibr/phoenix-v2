"""Phoenix v2 Core — Embeddings layer.

Two backends:
    - SentenceTransformerEmbedder (primary, all-MiniLM-L6-v2)
    - HashingEmbedder (fallback, dependency-free)

Vector BLOB serialization ported from Cortex PR #2:
    - vector_to_bytes() / bytes_to_vector() / deserialize_vector()
    - float32, 10-50x faster than JSON on semantic search

Cortex version format: CTXV1 magic + uint32 count + float32 array
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import struct
from functools import lru_cache
from typing import Iterable

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:-]*|\d+(?:\.\d+)?")

# Embedding model constants — from K's paper
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Cortex vector BLOB magic (PR #2)
VECTOR_MAGIC = b"CTXV1"


# ── Vector serialization (port from Cortex) ──────────────────────────────────

def vector_to_bytes(vector: list[float] | None) -> bytes | None:
    """Serialize a float vector to a compact float32 BLOB.

    Format: VECTOR_MAGIC (5 bytes) + uint32 count (4 bytes) + float32 array.
    """
    if vector is None:
        return None
    values = [float(v) for v in vector]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Vectors must contain only finite values")
    return VECTOR_MAGIC + struct.pack("<I", len(values)) + struct.pack(
        f"<{len(values)}f", *values
    )


def bytes_to_vector(data: bytes | None) -> list[float]:
    """Deserialize a Cortex float32 BLOB back to a list of floats."""
    if not data:
        return []
    if data.startswith(VECTOR_MAGIC):
        if len(data) < len(VECTOR_MAGIC) + 4:
            raise ValueError("Truncated Cortex vector header")
        count = struct.unpack("<I", data[len(VECTOR_MAGIC):len(VECTOR_MAGIC) + 4])[0]
        payload = data[len(VECTOR_MAGIC) + 4:]
        if len(payload) != count * 4:
            raise ValueError("Cortex vector length does not match header")
    else:
        payload = data
        if len(payload) % 4:
            raise ValueError("Vector BLOB length must be divisible by four")
        count = len(payload) // 4
    values = list(struct.unpack(f"<{count}f", payload))
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Vector BLOB contains non-finite values")
    return values


def deserialize_vector(raw: bytes | str | None) -> list[float]:
    """Backward-compatible deserialization. Handles BLOB (new) and JSON (legacy)."""
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        if raw.lstrip().startswith((b"[", b"{")):
            try:
                import json as _json
                return _json.loads(raw)
            except (ValueError, TypeError):
                return []
        try:
            return bytes_to_vector(raw)
        except (ValueError, struct.error):
            return []
    if isinstance(raw, str):
        try:
            import json as _json
            return _json.loads(raw)
        except (ValueError, TypeError):
            return []
    return []


# ── Similarity ───────────────────────────────────────────────────────────────

def cosine(left: Iterable[float], right: Iterable[float]) -> float:
    """Cosine similarity between two equal-dim vectors."""
    a = list(left)
    b = list(right)
    if len(a) != len(b) or not a:
        return 0.0
    numerator = sum(x * y for x, y in zip(a, b))
    denominator = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return numerator / denominator if denominator else 0.0


# ── Embedder backends ────────────────────────────────────────────────────────

class HashingEmbedder:
    """Dependency-free fallback. Signed feature hashing over tokens + char n-grams."""

    name = "feature-hash-v1"

    def __init__(self, dimensions: int = EMBEDDING_DIM) -> None:
        self.dimensions = dimensions

    def encode_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        normalized = text.lower()
        features: list[str] = TOKEN_RE.findall(normalized)
        compact = re.sub(r"\s+", " ", normalized)
        features.extend(compact[i:i + 4] for i in range(max(0, len(compact) - 3)))
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "big")
            index = raw % self.dimensions
            sign = -1.0 if (raw >> 8) & 1 else 1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        return [self.encode_one(t) for t in texts]


class SentenceTransformerEmbedder:
    """Primary embedder. sentence-transformers/all-MiniLM-L6-v2 by default."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore
        self.model_name = model_name
        self.name = f"sentence-transformers:{model_name}"
        self.model = SentenceTransformer(model_name)

    def encode_one(self, text: str) -> list[float]:
        vector = self.model.encode([text], normalize_embeddings=True)[0]
        return [float(v) for v in vector]

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        vectors = self.model.encode(
            texts, batch_size=batch_size, normalize_embeddings=True
        )
        return [[float(v) for v in vec] for vec in vectors]


@lru_cache(maxsize=1)
def get_embedder() -> HashingEmbedder | SentenceTransformerEmbedder:
    """Return the cached embedder. Prefers sentence-transformers if available."""
    model_name = os.environ.get("PHOENIX_EMBEDDING_MODEL", DEFAULT_MODEL).strip()
    if model_name:
        try:
            return SentenceTransformerEmbedder(model_name)
        except Exception:
            pass
    return HashingEmbedder()