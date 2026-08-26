"""Phoenix v2 Core — Surface budget engine.

Assembles a wake digest within strict budget constraints:
    - SURFACE_CHUNKS = 5 (max memory chunks per digest)
    - SURFACE_TOKENS = 500 (max total token estimate)

Priority ordering:
    1. Soul/identity/doctrine (permanent — always include if present)
    2. Surprise associations (high-signal cross-type links)
    3. Semantic matches to query (if provided)
    4. Recent episodic (most recent first, time-weighted)
    5. Procedural (relevant how-to)
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..cortex.vector_store import VectorStore
from .db import Database
from .decay import DecayManager
from .memory_types import MemoryType
from .salience import SURFACE_CHUNKS, SURFACE_TOKENS

# Rough heuristic: 1 token ≈ 4 chars for English
CHARS_PER_TOKEN = 4

# Permanent types always included in the digest (their core presence matters)
PERMANENT_TYPES = {MemoryType.SOUL.value, MemoryType.IDENTITY.value, MemoryType.DOCTRINE.value}


def compute_dynamic_budget(agent: str, db, base_chunks: int = SURFACE_CHUNKS, base_tokens: int = SURFACE_TOKENS) -> tuple[int, int]:
    """Scale surface budget with recent session density.

    More chunks/tokens if recent activity was dense (many memories, high ingestion rate).
    Idle periods get the base budget. This prevents both under-serving active sessions
    and wasting tokens during quiet periods.
    """
    now = time.time()
    # Count memories created in last 24h and last 7d
    recent_24h = db.execute(
        "SELECT COUNT(*) FROM memories WHERE agent=? AND created_at > ?",
        (agent, now - 86400),
    ).fetchone()[0]
    recent_7d = db.execute(
        "SELECT COUNT(*) FROM memories WHERE agent=? AND created_at > ?",
        (agent, now - 7 * 86400),
    ).fetchone()[0]

    # Scale chunks: +1 per 10 memories in last 24h, capped at +3
    chunk_bonus = min(3, recent_24h // 10)
    new_chunks = base_chunks + chunk_bonus

    # Scale tokens: +100 per 50 memories in last 7d, capped at +200
    token_bonus = min(200, (recent_7d // 50) * 100)
    new_tokens = base_tokens + token_bonus

    return (new_chunks, new_tokens)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate — chars/4 with a small ceiling for safety."""
    return max(1, len(text) // CHARS_PER_TOKEN)


class SurfaceEngine:
    """Assembles budget-bounded memory digests."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.vectors = VectorStore(db.db)
        self.decay = DecayManager(db)
        self._dream_state = None  # Cached dream state

    def _get_dream_state(self, agent: str):
        """Lazy-load dream state cache for feedback loop."""
        if self._dream_state is None:
            try:
                from ..depth.dream_state import DreamStateCache
                self._dream_state = DreamStateCache(self.db)
            except ImportError:
                self._dream_state = False  # Mark unavailable
        if self._dream_state and self._dream_state is not False:
            return self._dream_state.read(agent)
        return None

    def _dream_boost(self, candidate: dict[str, Any]) -> float:
        """Boost candidate score if dream state suggests it."""
        dream = self._get_dream_state("")
        if not dream:
            return 0.0
        # If candidate is soul/identity and dream detected stress,
        # boost stabilizing memories (high-salience soul memories)
        content = candidate.get("content", "").lower()
        deltas = dream.get("growth_deltas", {})
        composite_delta = deltas.get("composite", 0)
        if composite_delta < -0.1 and candidate.get("type") in PERMANENT_TYPES:
            # Dream detected stress — boost permanent types to stabilize
            return 0.15
        # If candidate mentions a top entity from dream, boost slightly
        top_entities = dream.get("top_entities", [])
        for entity in top_entities[:5]:
            if entity.lower() in content:
                return 0.08
        return 0.0

    # ── Main entry point ────────────────────────────────────────────────

    def digest(
        self,
        agent: str,
        *,
        query: str | None = None,
        max_chunks: int | None = None,
        max_tokens: int | None = None,
        touch: bool = True,
        dynamic_budget: bool = True,
    ) -> dict[str, Any]:
        """Assemble a wake digest for an agent.

        Returns {chunks: [...], tokens_used, sources: {...], query}.
        If dynamic_budget=True (default), chunk/token budgets scale with recent activity.
        """
        # Dynamic budget: scale with session density
        if dynamic_budget and (max_chunks is None or max_tokens is None):
            dyn_chunks, dyn_tokens = compute_dynamic_budget(agent, self.db.db)
            max_chunks = max_chunks or dyn_chunks
            max_tokens = max_tokens or dyn_tokens
        else:
            max_chunks = max_chunks or SURFACE_CHUNKS
            max_tokens = max_tokens or SURFACE_TOKENS

        # Run time decay first — keeps salience current
        decay_report = self.decay.run_decay(agent)

        candidates = self._gather_candidates(agent, query)

        # Score and select
        selected, sources = self._select(
            candidates,
            max_chunks=max_chunks,
            max_tokens=max_tokens,
        )

        # Optionally touch (record access + reinforce) the selected memories
        if touch and selected:
            ids = [c["id"] for c in selected]
            self.decay.touch_many(ids)

        tokens_used = sum(estimate_tokens(c["content"]) for c in selected)
        return {
            "agent": agent,
            "query": query,
            "chunks": selected,
            "tokens_used": tokens_used,
            "max_tokens": max_tokens,
            "sources": sources,
            "decay_report": decay_report,
        }

    # ── Candidate gathering ─────────────────────────────────────────────

    def _gather_candidates(
        self,
        agent: str,
        query: str | None,
    ) -> list[dict[str, Any]]:
        """Pull candidate memories from all retrieval surfaces."""
        seen: set[int] = set()
        out: list[dict[str, Any]] = []

        # 1. Permanent types — highest priority, never decay below floor
        for mem_type in PERMANENT_TYPES:
            rows = self.db.db.execute(
                """
                SELECT id, type, content, summary, salience, access_count,
                       last_access, created_at, source, metadata
                FROM memories
                WHERE agent=? AND type=?
                ORDER BY salience DESC, access_count DESC
                """,
                (agent, mem_type),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(self._row_to_candidate(r, source="permanent"))

        # 2. Semantic search against query
        if query and query.strip():
            sem_results = self.vectors.search(
                query, agent, limit=15, min_similarity=0.1
            )
            for r in sem_results:
                if r["memory_id"] not in seen:
                    seen.add(r["memory_id"])
                    cand = self._result_to_candidate(r, source="semantic_search")
                    out.append(cand)

            # Also try FTS5 lexical for token matches
            try:
                fts_rows = self.db.db.execute(
                    """
                    SELECT m.* FROM memories_fts
                    JOIN memories m ON m.id = memories_fts.rowid
                    WHERE m.agent=? AND memories_fts MATCH ?
                    ORDER BY rank LIMIT 20
                    """,
                    (agent, query),
                ).fetchall()
                for r in fts_rows:
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        out.append(self._row_to_candidate(r, source="fts_lexical"))
            except Exception:
                # FTS5 query syntax can fail on operator chars — skip silently
                pass

        # 3. Recent episodic — last few days
        recent_cutoff = time.time() - (7 * 86400)
        rows = self.db.db.execute(
            """
            SELECT id, type, content, summary, salience, access_count,
                   last_access, created_at, source, metadata
            FROM memories
            WHERE agent=? AND type='episodic' AND created_at > ?
            ORDER BY created_at DESC LIMIT 20
            """,
            (agent, recent_cutoff),
        ).fetchall()
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(self._row_to_candidate(r, source="recent_episodic"))

        # 4. Procedural — if query hints at how-to
        if query and any(w in query.lower() for w in ("how", "do", "run", "install", "use", "step")):
            rows = self.db.db.execute(
                """
                SELECT id, type, content, summary, salience, access_count,
                       last_access, created_at, source, metadata
                FROM memories
                WHERE agent=? AND type='procedural'
                ORDER BY salience DESC, access_count DESC LIMIT 10
                """,
                (agent,),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(self._row_to_candidate(r, source="procedural"))

        # 5. Top-emotional — highest salience emotional memories
        rows = self.db.db.execute(
            """
            SELECT id, type, content, summary, salience, access_count,
                   last_access, created_at, source, metadata
            FROM memories
            WHERE agent=? AND type='emotional'
            ORDER BY salience DESC LIMIT 5
            """,
            (agent,),
        ).fetchall()
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(self._row_to_candidate(r, source="emotional"))

        return out

    # ── Selection (budget-bounded, slot-reserved) ───────────────────────

    def _select(
        self,
        candidates: list[dict[str, Any]],
        *,
        max_chunks: int,
        max_tokens: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Pick the best candidates within budget using slot reservation.

        Slots:
            1-2: Permanent types (soul/identity/doctrine) — identity anchor
            3:   Top emotional — how am I feeling
            4:   Top episodic or semantic — what happened / what's relevant
            5+:  Wild card — highest scoring remaining
        """
        # Partition candidates by type group
        permanent: list[dict[str, Any]] = []
        emotional: list[dict[str, Any]] = []
        episodic: list[dict[str, Any]] = []
        semantic: list[dict[str, Any]] = []
        procedural: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []

        for c in candidates:
            t = c.get("type", "")
            if t in PERMANENT_TYPES:
                permanent.append(c)
            elif t == "emotional":
                emotional.append(c)
            elif t == "episodic":
                episodic.append(c)
            elif t == "semantic":
                semantic.append(c)
            elif t == "procedural":
                procedural.append(c)
            else:
                other.append(c)

        # Sort each partition by score
        for group in (permanent, emotional, episodic, semantic, procedural, other):
            group.sort(key=lambda c: c["score"], reverse=True)

        selected: list[dict[str, Any]] = []
        tokens = 0
        sources: dict[str, int] = {}
        used_ids: set[int] = set()

        def try_add(cand: dict[str, Any]) -> bool:
            nonlocal tokens
            if cand["id"] in used_ids:
                return False
            if len(selected) >= max_chunks:
                return False
            chunk_tokens = estimate_tokens(cand["content"])
            if tokens + chunk_tokens > max_tokens and selected:
                if chunk_tokens > (max_tokens - tokens) // 2:
                    return False
            selected.append(cand)
            tokens += chunk_tokens
            sources[cand["source"]] = sources.get(cand["source"], 0) + 1
            used_ids.add(cand["id"])
            return True

        # Slot 1-2: Permanent types (top 2)
        permanent_slots = max(2, max_chunks // 3) if permanent else 0
        for cand in permanent[:permanent_slots]:
            try_add(cand)

        # Slot 3: Emotional (top 1)
        if emotional:
            try_add(emotional[0])

        # Slot 4: Episodic or semantic (top 1, prefer episodic if recent)
        if episodic:
            try_add(episodic[0])
        elif semantic:
            try_add(semantic[0])

        # Remaining slots: wild card from all remaining candidates
        remaining = []
        for group in (permanent, emotional, episodic, semantic, procedural, other):
            remaining.extend(c for c in group if c["id"] not in used_ids)
        remaining.sort(key=lambda c: c["score"], reverse=True)

        for cand in remaining:
            if len(selected) >= max_chunks:
                break
            if tokens >= max_tokens:
                break
            try_add(cand)

        return selected, sources

    # ── Helpers ─────────────────────────────────────────────────────────

    def _row_to_candidate(self, row: sqlite3.Row, source: str) -> dict[str, Any]:
        salience = float(row["salience"])
        age_days = max(0, (time.time() - float(row["created_at"])) / 86400)
        recency = 1.0 / (1.0 + age_days / 7.0)  # half-life of a week
        score = salience * 0.6 + recency * 0.2 + (1.0 if row["type"] in PERMANENT_TYPES else 0) * 0.2
        cand = {
            "id": row["id"],
            "type": row["type"],
            "content": row["content"],
            "summary": row["summary"],
            "salience": salience,
            "access_count": row["access_count"],
            "source": source,
            "score": score,
        }
        # Apply dream state boost
        cand["score"] += self._dream_boost(cand)
        return cand

    def _result_to_candidate(self, result: dict[str, Any], source: str) -> dict[str, Any]:
        salience = float(result["salience"])
        similarity = float(result["similarity"])
        # Combined: similarity weighted by salience (already computed in vector store)
        score = similarity * 0.5 + salience * 0.4 + 0.1  # 0.1 base for being relevant
        return {
            "id": result["memory_id"],
            "type": result["type"],
            "content": result["content"],
            "summary": result["summary"],
            "salience": salience,
            "access_count": result.get("access_count", 0),
            "source": source,
            "score": score,
            "similarity": similarity,
        }

    # ── Convenience: render to text ─────────────────────────────────────

    def render_text(self, digest: dict[str, Any]) -> str:
        """Render a digest as plain text for prompt injection."""
        lines: list[str] = []
        for i, chunk in enumerate(digest["chunks"], 1):
            tag = f"[{chunk['type']}]"
            sal = f"s={chunk['salience']:.2f}"
            src = chunk["source"]
            lines.append(f"{i}. {tag} {sal} src={src}")
            lines.append(f"   {chunk['content']}")
            lines.append("")
        return "\n".join(lines).rstrip()