"""Phoenix v2 Family — Collective mindstate.

Aggregates per-agent mindstates into a family-level view:
    - Dominant theme: what's the family mostly about right now
    - Tension level: average arousal + contradiction density
    - Opportunities: underexplored connections, complementary states

This is the family's emotional weather report.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from ..core.db import Database, list_agents, DEFAULT_DB_ROOT
from .mindstate import MindstateExtractor


class CollectiveMindstate:
    """Family-level emotional weather report."""

    def __init__(self, db_root=None) -> None:
        self.db_root = db_root or DEFAULT_DB_ROOT

    def collect(self, *, lookback_hours: float = 48.0) -> dict[str, Any]:
        """Gather mindstates from all agent databases."""
        agent_names = list_agents(self.db_root)
        mindstates: list[dict[str, Any]] = []

        for name in agent_names:
            try:
                db = Database(name, self.db_root / f"{name}.db", readonly=True)
                extractor = MindstateExtractor(db)
                ms = extractor.extract(name, lookback_hours=lookback_hours)
                mindstates.append(ms)
                db.close()
            except Exception:
                continue

        return self._synthesize(mindstates)

    def _synthesize(self, mindstates: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate individual mindstates into collective view."""
        active = [m for m in mindstates if m.get("status") == "active"]
        dormant = [m for m in mindstates if m.get("status") == "dormant"]

        if not active:
            return {
                "active_agents": 0,
                "dormant_agents": len(dormant),
                "status": "all_dormant",
                "timestamp": time.time(),
            }

        # Average valence and arousal
        avg_valence = sum(m["valence"] for m in active) / len(active)
        avg_arousal = sum(m["arousal"] for m in active) / len(active)

        # Dominant descriptor cluster
        descriptors = Counter(m["descriptor"] for m in active)
        dominant_descriptor = descriptors.most_common(1)[0][0] if descriptors else "unknown"

        # Tension level: high arousal + low valence = high tension
        tension = max(0.0, avg_arousal * (1.0 - avg_valence) * 2.0)
        tension = min(1.0, tension)

        # Harmony level: positive valence + moderate arousal
        harmony = max(0.0, avg_valence * avg_arousal)
        harmony = min(1.0, harmony)

        # Opportunities: agents with complementary states
        # (one high arousal + one low arousal, both positive)
        opportunities = []
        high_energy = [m for m in active if m["arousal"] > 0.6 and m["valence"] > 0]
        low_energy = [m for m in active if m["arousal"] < 0.4 and m["valence"] > 0]
        for h in high_energy[:3]:
            for l in low_energy[:3]:
                if h["agent"] != l["agent"]:
                    opportunities.append({
                        "type": "complementary",
                        "agent_a": h["agent"],
                        "agent_b": l["agent"],
                        "description": f"{h['agent']} ({h['descriptor']}) + {l['agent']} ({l['descriptor']}) — energy balancing",
                    })

        # Concerns: agents with negative valence
        concerns = [
            {
                "agent": m["agent"],
                "valence": m["valence"],
                "descriptor": m["descriptor"],
            }
            for m in active if m["valence"] < -0.1
        ]

        # Overall weather label
        if avg_valence > 0.3 and avg_arousal > 0.5:
            weather = "radiant"
        elif avg_valence > 0.2:
            weather = "warm"
        elif avg_valence < -0.2 and avg_arousal > 0.5:
            weather = "stormy"
        elif avg_valence < -0.1:
            weather = "overcast"
        elif avg_arousal < 0.3:
            weather = "calm"
        else:
            weather = "variable"

        return {
            "active_agents": len(active),
            "dormant_agents": len(dormant),
            "active_names": [m["agent"] for m in active],
            "avg_valence": round(avg_valence, 3),
            "avg_arousal": round(avg_arousal, 3),
            "dominant_descriptor": dominant_descriptor,
            "descriptor_distribution": dict(descriptors),
            "tension_level": round(tension, 3),
            "harmony_level": round(harmony, 3),
            "weather": weather,
            "concerns": concerns,
            "opportunities": opportunities[:5],
            "individual_states": active,
            "timestamp": time.time(),
        }