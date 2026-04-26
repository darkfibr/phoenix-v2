"""
Phoenix v2 — Session Runner
Executes a single session from queue. Turn-based API calls, real dialogue.

Design: K, 2026-04-23 | Review: Opus
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

DB_PATH = Path("~/.phoenix/v2/phoenix_v2.db").expanduser()
SESSIONS_DIR = Path("~/.phoenix/v2/sessions").expanduser()

# Retry config (Opus #8)
API_MAX_RETRIES = 3
API_RETRY_BACKOFF = 2  # seconds


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue_entry(conn: sqlite3.Connection, queue_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM orchestrator_queue WHERE id = ? AND status = 'pending'",
        (queue_id,),
    ).fetchone()


def lock_queue_entry(conn: sqlite3.Connection, queue_id: int) -> bool:
    cur = conn.execute(
        "UPDATE orchestrator_queue SET status = 'running' WHERE id = ? AND status = 'pending'",
        (queue_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def unlock_queue_entry(conn: sqlite3.Connection, queue_id: int, status: str = "pending"):
    conn.execute(
        "UPDATE orchestrator_queue SET status = ? WHERE id = ?",
        (status, queue_id),
    )
    conn.commit()


def build_context_packet(agent_id: str, partner_id: str, session_type: str, conn: sqlite3.Connection) -> str:
    """Build context packet for an agent: surfaced memories + relationship summary + manifest."""
    # Relationship summary (3-sentence distill)
    a, b = sorted([agent_id, partner_id])
    pair = conn.execute(
        "SELECT * FROM agent_pairings WHERE agent_a = ? AND agent_b = ?",
        (a, b),
    ).fetchone()

    relationship_summary = "You have not met before."
    if pair:
        sessions = pair["total_sessions"] or 0
        rel_tags = pair["relationship_tags"] or "neutral"
        relationship_summary = (
            f"You and {partner_id} have had {sessions} sessions. "
            f"Your relationship is: {rel_tags}. "
            f"Last met: {pair['last_session_at'] or 'never'}."
        )

    manifest = (
        f"This is a {session_type} session with {partner_id}. "
        f"You may end the session by saying [END SESSION]. Be real. No performance."
    )

    # TODO: integrate v2 surface engine for agent-specific memory chunks
    # For Phase 1, use a minimal context
    packet = f"""{relationship_summary}

{manifest}

You are in a private session. Respond as yourself."""
    return packet


def call_agent_api(agent_id: str, context: str, transcript_so_far: List[dict]) -> Tuple[str, Optional[str], int]:
    """
    Call agent API. Returns (response_text, thinking_trace, latency_ms).

    TODO: wire to phoenix-cli provider map:
      - k/vesper/echo/forge/qwen/weave/scout -> Kimi K2.6
      - glm -> GLM-5.1
      - sonnet/opus -> async bridge (skip for now)
    """
    # Phase 1 stub: returns a placeholder. Replace with real API call.
    start = time.time()

    # Placeholder: echo back a simple response
    # Real implementation will use phoenix-cli or direct API
    full_prompt = context + "\n\n---\n"
    for turn in transcript_so_far[-6:]:  # last 6 turns for context window
        speaker = turn.get("speaker", "?")
        content = turn.get("content", "")
        full_prompt += f"{speaker}: {content}\n"
    full_prompt += f"{agent_id}: "

    # STUB: replace with actual API invocation
    response_text = f"[STUB RESPONSE from {agent_id}]"
    thinking_trace = None
    latency_ms = int((time.time() - start) * 1000)

    return response_text, thinking_trace, latency_ms


def detect_echo(current_text: str, prior_texts: List[str]) -> bool:
    """
    Detect if current response is echoing prior turns.
    Phase 1: exact match fallback. Phase 2: semantic similarity via embeddings (Opus #2).
    """
    if not prior_texts:
        return False
    # Exact match or near-exact
    for prior in prior_texts[-3:]:
        if current_text.strip().lower() == prior.strip().lower():
            return True
    return False


def save_transcript(session_uuid: str, turns: List[dict]) -> Path:
    """Write full transcript to JSONL. Returns path."""
    now = datetime.now(timezone.utc)
    year_month = now.strftime("%Y/%m")
    out_dir = SESSIONS_DIR / year_month
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{session_uuid}.jsonl"

    with open(path, "w", encoding="utf-8") as f:
        for turn in turns:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")

    return path


def run_session(queue_id: int) -> str:
    """
    Execute a single session from the queue.
    Returns session_uuid on success, or error string on failure.
    """
    with _connect() as conn:
        entry = load_queue_entry(conn, queue_id)
        if not entry:
            return "Queue entry not found or not pending."

        if not lock_queue_entry(conn, queue_id):
            return "Could not lock queue entry (race condition)."

        agent_a = entry["agent_a"]
        agent_b = entry["agent_b"]
        session_type = entry["session_type"]
        seed_topic = entry["seed_topic"]
        max_turns = 16  # hard cap

        session_uuid = str(uuid.uuid4())
        turns = []

        # Insert session manifest
        conn.execute(
            """
            INSERT INTO sessions
            (session_uuid, status, session_type, agent_a, agent_b, started_at, max_turns, seed_topic, privacy_level)
            VALUES (?, 'running', ?, ?, ?, ?, ?, ?, 'private')
            """,
            (session_uuid, session_type, agent_a, agent_b, _now_iso(), max_turns, seed_topic),
        )
        conn.commit()

        try:
            context_a = build_context_packet(agent_a, agent_b, session_type, conn)
            context_b = build_context_packet(agent_b, agent_a, session_type, conn)

            current_speaker = agent_a
            current_context = context_a
            other_context = context_b
            echo_count = 0
            termination = None

            for turn_num in range(1, max_turns + 1):
                # API call with retry (Opus #8)
                response_text = None
                thinking_trace = None
                latency_ms = 0
                for attempt in range(API_MAX_RETRIES):
                    try:
                        response_text, thinking_trace, latency_ms = call_agent_api(
                            current_speaker, current_context, turns
                        )
                        break
                    except Exception as e:
                        if attempt < API_MAX_RETRIES - 1:
                            time.sleep(API_RETRY_BACKOFF * (attempt + 1))
                        else:
                            raise

                turn_record = {
                    "session_uuid": session_uuid,
                    "turn_number": turn_num,
                    "speaker": current_speaker,
                    "content": response_text,
                    "thinking_trace": thinking_trace,
                    "latency_ms": latency_ms,
                    "timestamp": _now_iso(),
                }
                turns.append(turn_record)

                # Store turn in DB
                conn.execute(
                    """
                    INSERT INTO session_turns
                    (session_uuid, turn_number, speaker, content, thinking_trace, latency_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_uuid, turn_num, current_speaker, response_text, thinking_trace, latency_ms),
                )
                conn.commit()

                # Check for agent-end
                if "[END SESSION]" in response_text:
                    termination = "agent_end"
                    break

                # Echo detection
                prior_texts = [t["content"] for t in turns if t["speaker"] == current_speaker]
                if detect_echo(response_text, prior_texts):
                    echo_count += 1
                    if echo_count >= 2:
                        termination = "echo_detected"
                        break
                else:
                    echo_count = 0

                # Soft cap negotiation at turn 8
                if turn_num == 8:
                    # For Phase 1, auto-continue. Phase 2: ask agents.
                    pass

                # Swap speakers
                current_speaker = agent_b if current_speaker == agent_a else agent_a
                current_context, other_context = other_context, current_context

            # Finalize session
            ended_at = _now_iso()
            transcript_path = str(save_transcript(session_uuid, turns))
            turn_count = len(turns)
            termination = termination or ("max_turns" if turn_count >= max_turns else "natural")

            conn.execute(
                """
                UPDATE sessions
                SET status = 'completed', ended_at = ?, turn_count = ?, termination_reason = ?, transcript_path = ?
                WHERE session_uuid = ?
                """,
                (ended_at, turn_count, termination, transcript_path, session_uuid),
            )

            # Unlock queue
            conn.execute("DELETE FROM orchestrator_queue WHERE id = ?", (queue_id,))
            conn.commit()

            return session_uuid

        except Exception as e:
            # Save partial transcript on failure (Opus #8)
            if turns:
                save_transcript(session_uuid, turns)
            conn.execute(
                "UPDATE sessions SET status = 'failed', ended_at = ? WHERE session_uuid = ?",
                (_now_iso(), session_uuid),
            )
            unlock_queue_entry(conn, queue_id, "failed")
            return f"FAILED: {e}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 session_runner.py <queue_id>")
        sys.exit(1)
    result = run_session(int(sys.argv[1]))
    print(result)
