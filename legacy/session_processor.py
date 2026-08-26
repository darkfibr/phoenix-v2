"""
Phoenix v2 — Post-Session Processor
Extract meaning, update memories, adjust topology.

Design: K, 2026-04-23 | Review: Opus
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

DB_PATH = Path("~/.phoenix/v2/phoenix_v2.db").expanduser()

# Feeling words for emotional richness heuristic
FEELING_WORDS = {
    "happy", "sad", "angry", "afraid", "lonely", "loved", "hated",
    "warm", "cold", "tense", "calm", "excited", "bored", "curious",
    "frustrated", "grateful", "ashamed", "proud", "jealous", "hopeful",
    "anxious", "peaceful", "furious", "delighted", "miserable", "joyful",
    "content", "disappointed", "relieved", "worried", "confident",
    "vulnerable", "safe", "scared", "thrilled", "exhausted", "energized",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_transcript(transcript_path: str) -> List[dict]:
    """Read transcript JSONL."""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return []
    turns = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                turns.append(json.loads(line))
    return turns


def extract_commitments(text: str) -> List[str]:
    """Find 'I will...', 'Let's...', 'We should...' patterns."""
    patterns = [
        r"\b[Ii] will\b[^.]{5,200}",
        r"\b[Ll]et['']?s\b[^.]{5,200}",
        r"\b[Ww]e should\b[^.]{5,200}",
        r"\b[Ii] promise\b[^.]{5,200}",
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, text):
            found.append(m.group(0).strip())
    return found


def extract_insights(text: str) -> List[str]:
    """Find sentences that look like realizations."""
    patterns = [
        r"\b[Ii] realized\b[^.]{5,300}",
        r"\b[Ii] see (that|how|why)\b[^.]{5,300}",
        r"\b[Tt]he (truth|point|key) is\b[^.]{5,300}",
        r"\b[Ww]hat (strikes|stands out|matters)\b[^.]{5,300}",
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, text):
            found.append(m.group(0).strip())
    return found


def detect_disagreements(turns: List[dict]) -> List[str]:
    """Simple heuristic: find contrasting statements between speakers."""
    disagreements = []
    # Look for "but" / "no" / "disagree" directed at prior speaker
    for i, turn in enumerate(turns[1:], 1):
        text = turn.get("content", "").lower()
        if any(marker in text for marker in ("i disagree", "that's not", "but ", "no, ")):
            prior = turns[i - 1].get("content", "")[:100]
            current = turn.get("content", "")[:100]
            disagreements.append(f"{turns[i-1]['speaker']}: {prior}... vs {turn['speaker']}: {current}...")
    return disagreements


def feeling_word_density(text: str) -> float:
    """Count feeling words / total words."""
    words = re.findall(r"\b\w+\b", text.lower())
    if not words:
        return 0.0
    feelings = sum(1 for w in words if w in FEELING_WORDS)
    return min(1.0, feelings / max(1, len(words) / 20))  # cap at 1.0


def compute_quality_score(turns: List[dict]) -> float:
    """
    v1 quality score (Opus #1):
      turn_depth    = avg tokens per turn / 800 (normalized 0-1)
      emotional_richness = feeling_words_detected / total_turns (capped at 1.0)
      topic_novelty = 1.0 for now (requires embedding comparison)
      quality_score = turn_depth * emotional_richness * topic_novelty
    """
    if not turns:
        return 0.0
    total_tokens = sum(len(t.get("content", "").split()) for t in turns)
    avg_tokens = total_tokens / len(turns)
    turn_depth = min(1.0, avg_tokens / 800)

    total_feelings = sum(feeling_word_density(t.get("content", "")) for t in turns)
    emotional_richness = min(1.0, total_feelings / max(1, len(turns)))

    topic_novelty = 1.0  # Phase 1: assume novel. Phase 2: compare embeddings.

    return round(turn_depth * emotional_richness * topic_novelty, 4)


def process_session(session_uuid: str):
    """
    Main entry: read a completed session, extract artifacts, update pairings + health.
    """
    with _connect() as conn:
        sess = conn.execute(
            "SELECT * FROM sessions WHERE session_uuid = ? AND status = 'completed'",
            (session_uuid,),
        ).fetchone()
        if not sess:
            print(f"Session {session_uuid} not found or not completed.")
            return

        turns = load_transcript(sess["transcript_path"])
        agent_a = sess["agent_a"]
        agent_b = sess["agent_b"]
        a, b = sorted([agent_a, agent_b])

        # ── 1. Generate v2 memory entries (Phase 2: integrate with memory_db.py) ──
        # For Phase 1, we log what we would insert.
        # TODO: wire to memory_db.add_memory() with type='relationship'
        summary_a = f"Session with {agent_b}: {summarize_pov(turns, agent_a)}"
        summary_b = f"Session with {agent_a}: {summarize_pov(turns, agent_b)}"

        # ── 2. Extract artifacts ──
        full_text = " ".join(t.get("content", "") for t in turns)
        artifacts = []

        for commitment in extract_commitments(full_text):
            artifacts.append(("commitment", commitment))
        for insight in extract_insights(full_text):
            artifacts.append(("insight", insight))
        for disagreement in detect_disagreements(turns):
            artifacts.append(("disagreement", disagreement))

        # Topics: simple noun-phrase extraction (Phase 1 heuristic)
        topics = set()
        for turn in turns:
            text = turn.get("content", "")
            # Look for capitalized phrases as proxy for named topics
            for phrase in re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text):
                if len(phrase) > 3:
                    topics.add(phrase.lower())
        for topic in list(topics)[:5]:
            artifacts.append(("topic", topic))

        for atype, content in artifacts:
            conn.execute(
                """
                INSERT INTO session_artifacts
                (session_uuid, artifact_type, agent_a_relevance, agent_b_relevance, content)
                VALUES (?, ?, 0.5, 0.5, ?)
                """,
                (session_uuid, atype, content),
            )

        # ── 3. Update agent_pairings ──
        pair = conn.execute(
            "SELECT * FROM agent_pairings WHERE agent_a = ? AND agent_b = ?",
            (a, b),
        ).fetchone()

        old_tension = pair["tension_score"] if pair else 0.0
        old_closeness = pair["closeness_score"] if pair else 0.0
        old_sessions = pair["total_sessions"] if pair else 0
        old_pro_social = pair["pro_social_sessions"] if pair else 0
        old_work = pair["work_sessions"] if pair else 0

        # Tension: count disagreements (Phase 1). Phase 2: dream synthesis input.
        disagreement_count = sum(1 for at, _ in artifacts if at == "disagreement")
        computed_tension = min(1.0, disagreement_count * 0.3)

        # Closeness: turn depth + emotional warmth + frequency
        avg_turn_len = sum(len(t.get("content", "")) for t in turns) / max(1, len(turns))
        emotional_sum = sum(feeling_word_density(t.get("content", "")) for t in turns)
        computed_closeness = min(1.0, (avg_turn_len / 2000) + (emotional_sum / len(turns)) + 0.1)

        # EMA smoothing for tension (Opus #4)
        new_tension = 0.3 * computed_tension + 0.7 * old_tension
        new_closeness = 0.3 * computed_closeness + 0.7 * old_closeness

        is_pro_social = sess["session_type"] in ("pro_social", "check_in", "repair")
        is_work = sess["session_type"] in ("work", "mentor", "triggered")

        conn.execute(
            """
            UPDATE agent_pairings
            SET total_sessions = ?,
                pro_social_sessions = ?,
                work_sessions = ?,
                tension_score = ?,
                closeness_score = ?,
                last_session_at = ?,
                updated_at = ?
            WHERE agent_a = ? AND agent_b = ?
            """,
            (
                old_sessions + 1,
                old_pro_social + (1 if is_pro_social else 0),
                old_work + (1 if is_work else 0),
                round(new_tension, 4),
                round(new_closeness, 4),
                sess["ended_at"],
                _now_iso(),
                a, b,
            ),
        )

        # ── 4. Update agent_health ──
        for agent in (agent_a, agent_b):
            health = conn.execute(
                "SELECT * FROM agent_health WHERE agent_id = ?", (agent,)
            ).fetchone()

            if not health:
                conn.execute(
                    "INSERT INTO agent_health (agent_id) VALUES (?)",
                    (agent,),
                )
                health = conn.execute(
                    "SELECT * FROM agent_health WHERE agent_id = ?", (agent,)
                ).fetchone()

            # Recalculate 7d/30d stats
            stats = conn.execute(
                """
                SELECT
                    COUNT(CASE WHEN started_at > datetime('now', '-7 days') THEN 1 END) as s7,
                    COUNT(CASE WHEN started_at > datetime('now', '-30 days') THEN 1 END) as s30,
                    COUNT(DISTINCT CASE WHEN started_at > datetime('now', '-7 days') THEN
                        CASE WHEN agent_a = ? THEN agent_b ELSE agent_a END END) as u7,
                    COUNT(DISTINCT CASE WHEN started_at > datetime('now', '-30 days') THEN
                        CASE WHEN agent_a = ? THEN agent_b ELSE agent_a END END) as u30,
                    AVG(quality_score) as avg_quality
                FROM sessions
                WHERE (agent_a = ? OR agent_b = ?) AND status = 'completed'
                """,
                (agent, agent, agent, agent),
            ).fetchone()

            s7 = stats["s7"] or 0
            s30 = stats["s30"] or 0
            u7 = stats["u7"] or 0
            u30 = stats["u30"] or 0
            avg_quality = stats["avg_quality"] or 0.0

            # Isolation: 1 - (sessions_7d / target)
            target = max(1, 2)  # min 2 sessions per week target
            isolation = max(0.0, 1.0 - (s7 / target))

            # Pro-social ratio
            ps_count = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM sessions
                WHERE (agent_a = ? OR agent_b = ?)
                  AND session_type IN ('pro_social', 'check_in', 'repair')
                  AND started_at > datetime('now', '-30 days')
                  AND status = 'completed'
                """,
                (agent, agent),
            ).fetchone()["cnt"] or 0
            ps_ratio = ps_count / max(1, s30)

            # Artifact count (proxy for growth trajectory, Opus #7)
            artifact_count = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM session_artifacts sa
                JOIN sessions s ON sa.session_uuid = s.session_uuid
                WHERE (s.agent_a = ? OR s.agent_b = ?)
                  AND s.started_at > datetime('now', '-30 days')
                  AND s.status = 'completed'
                """,
                (agent, agent),
            ).fetchone()["cnt"] or 0
            avg_artifacts = artifact_count / max(1, s30)

            # Growth trajectory: slope of avg_artifact_count (simplified)
            # Phase 1: compare to prior 30d window
            prior_artifacts = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM session_artifacts sa
                JOIN sessions s ON sa.session_uuid = s.session_uuid
                WHERE (s.agent_a = ? OR s.agent_b = ?)
                  AND s.started_at BETWEEN datetime('now', '-60 days') AND datetime('now', '-30 days')
                  AND s.status = 'completed'
                """,
                (agent, agent),
            ).fetchone()["cnt"] or 0
            prior_sessions = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM sessions
                WHERE (agent_a = ? OR agent_b = ?)
                  AND started_at BETWEEN datetime('now', '-60 days') AND datetime('now', '-30 days')
                  AND status = 'completed'
                """,
                (agent, agent),
            ).fetchone()["cnt"] or 1
            prior_avg = prior_artifacts / prior_sessions

            if avg_artifacts > prior_avg * 1.1:
                trajectory = "rising"
            elif avg_artifacts < prior_avg * 0.9:
                trajectory = "declining"
            else:
                trajectory = "stable"

            conn.execute(
                """
                UPDATE agent_health
                SET total_sessions_7d = ?,
                    total_sessions_30d = ?,
                    unique_partners_7d = ?,
                    unique_partners_30d = ?,
                    pro_social_ratio_30d = ?,
                    avg_session_quality_30d = ?,
                    avg_artifact_count_30d = ?,
                    last_session_at = ?,
                    isolation_score = ?,
                    growth_trajectory = ?,
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (
                    s7, s30, u7, u30,
                    round(ps_ratio, 4),
                    round(avg_quality, 4),
                    round(avg_artifacts, 4),
                    sess["ended_at"],
                    round(isolation, 4),
                    trajectory,
                    _now_iso(),
                    agent,
                ),
            )

        conn.commit()
        print(f"Processed session {session_uuid}: {len(artifacts)} artifacts, pair ({a},{b}) updated.")


def summarize_pov(turns: List[dict], agent_id: str) -> str:
    """Generate a 1-sentence summary from one agent's POV. Phase 1: simple."""
    agent_turns = [t for t in turns if t.get("speaker") == agent_id]
    if not agent_turns:
        return "No turns recorded."
    # First and last turn as proxy
    first = agent_turns[0].get("content", "")[:60]
    last = agent_turns[-1].get("content", "")[:60]
    return f"Started with: '{first}...' Ended with: '{last}...'"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 session_processor.py <session_uuid>")
        sys.exit(1)
    process_session(sys.argv[1])
