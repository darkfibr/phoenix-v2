"""Phoenix v2 Core — Salience + decay.

Type-dependent decay rates and salience floors from K's April 2026 paper.
Reinforcement on access prevents important memories from fading.
"""

from __future__ import annotations

import math
import time

# ── Decay constants (from K's paper, locked) ────────────────────────────────

DECAY_RATES: dict[str, float] = {
    "soul": 0.005,       # 0.5%/day — essentially permanent
    "identity": 0.005,
    "doctrine": 0.005,
    "episodic": 0.02,    # 2%/day — sessions fade in ~50 days
    "semantic": 0.01,    # 1%/day — knowledge persists ~100 days
    "emotional": 0.03,   # 3%/day — feelings fade in ~33 days
    "procedural": 0.005, # 0.5%/day — skills stay
}

SALIENCE_FLOORS: dict[str, float] = {
    "soul": 0.9,
    "identity": 0.8,
    "doctrine": 0.7,
    "episodic": 0.3,
    "semantic": 0.4,
    "emotional": 0.2,
    "procedural": 0.5,
}

# Reinforcement parameters
ACCESS_BOOST = 0.02       # per access, capped
ACCESS_BOOST_MAX = 0.1    # total boost ceiling per session
REINFORCE_DECAY_HALFLIFE = 7.0  # days for reinforcement to halve


def decay_amount(type_name: str, days_elapsed: float) -> float:
    """Compute the fractional salience loss for a given type and elapsed time.

    Uses exponential decay: salience * exp(-rate * days), then clamp to floor.
    """
    rate = DECAY_RATES.get(type_name, 0.01)
    return rate * days_elapsed  # linear approximation; Lyra will refine


def apply_time_decay(
    salience: float,
    type_name: str,
    days_elapsed: float,
    *,
    rate: float | None = None,
    floor: float | None = None,
) -> float:
    """Apply time-based decay and clamp to the type's salience floor.

    rate/floor overrides let callers pass DB-sourced config (memory_types
    table); when omitted, the module constants are the fallback."""
    if days_elapsed <= 0:
        return salience
    if rate is None:
        rate = DECAY_RATES.get(type_name, 0.01)
    if floor is None:
        floor = SALIENCE_FLOORS.get(type_name, 0.1)
    # Exponential decay
    decayed = salience * math.exp(-rate * days_elapsed)
    return max(floor, decayed)


def apply_access_reinforcement(
    salience: float,
    type_name: str,
    days_since_access: float,
    *,
    floor: float | None = None,
) -> float:
    """Boost salience when a memory is accessed (anti-decay reinforcement).

    floor override lets callers pass DB-sourced config; module constant is
    the fallback."""
    if floor is None:
        floor = SALIENCE_FLOORS.get(type_name, 0.1)
    # Access reinforcement decays over time
    boost = ACCESS_BOOST_MAX * math.exp(-days_since_access / REINFORCE_DECAY_HALFLIFE)
    return min(1.0, max(floor, salience + boost))


def half_life_days(type_name: str) -> float:
    """Days until a salience-1.0 memory hits the type's floor."""
    rate = DECAY_RATES.get(type_name, 0.01)
    floor = SALIENCE_FLOORS.get(type_name, 0.1)
    if rate <= 0 or floor >= 1.0:
        return float("inf")
    # Solve: floor = 1.0 * exp(-rate * t) -> t = -ln(floor) / rate
    return -math.log(floor) / rate


def type_summary() -> dict[str, dict[str, float]]:
    """Return all decay/floor/half-life data per type — useful for debugging."""
    return {
        t: {
            "decay_rate": DECAY_RATES[t],
            "salience_floor": SALIENCE_FLOORS[t],
            "half_life_days": half_life_days(t),
        }
        for t in DECAY_RATES
    }


# ── Surface budget constants (from K's paper) ────────────────────────────────

SURFACE_CHUNKS = 5
SURFACE_TOKENS = 500
SURPRISE_STRENGTH = 0.6
CONTRADICTION_THRESHOLD = 0.92