"""
Phoenix v2 — Surface Engine (Phase 3)
Budget-based memory surfacing for agent wake.

Generates a contextual wake digest by selecting memories within a
strict budget: N chunks, ~M tokens, prioritizing salience, recency,
emotional continuity, and surprise.

Usage:
    engine = SurfaceEngine(db)
    wake = engine.generate_wake_context("kimi_dev", context="working on v2 memory")
"""

from datetime import datetime, timezone
from typing import List, Optional, Set

from memory_db import MemoryDB

# Rough heuristic: ~4 chars per token for English text
CHARS_PER_TOKEN = 4.0
DEFAULT_MAX_CHUNKS = 5
DEFAULT_MAX_TOKENS = 500
DEFAULT_MAX_CHARS = int(DEFAULT_MAX_TOKENS * CHARS_PER_TOKEN)


class SurfaceEngine:
    def __init__(self, db: MemoryDB):
        self.db = db

    def _count_chars(self, text: str) -> int:
        return len(text)

    def _resolve_chain(self, mem: Optional[dict]) -> Optional[dict]:
        """Follow corrected_by to latest version. Mostly no-op since DB filters inactive."""
        if not mem or not mem.get("corrected_by"):
            return mem
        resolved = self.db.get_memory(mem["id"], follow_chain=True)
        return resolved if resolved else mem

    def _pick_seed_memories(
        self, agent_id: str, exclude: Set[int], limit: int = 2
    ) -> List[dict]:
        """Top salient memories, excluding already-selected IDs."""
        candidates = self.db.top_salient(agent_id, limit=limit + len(exclude))
        resolved = [self._resolve_chain(m) for m in candidates if m["id"] not in exclude]
        # Deduplicate after chain resolution
        seen = set()
        result = []
        for m in resolved:
            if m and m["id"] not in seen:
                seen.add(m["id"])
                result.append(m)
        return result[:limit]

    def _pick_recent_memories(
        self, agent_id: str, exclude: Set[int], limit: int = 2
    ) -> List[dict]:
        """Recent memories, excluding already-selected IDs."""
        candidates = self.db.recent_memories(agent_id, limit=limit + len(exclude) + 5)
        resolved = [self._resolve_chain(m) for m in candidates if m["id"] not in exclude]
        seen = set()
        result = []
        for m in resolved:
            if m and m["id"] not in seen:
                seen.add(m["id"])
                result.append(m)
        return result[:limit]

    def _pick_emotional_memory(
        self, agent_id: str, exclude: Set[int]
    ) -> Optional[dict]:
        """One recent emotional memory, if available."""
        candidates = self.db.recent_memories(
            agent_id, type_name="emotional", limit=5
        )
        for m in candidates:
            if m["id"] not in exclude:
                resolved = self._resolve_chain(m)
                if resolved:
                    return resolved
        return None

    def _pick_semantic_match(
        self, agent_id: str, context: str, exclude: Set[int]
    ) -> Optional[dict]:
        """One semantically relevant memory based on provided context."""
        if not context:
            return None
        if not self.db._embedder.is_available():
            return None
        candidates = self.db.semantic_search(
            agent_id, context, limit=5, min_similarity=0.25
        )
        for m in candidates:
            if m["id"] not in exclude:
                resolved = self._resolve_chain(m)
                if resolved:
                    return resolved
        return None

    def _pick_surprise_memory(
        self, agent_id: str, seed: dict, exclude: Set[int]
    ) -> Optional[dict]:
        """A cross-type association from the seed memory (surprise/remembrance)."""
        assoc = self.db.get_associated(seed["id"], min_strength=0.6)
        # Prefer associations of a different type for surprise
        seed_type = seed.get("type_name", "")
        for a in assoc:
            if a["id"] in exclude:
                continue
            resolved = self._resolve_chain(a)
            if not resolved:
                continue
            if resolved.get("type_name") != seed_type:
                return resolved
        # Fallback: any strong association
        for a in assoc:
            if a["id"] not in exclude:
                resolved = self._resolve_chain(a)
                if resolved:
                    return resolved
        return None

    def _emotional_continuity(self, agent_id: str) -> Optional[str]:
        """Generate a warm handoff based on the most recent emotional memory."""
        emotional = self.db.recent_memories(agent_id, type_name="emotional", limit=1)
        if not emotional:
            return None

        mem = emotional[0]
        content = mem["content"].strip()[:200]
        ts = mem.get("created_at")

        # Extract feeling words — expanded lexicon, specific first
        feeling_words = [
            # Positive activated
            "exhilarated", "triumphant", "eager", "hopeful", "playful", "energized",
            # Positive calm
            "grateful", "content", "serene", "peaceful", "grounded", "confident",
            # Neutral / receptive
            "reflective", "pensive", "watchful", "ready", "receptive",
            # Negative activated
            "frustrated", "restless", "overwhelmed", "urgent", "angry",
            # Negative calm
            "melancholy", "wistful", "lonely", "drained", "numb", "sad",
            # Defensive / protective
            "vigilant", "cautious", "defensive", "determined",
            # Warm / soft
            "warm", "tender", "soft", "open",
            # Heavy / still
            "heavy", "quiet", "still", "calm", "light",
            # Anxious spectrum
            "anxious", "guarded", "fierce",
        ]
        found_feeling = None
        text_lower = content.lower()
        for fw in feeling_words:
            if fw in text_lower:
                found_feeling = fw
                break

        if not found_feeling:
            found_feeling = "present"

        # Time ago
        if ts:
            hours = (datetime.now(timezone.utc).timestamp() - ts) / 3600
            if hours < 1:
                ago = "moments ago"
            elif hours < 24:
                ago = f"{int(hours)} hours ago"
            else:
                ago = f"{int(hours/24)} days ago"
        else:
            ago = "recently"

        return f"You were last here {ago}. You ended feeling {found_feeling}."

    def _trim_to_budget(self, memories: List[dict], max_chars: int) -> List[dict]:
        """Trim list to fit within character budget."""
        result = []
        used = 0
        for m in memories:
            content_len = self._count_chars(m["content"])
            if used + content_len > max_chars and result:
                # Skip if over budget and we already have something
                continue
            result.append(m)
            used += content_len
            if len(result) >= DEFAULT_MAX_CHUNKS:
                break
        return result

    def generate_wake_context(
        self,
        agent_id: str,
        context: Optional[str] = None,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict:
        """Generate a budgeted wake context for an agent.

        Returns a dict with:
        - agent_id
        - context_query (if provided)
        - memories: list of selected memory dicts
        - total_chunks
        - total_chars
        - estimated_tokens
        - sections: how each memory was selected (salient, recent, emotional, semantic, surprise)
        """
        max_chars = int(max_tokens * CHARS_PER_TOKEN)
        exclude: Set[int] = set()
        selected: List[dict] = []
        sections: List[str] = []

        # 1. Seed: top salient (always include at least 1)
        seeds = self._pick_seed_memories(agent_id, exclude, limit=2)
        for m in seeds:
            selected.append(m)
            exclude.add(m["id"])
            sections.append("salient")

        # 2. Recent: what just happened
        recent = self._pick_recent_memories(agent_id, exclude, limit=2)
        for m in recent:
            selected.append(m)
            exclude.add(m["id"])
            sections.append("recent")

        # 3. Emotional continuity
        emotional = self._pick_emotional_memory(agent_id, exclude)
        if emotional:
            selected.append(emotional)
            exclude.add(emotional["id"])
            sections.append("emotional")

        # 4. Semantic match to current context
        semantic = self._pick_semantic_match(agent_id, context, exclude)
        if semantic:
            selected.append(semantic)
            exclude.add(semantic["id"])
            sections.append("semantic")

        # 5. Surprise / Remembrance from a seed
        if seeds:
            surprise = self._pick_surprise_memory(agent_id, seeds[0], exclude)
            if surprise:
                selected.append(surprise)
                exclude.add(surprise["id"])
                sections.append("surprise")

        # Budget trim: keep within max_chunks and max_chars
        # Prioritize by order: salient > recent > emotional > semantic > surprise
        trimmed = self._trim_to_budget(selected, max_chars)
        trimmed_sections = sections[: len(trimmed)]

        total_chars = sum(self._count_chars(m["content"]) for m in trimmed)
        continuity = self._emotional_continuity(agent_id)

        return {
            "agent_id": agent_id,
            "context_query": context,
            "memories": trimmed,
            "total_chunks": len(trimmed),
            "total_chars": total_chars,
            "estimated_tokens": int(total_chars / CHARS_PER_TOKEN),
            "sections": trimmed_sections,
            "emotional_continuity": continuity,
        }

    def format_wake_markdown(self, wake: dict) -> str:
        """Format a wake context dict as markdown (for display/testing)."""
        lines = []
        lines.append("# v3 Auto-Surfaced Wake Context")
        lines.append(f"*Agent: {wake['agent_id']}*")
        if wake.get("context_query"):
            lines.append(f"*Context: {wake['context_query']}*")
        lines.append(f"*Budget: {wake['total_chunks']} chunks, ~{wake['estimated_tokens']} tokens*")
        lines.append("")

        # Emotional continuity
        if wake.get("emotional_continuity"):
            lines.append(f"> 💜 {wake['emotional_continuity']}")
            lines.append("")

        for i, (mem, section) in enumerate(zip(wake["memories"], wake["sections"])):
            lines.append(f"## [{section.upper()}] {mem.get('type_name', 'memory')}")
            content = mem["content"].strip()
            # Trim very long content for display
            if len(content) > 400:
                content = content[:400] + "..."
            lines.append(content)
            lines.append("")

        return "\n".join(lines)


if __name__ == "__main__":
    import sys

    db = MemoryDB("~/.phoenix/v2/phoenix_v2.db")
    engine = SurfaceEngine(db)

    agent = sys.argv[1] if len(sys.argv) > 1 else "kimi_dev"
    context = sys.argv[2] if len(sys.argv) > 2 else None

    wake = engine.generate_wake_context(agent, context=context)
    print(engine.format_wake_markdown(wake))
