"""Phoenix v2 Depth — Predictive memory preloading.

Based on session patterns, predict which memories will be relevant.
Uses association strength + session co-occurrence to preload.

When an agent starts a new session, the predictive engine looks at:
    1. The current query/context
    2. Memories accessed in similar past sessions
    3. Association chains from seed memories
    4. Returns a preload set that the surface engine can merge with
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from ..core.db import Database
from ..cortex.episodic import EpisodicStore
from ..cortex.graph_store import GraphStore
from ..cortex.vector_store import VectorStore


class PredictiveEngine:
    """Predicts relevant memories for the next session."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.vectors = VectorStore(db.db)
        self.graph = GraphStore(db.db)
        self.episodic = EpisodicStore(db.db)

    def predict(
        self,
        agent: str,
        *,
        seed_query: str | None = None,
        seed_memory_ids: list[int] | None = None,
        lookback_sessions: int = 5,
        max_predictions: int = 10,
    ) -> list[dict[str, Any]]:
        """Predict which memories will be relevant in the next session.

        Combines:
            - Semantic similarity to seed query/memories
            - Association chains (memories linked to seed memories)
            - Session co-occurrence patterns (what tends to load with what)
        """
        scores: dict[int, float] = {}

        # 1. Semantic seed from query
        if seed_query and seed_query.strip():
            results = self.vectors.search(
                seed_query, agent, limit=15, min_similarity=0.2
            )
            for r in results:
                mid = r["memory_id"]
                scores[mid] = scores.get(mid, 0.0) + r["similarity"] * 0.4

        # 2. Association chains from seed memories
        if seed_memory_ids:
            for seed_id in seed_memory_ids:
                assocs = self.graph.get_associations(seed_id, min_strength=0.3)
                for a in assocs:
                    other = a["target_id"] if a["source_id"] == seed_id else a["source_id"]
                    boost = a["strength"] * 0.3
                    scores[other] = scores.get(other, 0.0) + boost

        # 3. Session co-occurrence — what memories tend to appear together?
        recent_sessions = self.episodic.recent_sessions(agent, limit=lookback_sessions)
        session_co: Counter[int] = Counter()

        for sess in recent_sessions:
            sess_id = sess["session_id"]
            mems = self.db.db.execute(
                "SELECT memory_id FROM session_memories WHERE session_id=?",
                (sess_id,),
            ).fetchall()
            for m in mems:
                session_co[m["memory_id"]] += 1

        # Frequently accessed memories get a boost
        for mid, count in session_co.items():
            boost = min(0.2, count * 0.05)  # cap at 0.2
            scores[mid] = scores.get(mid, 0.0) + boost

        # 4. Recent memories get a small recency boost
        now = time.time()
        rows = self.db.db.execute(
            """
            SELECT id, created_at FROM memories
            WHERE agent=? AND created_at > ?
            """,
            (agent, now - 86400 * 3),  # last 3 days
        ).fetchall()
        for row in rows:
            age_days = (now - row["created_at"]) / 86400
            recency = 1.0 / (1.0 + age_days)
            scores[row["id"]] = scores.get(row["id"], 0.0) + recency * 0.1

        # Sort by score and return top N
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        out = []
        for mid, score in ranked[:max_predictions]:
            row = self.db.db.execute(
                """
                SELECT id, type, content, summary, salience
                FROM memories WHERE id=?
                """,
                (mid,),
            ).fetchone()
            if row:
                out.append({
                    "memory_id": mid,
                    "type": row["type"],
                    "content": row["content"][:150],
                    "salience": float(row["salience"]),
                    "prediction_score": float(score),
                })
        return out

    def preload_report(self, agent: str, query: str | None = None) -> dict[str, Any]:
        """Human-readable preload report for debugging."""
        predictions = self.predict(agent, seed_query=query)
        return {
            "agent": agent,
            "query": query,
            "prediction_count": len(predictions),
            "predictions": predictions,
            "top_type": predictions[0]["type"] if predictions else None,
        }