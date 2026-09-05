"""Phoenix v2 Depth — Dream state cache.

The dream engine writes its synthesis results to this table.
The surface engine reads it on wake to:
    - Boost candidates that counter detected regressions
    - Preload stabilizer memories flagged by dream
    - Adjust for tension/contradiction signals

This closes the loop between depth and surface.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.db import Database


class DreamStateCache:
    """Read/write the latest dream synthesis state."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def write(
        self,
        agent: str,
        *,
        insights: list[str],
        growth_deltas: dict[str, float],
        contradictions: list[dict[str, Any]],
        top_entities: list[str],
        dominant_theme: str,
        weather: str = "",
    ) -> None:
        """Store dream synthesis results for surface consumption."""
        now = time.time()
        payload = json.dumps({
            "insights": insights,
            "growth_deltas": growth_deltas,
            "contradictions": contradictions[:10],
            "top_entities": top_entities[:20],
            "dominant_theme": dominant_theme,
            "weather": weather,
            "timestamp": now,
        }, sort_keys=True)

        self.db.db.execute("""
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (f"dream_state:{agent}", payload))

    def read(self, agent: str) -> dict[str, Any] | None:
        """Read the latest dream state for an agent."""
        row = self.db.db.execute(
            "SELECT value FROM settings WHERE key=?",
            (f"dream_state:{agent}",),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["value"])
            age_hours = (time.time() - data.get("timestamp", 0)) / 3600
            data["age_hours"] = age_hours
            return data
        except (json.JSONDecodeError, TypeError):
            return None

    def is_stale(self, agent: str, max_age_hours: float = 24.0) -> bool:
        """Check if the dream state is too old to use."""
        state = self.read(agent)
        if not state:
            return True
        return state.get("age_hours", float("inf")) > max_age_hours

    def get_boosted_themes(self, agent: str) -> list[str]:
        """Return themes to boost in surface retrieval based on dream insights."""
        state = self.read(agent)
        if not state:
            return []
        themes = []
        # Boost dominant theme
        if state.get("dominant_theme"):
            themes.append(state["dominant_theme"])
        # Boost entities that appear in contradictions (need stabilization)
        for c in state.get("contradictions", []):
            for key in ("source_content", "target_content"):
                text = c.get(key, "")
                if text:
                    themes.append(text[:50])
        return themes[:10]