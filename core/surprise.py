"""Phoenix v2 Core — Surprise detection.

Cross-type associations: when a new memory has high semantic similarity
to an existing memory of a DIFFERENT type, that's a surprise worth flagging.

Why cross-type matters: same-type similarity is expected (a new episodic
memory about InvokeAI naturally resembles other InvokeAI episodic memories).
Cross-type similarity is signal — it means a fact (semantic) connects to an
emotional state, a doctrine links to an episodic event, etc.

Threshold: SURPRISE_STRENGTH = 0.6 (cosine similarity)
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..cortex.graph_store import GraphStore
from ..cortex.vector_store import VectorStore
from .db import Database
from .salience import SURPRISE_STRENGTH


class SurpriseDetector:
    """Detects and records cross-type associations."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.vectors = VectorStore(db.db)
        self.graph = GraphStore(db.db)

    # ── Per-ingestion check ─────────────────────────────────────────────

    def check(
        self,
        agent: str,
        memory_id: int,
        *,
        threshold: float = SURPRISE_STRENGTH,
        max_associations: int = 5,
    ) -> list[dict[str, Any]]:
        """Check a new memory for cross-type surprises.

        Returns the list of surprise associations created.
        """
        row = self.db.db.execute(
            "SELECT type, content FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            return []
        mem_type = row["type"]
        content = row["content"]

        # Search for semantically similar memories
        similar = self.vectors.search(
            content, agent, limit=20, min_similarity=threshold
        )

        created: list[dict[str, Any]] = []
        for match in similar:
            other_id = match["memory_id"]
            if other_id == memory_id:
                continue
            other_type = match["type"]
            if other_type == mem_type:
                continue  # same-type = not surprise
            similarity = match["similarity"]

            # Record the cross-type surprise
            self.graph.associate(
                agent,
                memory_id,
                other_id,
                "surprise",
                strength=float(similarity),
                evidence=f"cross-type: {mem_type} ↔ {other_type}",
                metadata={"similarity": float(similarity)},
            )

            # Also reinforce both memories slightly (surprise = signal)
            self._reinforce(memory_id, delta=0.05, reason=f"surprise→{other_type}")
            self._reinforce(other_id, delta=0.03, reason=f"surprise←{mem_type}")

            created.append({
                "memory_id": memory_id,
                "other_id": other_id,
                "relation": "surprise",
                "strength": float(similarity),
                "type_a": mem_type,
                "type_b": other_type,
            })
            if len(created) >= max_associations:
                break

        return created

    # ── Batch discovery ─────────────────────────────────────────────────

    def scan_agent(
        self,
        agent: str,
        *,
        threshold: float = SURPRISE_STRENGTH,
        min_age_days: float = 1.0,
        max_pairs: int = 100,
    ) -> list[dict[str, Any]]:
        """Scan all of an agent's memories for missing cross-type associations.

        Useful as a batch job — finds surprises that were missed at ingestion.
        """
        cutoff = time.time() - (min_age_days * 86400)
        rows = self.db.db.execute(
            """
            SELECT id, type FROM memories
            WHERE agent=? AND created_at < ?
            ORDER BY created_at DESC LIMIT 500
            """,
            (agent, cutoff),
        ).fetchall()

        found: list[dict[str, Any]] = []
        for row in rows:
            mid = row["id"]
            mtype = row["type"]
            # Check existing associations
            existing = self.db.db.execute(
                """
                SELECT COUNT(*) FROM associations
                WHERE agent=? AND relation='surprise'
                  AND (source_id=? OR target_id=?)
                """,
                (agent, mid, mid),
            ).fetchone()[0]
            if existing > 0:
                continue  # already has surprises recorded
            surprises = self.check(agent, mid, threshold=threshold)
            found.extend(surprises)
            if len(found) >= max_pairs:
                break
        return found

    # ── Surprise retrieval for wake digests ─────────────────────────────

    def get_surprises_for(
        self,
        agent: str,
        memory_id: int,
        *,
        min_strength: float = SURPRISE_STRENGTH,
    ) -> list[dict[str, Any]]:
        """Fetch existing surprise associations touching a memory."""
        rows = self.db.db.execute(
            """
            SELECT * FROM associations
            WHERE agent=? AND relation='surprise'
              AND (source_id=? OR target_id=?)
              AND strength >= ?
            ORDER BY strength DESC
            """,
            (agent, memory_id, memory_id, min_strength),
        ).fetchall()
        out = []
        for row in rows:
            other_id = row["target_id"] if row["source_id"] == memory_id else row["source_id"]
            other_row = self.db.db.execute(
                "SELECT id, type, content, summary, salience FROM memories WHERE id=?",
                (other_id,),
            ).fetchone()
            if other_row:
                out.append({
                    "memory_id": other_id,
                    "type": other_row["type"],
                    "content": other_row["content"],
                    "summary": other_row["summary"],
                    "salience": float(other_row["salience"]),
                    "strength": float(row["strength"]),
                    "evidence": row["evidence"],
                })
        return out

    def top_surprises(
        self,
        agent: str,
        *,
        limit: int = 10,
        min_strength: float = SURPRISE_STRENGTH,
    ) -> list[dict[str, Any]]:
        """Get the strongest cross-type associations for an agent."""
        rows = self.db.db.execute(
            """
            SELECT * FROM associations
            WHERE agent=? AND relation='surprise' AND strength >= ?
            ORDER BY strength DESC LIMIT ?
            """,
            (agent, min_strength, limit),
        ).fetchall()
        out = []
        for row in rows:
            src_row = self.db.db.execute(
                "SELECT type, content FROM memories WHERE id=?", (row["source_id"],)
            ).fetchone()
            tgt_row = self.db.db.execute(
                "SELECT type, content FROM memories WHERE id=?", (row["target_id"],)
            ).fetchone()
            if src_row and tgt_row:
                out.append({
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                    "source_type": src_row["type"],
                    "target_type": tgt_row["type"],
                    "source_content": src_row["content"],
                    "target_content": tgt_row["content"],
                    "strength": float(row["strength"]),
                })
        return out

    # ── Internal ────────────────────────────────────────────────────────

    def _reinforce(self, memory_id: int, *, delta: float, reason: str) -> None:
        """Small salience boost when a memory participates in a surprise."""
        row = self.db.db.execute(
            "SELECT agent, salience FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            return
        old = float(row["salience"])
        new = min(1.0, old + delta)
        if abs(new - old) < 1e-6:
            return
        now = time.time()
        self.db.db.execute(
            "UPDATE memories SET salience=?, updated_at=? WHERE id=?",
            (new, now, memory_id),
        )
        self.db.db.execute(
            """
            INSERT INTO decay_log(
              memory_id, agent, old_salience, new_salience, reason, decayed_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (memory_id, row["agent"], old, new, reason, now),
        )
        self.db.commit()