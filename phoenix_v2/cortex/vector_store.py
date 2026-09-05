"""Phoenix v2 Cortex — Vector store.

Binary BLOB storage with cosine similarity search.
Uses Cortex PR #2 format for 10-50x faster semantic search vs JSON.

Schema: memory_vectors(memory_id, vector BLOB, dim, model, created_at)
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..core.embeddings import (
    EMBEDDING_DIM,
    bytes_to_vector,
    deserialize_vector,
    get_embedder,
    vector_to_bytes,
)


class VectorStore:
    """Manages vector storage and similarity search for an agent."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.embedder = get_embedder()

    # ── Ingest ────────────────────────────────────────────────────────────

    def store(
        self,
        memory_id: int,
        text: str,
        *,
        model: str | None = None,
    ) -> list[float]:
        """Encode text and store its vector. Returns the raw vector."""
        vector = self.embedder.encode_one(text)
        model_name = model or self.embedder.name
        self.db.execute(
            """
            INSERT INTO memory_vectors(memory_id, vector, dim, model, created_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
              vector=excluded.vector, dim=excluded.dim, model=excluded.model,
              created_at=excluded.created_at
            """,
            (memory_id, vector_to_bytes(vector), len(vector), model_name, time.time()),
        )
        self.db.commit()
        return vector

    def store_batch(
        self,
        items: list[tuple[int, str]],
        *,
        model: str | None = None,
        batch_size: int = 32,
    ) -> list[list[float]]:
        """Encode and store vectors for many memories. items = [(memory_id, text), ...]"""
        if not items:
            return []
        texts = [text for _, text in items]
        vectors = self.embedder.encode(texts, batch_size=batch_size)
        model_name = model or self.embedder.name
        now = time.time()
        with self.db:
            for (memory_id, _), vec in zip(items, vectors):
                self.db.execute(
                    """
                    INSERT INTO memory_vectors(memory_id, vector, dim, model, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                      vector=excluded.vector, dim=excluded.dim, model=excluded.model,
                      created_at=excluded.created_at
                    """,
                    (memory_id, vector_to_bytes(vec), len(vec), model_name, now),
                )
        return vectors

    # ── Retrieve ─────────────────────────────────────────────────────────

    def get(self, memory_id: int) -> list[float]:
        """Fetch a single vector by memory_id."""
        row = self.db.execute(
            "SELECT vector FROM memory_vectors WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if not row:
            return []
        return deserialize_vector(row["vector"])

    def all_vectors(self, agent: str | None = None) -> list[dict[str, Any]]:
        """Iterate all stored vectors for an agent (or all if None)."""
        if agent:
            rows = self.db.execute(
                """
                SELECT mv.memory_id, mv.vector, mv.dim, mv.model, m.agent
                FROM memory_vectors mv
                JOIN memories m ON m.id = mv.memory_id
                WHERE m.agent = ?
                """,
                (agent,),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT memory_id, vector, dim, model FROM memory_vectors"
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "vector": deserialize_vector(row["vector"]),
                "dim": row["dim"],
                "model": row["model"],
            }
            for row in rows
        ]

    # ── Search ───────────────────────────────────────────────────────────

    def cosine(self, a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors."""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    def search(
        self,
        query: str | list[float],
        agent: str,
        *,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Top-k semantic search for an agent.

        Returns list of {memory_id, similarity, salience, content, type}.
        """
        if isinstance(query, str):
            qvec = self.embedder.encode_one(query)
        else:
            qvec = query

        rows = self.db.execute(
            """
            SELECT mv.memory_id, mv.vector, m.content, m.summary, m.type,
                   m.salience, m.access_count
            FROM memory_vectors mv
            JOIN memories m ON m.id = mv.memory_id
            WHERE m.agent = ?
            """,
            (agent,),
        ).fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            vec = deserialize_vector(row["vector"])
            sim = self.cosine(qvec, vec)
            if sim < min_similarity:
                continue
            # Combined score: similarity * salience
            combined = sim * row["salience"]
            scored.append({
                "memory_id": row["memory_id"],
                "similarity": sim,
                "combined": combined,
                "salience": row["salience"],
                "content": row["content"],
                "summary": row["summary"],
                "type": row["type"],
                "access_count": row["access_count"],
            })

        scored.sort(key=lambda x: x["combined"], reverse=True)
        return scored[:limit]

    # ── Maintenance ──────────────────────────────────────────────────────

    def stats(self, agent: str) -> dict[str, Any]:
        """Storage stats for this agent's vectors."""
        row = self.db.execute(
            """
            SELECT COUNT(*) AS n, MIN(dim) AS min_dim, MAX(dim) AS max_dim
            FROM memory_vectors mv
            JOIN memories m ON m.id = mv.memory_id
            WHERE m.agent = ?
            """,
            (agent,),
        ).fetchone()
        return {
            "agent": agent,
            "vector_count": int(row["n"]) if row else 0,
            "min_dim": row["min_dim"] if row else None,
            "max_dim": row["max_dim"] if row else None,
        }