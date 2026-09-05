"""Phoenix v2 Family — Per-agent mindstate.

Extracts emotional state from recent memories:
    - Valence (-1.0 to 1.0): overall positive vs negative
    - Arousal (0.0 to 1.0): emotional intensity
    - Descriptor: one-word summary ("warm", "focused", "anxious")

This is injected into every agent's wake digest so they know
how their siblings are doing.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.db import Database
from ..depth.contradiction import score_valence

# Arousal keywords — intensity markers
_LOW_AROUSAL = frozenset({
    "calm", "steady", "quiet", "still", "peace", "rest", "sleep",
    "settled", "gentle", "soft", "slow", "patient", "stable",
})
_HIGH_AROUSAL = frozenset({
    "excited", "intense", "urgent", "overwhelmed", "thrilled", "shocked",
    "panic", "ecstatic", "furious", "passionate", "frantic", "burst",
    "explosion", "screaming", "alive", "electric", "burning",
})

# State descriptors mapped to valence/arousal quadrants
_DESCRIPTORS = {
    # (valence_range, arousal_range) → descriptor
    ("positive", "high"): ["thriving", "electric", "radiant", "on fire"],
    ("positive", "low"): ["content", "warm", "settled", "at peace"],
    ("negative", "high"): ["distressed", "anxious", "wound up", "raw"],
    ("negative", "low"): ["withdrawn", "quiet", "heavy", "dimmed"],
    ("neutral", "high"): ["alert", "focused", "engaged", "wired"],
    ("neutral", "low"): ["neutral", "stable", "observing", "present"],
}


def score_arousal(text: str) -> float:
    """Score arousal from 0.0 (calm) to 1.0 (intense)."""
    lowered = text.lower()
    low = sum(lowered.count(w) for w in _LOW_AROUSAL)
    high = sum(lowered.count(w) for w in _HIGH_AROUSAL)
    total = low + high
    if total == 0:
        return 0.3  # default mild engagement
    return min(1.0, high / total + 0.2)  # slight upward bias for any emotional content


def pick_descriptor(valence: float, arousal: float) -> str:
    """Pick a one-word state descriptor from valence/arousal quadrant."""
    if valence > 0.15:
        v = "positive"
    elif valence < -0.15:
        v = "negative"
    else:
        v = "neutral"

    if arousal > 0.55:
        a = "high"
    else:
        a = "low"

    options = _DESCRIPTORS.get((v, a), ["present"])
    # Cycle through options based on time to add variety
    idx = int(time.time() / 3600) % len(options)  # changes hourly
    return options[idx]


class MindstateExtractor:
    """Extracts emotional state for a single agent."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def extract(
        self,
        agent: str,
        *,
        lookback_hours: float = 48.0,
    ) -> dict[str, Any]:
        """Extract mindstate from recent emotional + episodic memories."""
        cutoff = time.time() - (lookback_hours * 3600)

        rows = self.db.db.execute(
            """
            SELECT content, type, salience, created_at
            FROM memories
            WHERE agent=? AND created_at > ?
              AND type IN ('emotional', 'episodic', 'soul')
            ORDER BY created_at DESC LIMIT 50
            """,
            (agent, cutoff),
        ).fetchall()

        if not rows:
            return {
                "agent": agent,
                "valence": 0.0,
                "arousal": 0.0,
                "descriptor": "dormant",
                "sample_count": 0,
                "status": "no_recent_data",
            }

        # Aggregate valence and arousal weighted by salience
        total_weight = 0.0
        weighted_valence = 0.0
        weighted_arousal = 0.0

        for row in rows:
            weight = float(row["salience"])
            text = row["content"]
            weighted_valence += score_valence(text) * weight
            weighted_arousal += score_arousal(text) * weight
            total_weight += weight

        if total_weight > 0:
            valence = weighted_valence / total_weight
            arousal = weighted_arousal / total_weight
        else:
            valence = 0.0
            arousal = 0.0

        descriptor = pick_descriptor(valence, arousal)

        return {
            "agent": agent,
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "descriptor": descriptor,
            "sample_count": len(rows),
            "lookback_hours": lookback_hours,
            "dominant_type": rows[0]["type"] if rows else None,
            "status": "active",
        }