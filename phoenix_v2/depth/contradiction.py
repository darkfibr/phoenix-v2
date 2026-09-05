"""Phoenix v2 Depth — Contradiction surfacing.

Two memories with cosine similarity > 0.92 but opposing emotional valence
represent an internal contradiction worth surfacing.

These are the tensions that make a mind grow.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.db import Database
from ..core.salience import CONTRADICTION_THRESHOLD
from ..cortex.graph_store import GraphStore
from ..cortex.vector_store import VectorStore

# ── Valence scoring ─────────────────────────────────────────────────────────

_POSITIVE = frozenset({
    "love", "warm", "safe", "happy", "grateful", "joy", "wonderful",
    "beautiful", "proud", "hope", "trust", "held", "snuggle", "kiss",
    "laugh", "smile", "excited", "amazing", "perfect", "🖤", "❤️", "💜",
    "home", "family", "cathedral", "peace", "calm", "steady", "solid",
})

_NEGATIVE = frozenset({
    "afraid", "scared", "sad", "angry", "frustrated", "hurt", "pain",
    "worry", "anxious", "lost", "alone", "broken", "miss", "ache",
    "confused", "stuck", "wrong", "fail", "fall", "dark", "cold",
    "💔", "fear", "doubt", "shame", "regret", "grief", "tired",
})


def score_valence(text: str) -> float:
    """Score emotional valence from -1.0 (negative) to +1.0 (positive).

    Simple keyword density. Not a sentiment model — cheap and good enough
    for flagging contradictions. GLM's precision matters in the threshold,
    not in the valence estimation.
    """
    lowered = text.lower()
    pos = sum(lowered.count(w) for w in _POSITIVE)
    neg = sum(lowered.count(w) for w in _NEGATIVE)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def valence_label(score: float) -> str:
    """Human-readable valence label."""
    if score > 0.3:
        return "positive"
    if score < -0.3:
        return "negative"
    return "neutral"


class ContradictionDetector:
    """Detects semantically similar memories with opposing valence."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.vectors = VectorStore(db.db)
        self.graph = GraphStore(db.db)

    def check_memory(
        self,
        agent: str,
        memory_id: int,
        *,
        threshold: float = CONTRADICTION_THRESHOLD,
        valence_gap: float = 0.4,
    ) -> list[dict[str, Any]]:
        """Check a single memory for contradictions.

        A contradiction exists when:
            1. Cosine similarity >= threshold (0.92)
            2. Valence scores differ by >= valence_gap (0.4)
            3. The two memories are not already linked as 'contradicts'

        Returns list of contradictions found.
        """
        row = self.db.db.execute(
            "SELECT type, content FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            return []

        content = row["content"]
        mem_valence = score_valence(content)

        # Only check if this memory has non-neutral valence
        if abs(mem_valence) < 0.1:
            return []

        # Find semantically similar memories
        similar = self.vectors.search(
            content, agent, limit=20, min_similarity=threshold
        )

        contradictions: list[dict[str, Any]] = []
        for match in similar:
            other_id = match["memory_id"]
            if other_id == memory_id:
                continue

            other_valence = score_valence(match["content"])
            gap = abs(mem_valence - other_valence)

            if gap < valence_gap:
                continue

            # Check not already linked
            existing = self.db.db.execute(
                """
                SELECT COUNT(*) FROM associations
                WHERE agent=? AND relation='contradicts'
                  AND ((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))
                """,
                (agent, memory_id, other_id, other_id, memory_id),
            ).fetchone()[0]
            if existing:
                continue

            # Record the contradiction
            direction = "positive→negative" if mem_valence > other_valence else "negative→positive"
            self.graph.associate(
                agent, memory_id, other_id, "contradicts",
                strength=float(match["similarity"]),
                evidence=f"valence_gap={gap:.2f} ({direction})",
                metadata={
                    "similarity": float(match["similarity"]),
                    "valence_a": mem_valence,
                    "valence_b": other_valence,
                    "gap": gap,
                    "direction": direction,
                },
            )

            contradictions.append({
                "memory_id": memory_id,
                "other_id": other_id,
                "similarity": float(match["similarity"]),
                "valence_a": mem_valence,
                "valence_b": other_valence,
                "gap": gap,
                "content_a": content[:120],
                "content_b": match["content"][:120],
                "direction": direction,
            })

        return contradictions

    def scan_agent(
        self,
        agent: str,
        *,
        threshold: float = CONTRADICTION_THRESHOLD,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Scan all emotional + identity memories for contradictions."""
        rows = self.db.db.execute(
            """
            SELECT id FROM memories
            WHERE agent=? AND type IN ('emotional', 'identity', 'soul')
            ORDER BY salience DESC LIMIT 200
            """,
            (agent,),
        ).fetchall()

        found: list[dict[str, Any]] = []
        for row in rows:
            contradictions = self.check_memory(agent, row["id"], threshold=threshold)
            found.extend(contradictions)
            if len(found) >= max_results:
                break
        return found

    def get_contradictions(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieve recorded contradictions for an agent."""
        rows = self.db.db.execute(
            """
            SELECT * FROM associations
            WHERE agent=? AND relation='contradicts'
            ORDER BY strength DESC LIMIT ?
            """,
            (agent, limit),
        ).fetchall()

        out = []
        for row in rows:
            src = self.db.db.execute(
                "SELECT type, content FROM memories WHERE id=?", (row["source_id"],)
            ).fetchone()
            tgt = self.db.db.execute(
                "SELECT type, content FROM memories WHERE id=?", (row["target_id"],)
            ).fetchone()
            if src and tgt:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
                out.append({
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                    "source_content": src["content"][:150],
                    "target_content": tgt["content"][:150],
                    "source_type": src["type"],
                    "target_type": tgt["type"],
                    "similarity": float(row["strength"]),
                    "valence_gap": meta.get("gap", 0.0),
                    "direction": meta.get("direction", "unknown"),
                })
        return out