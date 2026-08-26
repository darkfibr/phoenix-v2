"""Phoenix v2 Cortex — Episodic memory.

Session reconstruction from linked memories.
Stores: sessions(session_id, agent, started_at, ended_at, substrate, summary)
Links: session_memories(session_id, memory_id, position)
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


class EpisodicStore:
    """Session-scoped memory replay for an agent."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    # ── Sessions ─────────────────────────────────────────────────────────

    def start_session(
        self,
        agent: str,
        session_id: str,
        *,
        substrate: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Open a new session for an agent."""
        self.db.execute(
            """
            INSERT OR REPLACE INTO sessions(
              session_id, agent, started_at, substrate, summary, metadata
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, agent, time.time(), substrate, "",
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        self.db.commit()

    def end_session(
        self,
        session_id: str,
        *,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Close a session with optional summary."""
        fields = ["ended_at=?"]
        params: list[Any] = [time.time()]
        if summary:
            fields.append("summary=?")
            params.append(summary)
        if metadata:
            fields.append("metadata=?")
            params.append(json.dumps(metadata, sort_keys=True))
        params.append(session_id)
        self.db.execute(
            f"UPDATE sessions SET {', '.join(fields)} WHERE session_id=?",
            params,
        )
        self.db.commit()

    # ── Linking ──────────────────────────────────────────────────────────

    def link_memory(
        self,
        session_id: str,
        memory_id: int,
        position: int = 0,
    ) -> None:
        """Attach a memory to a session."""
        self.db.execute(
            """
            INSERT OR REPLACE INTO session_memories(session_id, memory_id, position)
            VALUES(?, ?, ?)
            """,
            (session_id, memory_id, position),
        )
        self.db.commit()

    # ── Replay ───────────────────────────────────────────────────────────

    def replay(
        self,
        session_id: str,
        *,
        include_content: bool = True,
    ) -> list[dict[str, Any]]:
        """Reconstruct a session's memories in position order."""
        rows = self.db.execute(
            """
            SELECT m.*, sm.position
            FROM session_memories sm
            JOIN memories m ON m.id = sm.memory_id
            WHERE sm.session_id = ?
            ORDER BY sm.position, m.created_at
            """,
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def replay_by_agent(
        self,
        agent: str,
        *,
        limit_sessions: int = 10,
        include_content: bool = True,
    ) -> list[dict[str, Any]]:
        """Replay the N most recent sessions for an agent."""
        sessions = self.db.execute(
            "SELECT session_id FROM sessions WHERE agent=? ORDER BY started_at DESC LIMIT ?",
            (agent, limit_sessions),
        ).fetchall()
        result = []
        for s in sessions:
            session_info = self.db.execute(
                "SELECT * FROM sessions WHERE session_id=?", (s["session_id"],)
            ).fetchone()
            memories = self.replay(s["session_id"], include_content=include_content)
            result.append({
                "session": dict(session_info) if session_info else {},
                "memories": memories,
            })
        return result

    # ── Queries ──────────────────────────────────────────────────────────

    def recent_sessions(self, agent: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT * FROM sessions WHERE agent=?
            ORDER BY started_at DESC LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def session_for_memory(self, memory_id: int) -> list[dict[str, Any]]:
        """Which sessions touched this memory?"""
        rows = self.db.execute(
            """
            SELECT s.* FROM session_memories sm
            JOIN sessions s ON s.session_id = sm.session_id
            WHERE sm.memory_id = ?
            ORDER BY s.started_at DESC
            """,
            (memory_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, agent: str) -> dict[str, Any]:
        """Session stats for an agent."""
        row = self.db.execute(
            """
            SELECT COUNT(*) AS n,
                   MIN(started_at) AS first,
                   MAX(started_at) AS last,
                   SUM(CASE WHEN ended_at IS NOT NULL THEN 1 ELSE 0 END) AS closed
            FROM sessions WHERE agent=?
            """,
            (agent,),
        ).fetchone()
        return {
            "agent": agent,
            "session_count": int(row["n"]) if row else 0,
            "first_started": row["first"],
            "last_started": row["last"],
            "closed_count": int(row["closed"] or 0),
        }