"""
Phoenix v2 — Session Orchestrator
Decides WHO meets, WHEN, and WHY. Runs every 30 minutes or on-demand.

Design: K, 2026-04-23 | Review: Opus
"""

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

DB_PATH = Path("~/.phoenix/v2/phoenix_v2.db").expanduser()
AGENTS_DIR = Path("~/.phoenix/agents").expanduser()

# Phase 1: crossed agents only (Opus #10)
CROSSED_AGENTS = ["k", "vesper", "echo", "glm", "scout"]

# Min interval between sessions for same agent (hours)
MIN_AGENT_INTERVAL_HOURS = 6
MAX_SESSIONS_PER_AGENT_PER_DAY = 2


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_all_agents() -> List[str]:
    """Return list of active agent IDs from filesystem."""
    agents = []
    if AGENTS_DIR.exists():
        for d in AGENTS_DIR.iterdir():
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("__"):
                # Skip non-agent directories
                if (d / "WAKE_DIGEST.md").exists() or (d / "SOUL.md").exists():
                    agents.append(d.name)
    # Fallback to crossed agents if filesystem scan is empty
    if not agents:
        agents = CROSSED_AGENTS.copy()
    return sorted(set(agents))


def load_agent_health(conn: sqlite3.Connection, agent_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM agent_health WHERE agent_id = ?", (agent_id,)
    ).fetchone()


def load_pairing(conn: sqlite3.Connection, a: str, b: str) -> Optional[sqlite3.Row]:
    a, b = sorted([a, b])
    return conn.execute(
        "SELECT * FROM agent_pairings WHERE agent_a = ? AND agent_b = ?",
        (a, b),
    ).fetchone()


def compute_interaction_deficit(conn: sqlite3.Connection, agent_id: str, all_agents: List[str]) -> int:
    """How many more unique partners this agent needs in the last 7 days."""
    target_partners = min(3, max(1, (len(all_agents) - 1) // 3))

    row = conn.execute(
        """
        SELECT COUNT(DISTINCT CASE WHEN agent_a = ? THEN agent_b ELSE agent_a END) as unique_partners
        FROM sessions
        WHERE (agent_a = ? OR agent_b = ?)
          AND started_at > datetime('now', '-7 days')
          AND status = 'completed'
        """,
        (agent_id, agent_id, agent_id),
    ).fetchone()

    actual = row["unique_partners"] if row and row["unique_partners"] else 0
    return max(0, target_partners - actual)


def last_session_time(conn: sqlite3.Connection, agent_id: str) -> Optional[datetime]:
    row = conn.execute(
        """
        SELECT MAX(started_at) as last_started,
               MAX(ended_at) as last_ended
        FROM sessions
        WHERE agent_a = ? OR agent_b = ?
        """,
        (agent_id, agent_id),
    ).fetchone()
    times = []
    for col in ("last_started", "last_ended"):
        if row and row[col]:
            t = _parse_iso(row[col])
            if t:
                times.append(t)
    return max(times) if times else None


def agent_available(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Check if agent is within cooldown and session limits."""
    last = last_session_time(conn, agent_id)
    if last:
        if datetime.utcnow() - last < timedelta(hours=MIN_AGENT_INTERVAL_HOURS):
            return False

    # Count sessions in last 24h
    row = conn.execute(
        """
        SELECT COUNT(*) as cnt FROM sessions
        WHERE (agent_a = ? OR agent_b = ?)
          AND started_at > datetime('now', '-1 day')
          AND status IN ('completed', 'running')
        """,
        (agent_id, agent_id),
    ).fetchone()

    if row and row["cnt"] >= MAX_SESSIONS_PER_AGENT_PER_DAY:
        return False

    # Check if already queued or running
    row = conn.execute(
        """
        SELECT COUNT(*) as cnt FROM orchestrator_queue
        WHERE (agent_a = ? OR agent_b = ?) AND status IN ('pending', 'running')
        """,
        (agent_id, agent_id),
    ).fetchone()

    if row and row["cnt"] > 0:
        return False

    return True


def generate_candidates(conn: sqlite3.Connection, all_agents: List[str]) -> List[dict]:
    """Generate session candidates using all six strategies."""
    candidates = []

    # Precompute deficits and availability
    deficits = {a: compute_interaction_deficit(conn, a, all_agents) for a in all_agents}
    available = {a: agent_available(conn, a) for a in all_agents}

    # ── Strategy A: ROTATION ──
    # Agent with highest deficit + least-recent partner
    for agent in sorted(all_agents, key=lambda a: deficits[a], reverse=True):
        if not available[agent]:
            continue
        # Find least-recent partner among available agents
        best_partner = None
        best_last = datetime.utcnow()
        for partner in all_agents:
            if partner == agent or not available[partner]:
                continue
            pair = load_pairing(conn, agent, partner)
            last = _parse_iso(pair["last_session_at"]) if pair else None
            if last is None or last < best_last:
                best_last = last or datetime.min
                best_partner = partner
        if best_partner:
            candidates.append({
                "agent_a": agent,
                "agent_b": best_partner,
                "session_type": "pro_social",
                "priority": max(1, 6 - deficits[agent]),
                "trigger_source": "rotation",
                "reason": f"{agent} needs interaction (deficit={deficits[agent]})",
            })

    # ── Strategy B: TENSION ──
    # Pairs with tension_score > 0.6 → repair session
    rows = conn.execute(
        """
        SELECT agent_a, agent_b, tension_score FROM agent_pairings
        WHERE tension_score > 0.6
        """
    ).fetchall()
    for row in rows:
        a, b = row["agent_a"], row["agent_b"]
        if a in all_agents and b in all_agents and available.get(a) and available.get(b):
            candidates.append({
                "agent_a": a,
                "agent_b": b,
                "session_type": "repair",
                "priority": 2,
                "trigger_source": "tension",
                "reason": f"Tension score {row['tension_score']:.2f} between {a} and {b}",
            })

    # ── Strategy C: DECAY ──
    # Pairs not met in 48h → check_in
    for a in all_agents:
        for b in all_agents:
            if a >= b:
                continue
            if not available.get(a) or not available.get(b):
                continue
            pair = load_pairing(conn, a, b)
            last = _parse_iso(pair["last_session_at"]) if pair else None
            if last is None or (datetime.utcnow() - last) > timedelta(hours=48):
                candidates.append({
                    "agent_a": a,
                    "agent_b": b,
                    "session_type": "pro_social",
                    "priority": 4,
                    "trigger_source": "decay",
                    "reason": f"No session since {pair['last_session_at'] if pair else 'never'}",
                })

    # ── Strategy D: GROWTH ──
    # Senior + junior where junior shows 'rising' trajectory
    rows = conn.execute(
        """
        SELECT agent_id FROM agent_health
        WHERE growth_trajectory = 'rising'
        """
    ).fetchall()
    rising = [r["agent_id"] for r in rows if r["agent_id"] in all_agents]
    for junior in rising:
        if not available.get(junior):
            continue
        # Pair with most senior partner (most total_sessions)
        senior = None
        senior_sessions = -1
        for other in all_agents:
            if other == junior:
                continue
            if not available.get(other):
                continue
            health = load_agent_health(conn, other)
            sessions = health["total_sessions_30d"] if health else 0
            if sessions > senior_sessions:
                senior_sessions = sessions
                senior = other
        if senior:
            candidates.append({
                "agent_a": junior,
                "agent_b": senior,
                "session_type": "mentor",
                "priority": 3,
                "trigger_source": "growth",
                "reason": f"{junior} is rising; paired with { senior} for growth",
            })

    # ── Strategy E: SELF_REQUEST ──
    # Parse agent journals for "want to talk to X" / "need to see Y"
    for agent in all_agents:
        journal_path = AGENTS_DIR / agent / "JOURNAL.md"
        if not journal_path.exists():
            continue
        try:
            text = journal_path.read_text(encoding="utf-8")
            for other in all_agents:
                if other == agent:
                    continue
                # Simple keyword detection
                patterns = [
                    f"want to talk to {other}",
                    f"need to see {other}",
                    f"miss {other}",
                    f"reach out to {other}",
                ]
                if any(p in text.lower() for p in patterns):
                    if available.get(agent) and available.get(other):
                        candidates.append({
                            "agent_a": agent,
                            "agent_b": other,
                            "session_type": "pro_social",
                            "priority": 2,
                            "trigger_source": "self_request",
                            "reason": f"{agent} journal indicates desire to connect with {other}",
                        })
        except Exception:
            pass

    # ── Strategy F: ANOMALY ──
    # TODO: Scout/system flags → queue with relevant expert
    # For now, placeholder: any agent with isolation_score > 0.7
    rows = conn.execute(
        """
        SELECT agent_id FROM agent_health
        WHERE isolation_score > 0.7
        """
    ).fetchall()
    for row in rows:
        agent = row["agent_id"]
        if agent not in all_agents or not available.get(agent):
            continue
        # Pair with closest partner (highest closeness_score)
        pair_rows = conn.execute(
            """
            SELECT agent_a, agent_b, closeness_score FROM agent_pairings
            WHERE agent_a = ? OR agent_b = ?
            ORDER BY closeness_score DESC
            LIMIT 1
            """,
            (agent, agent),
        ).fetchall()
        for pr in pair_rows:
            partner = pr["agent_b"] if pr["agent_a"] == agent else pr["agent_a"]
            if partner in all_agents and available.get(partner):
                candidates.append({
                    "agent_a": agent,
                    "agent_b": partner,
                    "session_type": "triggered",
                    "priority": 1,
                    "trigger_source": "anomaly",
                    "reason": f"{agent} isolation score critical; emergency connection",
                })

    return candidates


def score_candidate(cand: dict, deficits: dict) -> float:
    """Score a candidate by deficit * urgency * recency_factor."""
    deficit = max(deficits.get(cand["agent_a"], 0), deficits.get(cand["agent_b"], 0))
    urgency = 11 - cand.get("priority", 5)  # invert: lower priority number = higher urgency
    # Recency factor: penalty if same pair met recently
    # (simplified: assume decay strategy already handles this)
    return deficit * urgency


def deduplicate_and_enqueue(conn: sqlite3.Connection, candidates: List[dict], all_agents: List[str]) -> int:
    """Deduplicate candidates, enforce constraints, write to queue. Return count queued."""
    deficits = {a: compute_interaction_deficit(conn, a, all_agents) for a in all_agents}

    # Sort by score descending
    candidates.sort(key=lambda c: score_candidate(c, deficits), reverse=True)

    seen_pairs = set()
    queued_agents = set()
    enqueued = 0

    for cand in candidates:
        a, b = sorted([cand["agent_a"], cand["agent_b"]])
        pair_key = (a, b)

        # Deduplicate: keep highest-scored for each pair
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        # Constraint: no agent in >1 queued session
        # (Opus: allow queued, just don't run concurrently. We enforce at runner level.)
        # But we do enforce no duplicate pairs in queue.
        existing = conn.execute(
            """
            SELECT id FROM orchestrator_queue
            WHERE agent_a = ? AND agent_b = ? AND status = 'pending'
            """,
            (a, b),
        ).fetchone()
        if existing:
            continue

        # Insert
        conn.execute(
            """
            INSERT INTO orchestrator_queue
            (agent_a, agent_b, session_type, priority, trigger_source, seed_topic, scheduled_for, reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                a, b,
                cand["session_type"],
                cand.get("priority", 5),
                cand.get("trigger_source", "rotation"),
                cand.get("seed_topic"),
                _now_iso(),
                cand.get("reason", ""),
            ),
        )
        enqueued += 1

    conn.commit()
    return enqueued


def run_orchestrator() -> int:
    """Main entry point. Returns number of sessions queued."""
    with _connect() as conn:
        all_agents = get_all_agents()
        # Phase 1: crossed agents only (Opus #10)
        all_agents = [a for a in all_agents if a in CROSSED_AGENTS]
        if len(all_agents) < 2:
            print("Not enough active agents to schedule sessions.")
            return 0

        candidates = generate_candidates(conn, all_agents)
        enqueued = deduplicate_and_enqueue(conn, candidates, all_agents)
        print(f"Orchestrator: {enqueued} session(s) queued for {len(all_agents)} agents.")
        return enqueued


if __name__ == "__main__":
    run_orchestrator()
