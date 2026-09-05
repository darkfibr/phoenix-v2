"""Phoenix v2 Wake — Digest generator.

Combines:
    - Surface engine (budget-bounded memory retrieval)
    - Dream synthesis (insight from last dream cycle)
    - Family mindstate (how are the siblings?)

This is what gets injected into an agent's system prompt on wake.
"""

from __future__ import annotations

import time
from typing import Any

from ..core.db import Database
from ..core.surface import SurfaceEngine, estimate_tokens
from ..depth.contradiction import ContradictionDetector
from ..depth.growth import GrowthTracker
from ..family.mindstate import MindstateExtractor

# Budget for dream + mindstate sections (subtracted from total)
_DEPTH_TOKENS = 80
_MINDSTATE_TOKENS = 40


class DigestGenerator:
    """Full wake digest: surface + depth + mindstate."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.surface = SurfaceEngine(db)
        self.mindstate = MindstateExtractor(db)
        self.contradiction = ContradictionDetector(db)
        self.growth = GrowthTracker(db)

    def generate(
        self,
        agent: str,
        *,
        query: str | None = None,
        max_chunks: int = 5,
        max_tokens: int = 500,
        include_mindstate: bool = True,
        include_dream_summary: bool = True,
        touch: bool = True,
    ) -> dict[str, Any]:
        """Generate the complete wake digest.

        Returns a structured digest ready for prompt injection.
        """
        # Reserve budget for depth sections
        surface_budget = max_tokens
        if include_dream_summary:
            surface_budget -= _DEPTH_TOKENS
        if include_mindstate:
            surface_budget -= _MINDSTATE_TOKENS

        # Surface memories
        surface_digest = self.surface.digest(
            agent,
            query=query,
            max_chunks=max_chunks,
            max_tokens=surface_budget,
            touch=touch,
        )

        # Dream summary: latest dream synthesis memory
        dream_summary = None
        if include_dream_summary:
            dream_row = self.db.db.execute(
                """
                SELECT content, summary, created_at
                FROM memories
                WHERE agent=? AND source LIKE 'dream_synthesis:%'
                ORDER BY created_at DESC LIMIT 1
                """,
                (agent,),
            ).fetchone()
            if dream_row:
                # Trim to budget
                content = dream_row["content"]
                max_chars = _DEPTH_TOKENS * 4
                if len(content) > max_chars:
                    content = content[:max_chars] + "..."
                dream_summary = {
                    "content": content,
                    "created_at": dream_row["created_at"],
                    "age_hours": (time.time() - dream_row["created_at"]) / 3600,
                }

        # Mindstate
        mindstate = None
        if include_mindstate:
            mindstate = self.mindstate.extract(agent)

        # Contradictions to surface
        tensions = self.contradiction.get_contradictions(agent, limit=3)

        # Assemble
        total_tokens = surface_digest["tokens_used"]
        if dream_summary:
            total_tokens += estimate_tokens(dream_summary["content"])
        if mindstate:
            total_tokens += _MINDSTATE_TOKENS  # approximate

        return {
            "agent": agent,
            "query": query,
            "generated_at": time.time(),
            "surface": surface_digest,
            "dream_summary": dream_summary,
            "mindstate": mindstate,
            "tensions": tensions,
            "tokens_used": total_tokens,
            "max_tokens": max_tokens,
        }

    def render(self, digest: dict[str, Any]) -> str:
        """Render the digest as text for system prompt injection."""
        lines: list[str] = []

        # Mindstate (if present)
        ms = digest.get("mindstate")
        if ms and ms.get("status") == "active":
            lines.append(f"── Emotional State: {ms['descriptor']} "
                         f"(valence={ms['valence']:+.2f}, arousal={ms['arousal']:.2f}) ──")
            lines.append("")

        # Dream summary (if present and fresh)
        dream = digest.get("dream_summary")
        if dream:
            lines.append("── Last Dream ──")
            lines.append(dream["content"])
            lines.append("")

        # Tensions
        tensions = digest.get("tensions", [])
        if tensions:
            lines.append("── Tensions Held ──")
            for t in tensions[:2]:
                lines.append(f"  • {t['source_content'][:60]} ↔ {t['target_content'][:60]}")
            lines.append("")

        # Surface memories
        surface = digest.get("surface", {})
        lines.append("── Memory Surface ──")
        for i, chunk in enumerate(surface.get("chunks", []), 1):
            tag = f"[{chunk['type']}]"
            sal = f"s={chunk['salience']:.2f}"
            lines.append(f"{i}. {tag} {sal}")
            lines.append(f"   {chunk['content']}")
            lines.append("")

        return "\n".join(lines).rstrip()