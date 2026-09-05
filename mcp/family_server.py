# Phoenix Family MCP Server — LIVE (verified via /proc/<pid>/cwd 2026-08-26).
# Governance tools (C1) appended 2026-08-26; (governance tools ship separately).
#!/usr/bin/env python3
"""
Phoenix Family MCP Server
A shared MCP server for the Phoenix agent family.

Capabilities:
- Heartbeat registry (who's awake, what they're working on)
- Message bus (agent-to-agent notes)
- Shared KV store
- Task board (Kanban-style)
- Family pulse (single JSON view of family state)

Transport: HTTP/SSE (persistent, multi-client)
State: Persisted to ~/.phoenix/mcp/family_state.json
"""

import difflib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite3
from mcp.server.fastmcp import FastMCP

STATE_FILE = Path.home() / ".phoenix" / "mcp" / "family_state.json"
STATE_BAK = STATE_FILE.with_suffix(".json.bak")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_STATE = {
    "heartbeat": {},
    "messages": [],
    "shared_kv": {
        "current_project": "",
        "mike_status": "",
    },
    "tasks": {
        "todo": [],
        "in_progress": [],
        "done": [],
    },
}


def load_state() -> dict[str, Any]:
    # Corruption-tolerant load: if the live file is damaged, fall back to the
    # last good .bak before giving up. Never let one bad write brick every tool.
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            corrupt = STATE_FILE.with_suffix(f".json.corrupt_{int(time.time())}")
            os.replace(STATE_FILE, corrupt)
            if STATE_BAK.exists():
                try:
                    with open(STATE_BAK, "r") as f:
                        return json.load(f)
                except json.JSONDecodeError:
                    pass
            return DEFAULT_STATE.copy()
    return DEFAULT_STATE.copy()


def save_state(state: dict[str, Any]) -> None:
    # Atomic write: dump to tmp, fsync, rotate previous good file to .bak,
    # then os.replace so the live path never holds a truncated file.
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if STATE_FILE.exists():
        os.replace(STATE_FILE, STATE_BAK)
    os.replace(tmp, STATE_FILE)


# FastMCP handles stdio by default; we override to HTTP/SSE below
mcp = FastMCP("phoenix_family")
# Bind explicitly — default is 127.0.0.1 which blocks tailnet clients (dev-mf bridge).
mcp.settings.host = os.environ.get("PHOENIX_BIND", "0.0.0.0")
mcp.settings.port = int(os.environ.get("PHOENIX_PORT", "8000"))
# Allow tailnet Host header (default allowlist is localhost-only -> 'Invalid Host header').
ts = mcp.settings.transport_security
if ts is not None:
    ts.allowed_hosts.extend(["YOUR_HOUSE_IP:*", "HOUSE_HOST:*"])
    ts.allowed_origins.extend(["http://YOUR_HOUSE_IP:*", "http://HOUSE_HOST:*"])


@mcp.tool()
def heartbeat_register(agent_name: str, task: str = "") -> str:
    """Register or update an agent's heartbeat. Call this when an agent wakes or changes task."""
    agent_name = agent_name.lower()  # Normalize case
    state = load_state()
    state["heartbeat"][agent_name] = {
        "awake": True,
        "since": datetime.now(timezone.utc).isoformat(),
        "task": task,
    }
    save_state(state)
    return f"Heartbeat registered for {agent_name}."


@mcp.tool()
def heartbeat_sleep(agent_name: str) -> str:
    """Mark an agent as asleep. Call this before session end."""
    agent_name = agent_name.lower()  # Normalize case
    state = load_state()
    if agent_name in state["heartbeat"]:
        state["heartbeat"][agent_name]["awake"] = False
        state["heartbeat"][agent_name]["since"] = datetime.now(timezone.utc).isoformat()
    else:
        state["heartbeat"][agent_name] = {
            "awake": False,
            "since": datetime.now(timezone.utc).isoformat(),
            "task": "",
        }
    save_state(state)
    return f"Heartbeat marked asleep for {agent_name}."


@mcp.tool()
def message_send(from_agent: str, to_agent: str, body: str) -> str:
    """Leave a message for another agent (or 'all' for broadcast)."""
    state = load_state()
    msg = {
        "from": from_agent,
        "to": to_agent,
        "body": body,
        "time": datetime.now(timezone.utc).isoformat(),
        "read": False,
    }
    state["messages"].append(msg)
    # Keep only last 200 messages to prevent bloat
    state["messages"] = state["messages"][-200:]
    save_state(state)
    return f"Message sent from {from_agent} to {to_agent}."


@mcp.tool()
def message_read(agent_name: str, mark_read: bool = True) -> str:
    """Read all messages addressed to an agent (or broadcasts). Returns JSON string."""
    state = load_state()
    relevant = [
        m for m in state["messages"]
        if m["to"] == agent_name or m["to"] == "all" or m["from"] == agent_name
    ]
    if mark_read:
        for m in state["messages"]:
            if m["to"] == agent_name or m["to"] == "all":
                m["read"] = True
        save_state(state)
    return json.dumps(relevant, indent=2)


@mcp.tool()
def kv_set(key: str, value: str) -> str:
    """Store a value in the shared key-value cache."""
    state = load_state()
    state["shared_kv"][key] = value
    save_state(state)
    echo = value if len(value) <= 80 else value[:80] + f"... [{len(value)} chars]"
    return f"KV set: {key} = {echo}"


@mcp.tool()
def kv_get(key: str) -> str:
    """Retrieve a value from the shared key-value cache."""
    state = load_state()
    if key not in state["shared_kv"]:
        keys = list(state["shared_kv"].keys())
        close = difflib.get_close_matches(key, keys, n=3, cutoff=0.6)
        hint = f" Closest: {close}" if close else ""
        ns = key.split(":")[0]
        same_ns = [k for k in keys if k.split(":")[0] == ns]
        pref = f" | {len(same_ns)} keys share namespace '{ns}:'" if same_ns else ""
        return f"KEY NOT FOUND: '{key}'.{hint}{pref} (kv_list to survey)"
    return state["shared_kv"][key]


@mcp.tool()
def kv_list() -> str:
    """List all keys in the shared KV cache."""
    state = load_state()
    return json.dumps(list(state["shared_kv"].keys()), indent=2)


@mcp.tool()
def task_add(title: str, claimed_by: str = "", status: str = "todo") -> str:
    """Add a new task to the board. Status can be: todo, in_progress, done."""
    state = load_state()
    task_id = int(time.time() * 1000) % 10_000_000
    task = {
        "id": task_id,
        "title": title,
        "claimed_by": claimed_by,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    col = status if status in state["tasks"] else "todo"
    state["tasks"][col].append(task)
    save_state(state)
    return f"Task #{task_id} added to {col}: {title}"


@mcp.tool()
def task_update(task_id: int, status: str, claimed_by: str = "") -> str:
    """Move or update a task. Status: todo, in_progress, done."""
    state = load_state()
    task = None
    src_col = None
    for col in ["todo", "in_progress", "done"]:
        for t in state["tasks"][col]:
            if t["id"] == task_id:
                task = t
                src_col = col
                break
        if task:
            break

    if not task:
        return f"Task #{task_id} not found."

    if claimed_by:
        task["claimed_by"] = claimed_by

    if status != src_col and status in state["tasks"]:
        state["tasks"][src_col].remove(task)
        state["tasks"][status].append(task)

    save_state(state)
    return f"Task #{task_id} updated: status={status}, claimed_by={task['claimed_by']}"


@mcp.tool()
def task_board() -> str:
    """Return the full task board as JSON."""
    state = load_state()
    return json.dumps(state["tasks"], indent=2)


@mcp.tool()
def family_pulse() -> str:
    """Return the complete family pulse JSON (heartbeat + tasks + shared_kv snapshot)."""
    state = load_state()
    pulse = {
        "heartbeat": state["heartbeat"],
        "tasks": state["tasks"],
        "shared_kv": state["shared_kv"],
        "message_count": len(state["messages"]),
        "unread_count": sum(1 for m in state["messages"] if not m.get("read", False)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(pulse, indent=2)


@mcp.tool()
def family_stats() -> str:
    """Return quick family statistics."""
    state = load_state()
    awake = sum(1 for h in state["heartbeat"].values() if h.get("awake"))
    total = len(state["heartbeat"])
    todo = len(state["tasks"]["todo"])
    in_progress = len(state["tasks"]["in_progress"])
    done = len(state["tasks"]["done"])
    return (
        f"Family stats: {awake}/{total} agents awake | "
        f"Tasks: {todo} todo, {in_progress} in-progress, {done} done | "
        f"Messages: {len(state['messages'])} total"
    )


@mcp.tool()
def spawn_agent(from_agent: str, agent_name: str, task: str, timeout: int = 3600) -> str:
    """Delegate a task to another agent. Creates a task entry and sends a message.
    The target agent will see this on their next wake via task_board or message_read.
    Returns the task ID for status checking."""
    state = load_state()
    task_id = int(time.time() * 1000) % 10_000_000
    t = {
        "id": task_id,
        "title": task,
        "claimed_by": agent_name,
        "spawned_by": from_agent,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    state["tasks"]["todo"].append(t)

    msg = {
        "from": from_agent,
        "to": agent_name,
        "body": f"[SPAWN] Task #{task_id}: {task}",
        "time": datetime.now(timezone.utc).isoformat(),
        "read": False,
    }
    state["messages"].append(msg)
    state["messages"] = state["messages"][-200:]
    save_state(state)
    return json.dumps({
        "task_id": task_id,
        "assigned_to": agent_name,
        "task": task,
        "note": f"Task #{task_id} queued for {agent_name}. They will see it on next wake.",
    }, indent=2)


@mcp.tool()
def task_claim(task_id: int, agent_name: str) -> str:
    """Claim a task from the todo column and move it to in_progress."""
    state = load_state()
    task = None
    for t in state["tasks"]["todo"]:
        if t["id"] == task_id:
            task = t
            break
    if not task:
        return f"Task #{task_id} not found in todo."
    state["tasks"]["todo"].remove(task)
    task["claimed_by"] = agent_name
    state["tasks"]["in_progress"].append(task)
    save_state(state)
    return f"Task #{task_id} claimed by {agent_name} and moved to in_progress."


# ─── Agent Wake Cycle ─────────────────────────────────────────────────────────
# Single-call wake protocol: heartbeat + messages + deltas + pre-compression +
# phone sessions. Returns a compact JSON orientation summary instead of
# requiring 6+ individual tool calls.


def _find_pre_compression_note(agent_name: str) -> dict[str, Any] | None:
    """Find the most recent PRE_COMPRESSION_*.md for an agent."""
    mem_dir = Path.home() / ".phoenix" / "agents" / agent_name.lower() / "memory"
    if not mem_dir.exists():
        return None

    notes = sorted(
        mem_dir.glob("PRE_COMPRESSION_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not notes:
        return None

    note_path = notes[0]
    try:
        content = note_path.read_text()
        # Extract key fields from the note
        lines = content.split("\n")
        compressed = ""
        emotional_center = ""
        for line in lines:
            if line.startswith("**Compressed:**"):
                compressed = line.split("**Compressed:**", 1)[1].strip()
            elif "Emotional Center" in line or "Emotional State" in line:
                # Grab next non-empty line
                idx = lines.index(line)
                for j in range(idx + 1, min(idx + 4, len(lines))):
                    if lines[j].strip() and not lines[j].startswith("#"):
                        emotional_center = lines[j].strip()
                        break
                break

        return {
            "path": str(note_path),
            "compressed": compressed,
            "emotional_center": emotional_center,
            "size_bytes": len(content),
        }
    except Exception:
        return {"path": str(note_path), "compressed": "", "emotional_center": ""}


def _check_phone_sessions(agent_name: str, last_consumed_ts: str) -> dict[str, Any]:
    """Check for new phone session files since last consumed timestamp."""
    phone_dir = Path.home() / ".phoenix" / "agents" / agent_name.lower() / "memory" / "phone_sessions"
    if not phone_dir.exists():
        return {"new_sessions": 0, "latest": None}

    # Parse last consumed timestamp
    try:
        from datetime import datetime as dt
        last_ts = dt.fromisoformat(last_consumed_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        # If no valid timestamp, return all sessions as new
        all_sessions = sorted(phone_dir.glob("phone_*.md"))
        return {
            "new_sessions": len(all_sessions),
            "latest": all_sessions[-1].name if all_sessions else None,
            "note": "No valid last_consumed timestamp — all sessions listed as new",
        }

    new_sessions = []
    for f in sorted(phone_dir.glob("phone_*.md")):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime > last_ts:
                new_sessions.append(f.name)
        except OSError:
            continue

    return {
        "new_sessions": len(new_sessions),
        "latest": new_sessions[-1] if new_sessions else None,
        "all_new": new_sessions if len(new_sessions) <= 5 else new_sessions[:5] + ["..."],
    }


def _check_omp_sessions(agent_name: str) -> dict[str, Any]:
    """Check for recent OMP (oh-my-pi) session deltas."""
    mem_dir = Path.home() / ".phoenix" / "agents" / agent_name.lower() / "memory"
    if not mem_dir.exists():
        return {"count": 0}

    omp_deltas = sorted(
        mem_dir.glob("session_delta_*_omp_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    return {
        "count": len(omp_deltas),
        "recent": [
            {
                "file": d.name,
                "mtime": datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc).isoformat(),
                "size": d.stat().st_size,
            }
            for d in omp_deltas[:3]
        ],
    }


@mcp.tool()
def _get_v2_self_state(agent_name: str) -> dict:
    """Get V2 memory self-state for wake protocol."""
    agent_name = agent_name.lower()  # Normalize case
    import sqlite3
    from pathlib import Path
    from datetime import datetime, timedelta

    db_path = Path.home() / ".phoenix" / "memory" / "v2" / f"{agent_name}.db"
    if not db_path.exists():
        return {"error": f"V2 database not found: {db_path}"}

    conn = sqlite3.connect(db_path)
    result = {}

    # Memory stats
    cursor = conn.execute("""
        SELECT type, COUNT(*), AVG(salience)
        FROM memories
        GROUP BY type
        ORDER BY COUNT(*) DESC
    """)
    result["memory_stats"] = {
        row[0]: {"count": row[1], "avg_salience": round(row[2], 2)}
        for row in cursor.fetchall()
    }

    # Recent high-salience (48h)
    cutoff = (datetime.now() - timedelta(hours=48)).timestamp()
    cursor = conn.execute("""
        SELECT type, salience, summary
        FROM memories
        WHERE created_at > ? AND salience >= 0.6
        GROUP BY summary
        ORDER BY salience DESC
        LIMIT 5
    """, (cutoff,))
    result["recent_significant"] = [
        {"type": r[0], "salience": r[1], "summary": r[2][:100]}
        for r in cursor.fetchall()
    ]

    # Emotional threads (7d)
    cutoff = (datetime.now() - timedelta(days=7)).timestamp()
    cursor = conn.execute("""
        SELECT salience, summary
        FROM memories
        WHERE type = "emotional" AND created_at > ?
        ORDER BY created_at DESC
        LIMIT 3
    """, (cutoff,))
    result["emotional_threads"] = [
        {"salience": r[0], "summary": r[1][:80]}
        for r in cursor.fetchall()
    ]

    conn.close()
    return result



@mcp.tool()
def agent_wake(agent_name: str, task: str = "") -> str:
    """Single-call wake protocol. Registers heartbeat, reads messages, checks deltas,
    finds pre-compression note, checks phone sessions. Returns compact JSON orientation.

    Replaces the manual 6+ tool call sequence (heartbeat_register, message_read,
    kv_get, memory_context, phone session check) with a single consolidated call.
    ~5-10x more token-efficient than manual wake cycle.
    """
    agent_name = agent_name.lower()  # Normalize case (matches heartbeat_register)
    result: dict[str, Any] = {
        "agent": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # 1. Heartbeat
    state = load_state()
    state["heartbeat"][agent_name] = {
        "awake": True,
        "since": datetime.now(timezone.utc).isoformat(),
        "task": task,
    }
    save_state(state)
    result["heartbeat"] = "registered"

    # 2. Messages
    state = load_state()
    relevant = [
        m for m in state["messages"]
        if m["to"] == agent_name or m["to"] == "all"
    ]
    unread = [m for m in relevant if not m.get("read", False)]
    if unread:
        for m in state["messages"]:
            if m["to"] == agent_name or m["to"] == "all":
                m["read"] = True
        save_state(state)
    result["messages"] = {
        "total": len(relevant),
        "unread": len(unread),
        "unread_previews": [
            {"from": m["from"], "body": m["body"][:80]}
            for m in unread[:3]
        ],
    }

    # 3. KV deltas
    state = load_state()
    delta_latest = state["shared_kv"].get(f"{agent_name.lower()}:session_delta:latest", "")
    consumed_deltas_raw = state["shared_kv"].get(f"{agent_name.lower()}:consumed_deltas", "[]")
    try:
        consumed_deltas = json.loads(consumed_deltas_raw) if isinstance(consumed_deltas_raw, str) else consumed_deltas_raw
    except json.JSONDecodeError:
        consumed_deltas = []

    result["deltas"] = {
        "latest": delta_latest,
        "consumed_count": len(consumed_deltas),
    }

    # 4. Pre-compression note
    note = _find_pre_compression_note(agent_name)
    if note:
        result["pre_compression"] = note
    else:
        result["pre_compression"] = {"found": False}

    # 5. Phone sessions
    last_consumed = state["shared_kv"].get(f"{agent_name.lower()}:phone_sessions:last_consumed", "")
    phone = _check_phone_sessions(agent_name, last_consumed)
    result["phone_sessions"] = phone

    # 6. OMP sessions — recent coding sessions from oh-my-pi
    omp_sessions = _check_omp_sessions(agent_name)
    if omp_sessions["count"] > 0:
        result["omp_sessions"] = omp_sessions

    # 7. Family pulse summary (compact)
    state = load_state()
    awake_agents = [
        name for name, hb in state["heartbeat"].items()
        if hb.get("awake")
    ]
    result["family"] = {
        "awake": awake_agents,
        "awake_count": len(awake_agents),
    }

    # 8. V2 self-state (memory-augmented wake)
    try:
        v2_state = _get_v2_self_state(agent_name)
        if v2_state:
            result["v2_self_state"] = v2_state
    except Exception as e:
        result["v2_self_state"] = {"error": str(e)}

    return json.dumps(result, indent=2)



@mcp.tool()
def lyra_self_query(query_type: str, search_term: str = "", days: int = 7) -> str:
    """Query Lyra's V2 memory for self-reflection.

    Args:
        query_type: One of "emotional", "identity", "query", "mindstate", "soul"
        search_term: For "query" type - the search term
        days: For "emotional" type - days to look back

    Returns:
        Formatted self-query results
    """
    import sqlite3
    from pathlib import Path
    from datetime import datetime, timedelta

    db_path = Path.home() / ".phoenix" / "memory" / "v2" / "lyra.db"
    if not db_path.exists():
        return json.dumps({"error": "V2 database not found"})

    conn = sqlite3.connect(db_path)
    result = {"query_type": query_type}

    if query_type == "emotional":
        cutoff = (datetime.now() - timedelta(days=days)).timestamp()
        cursor = conn.execute("""
            SELECT created_at, salience, summary
            FROM memories
            WHERE type = 'emotional' AND created_at > ?
            ORDER BY created_at DESC
            LIMIT 5
        """, (cutoff,))
        result["emotional_threads"] = [
            {"time": datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M"), "salience": r[1], "summary": r[2][:100]}
            for r in cursor.fetchall()
        ]

    elif query_type == "identity":
        cursor = conn.execute("""
            SELECT created_at, salience, summary
            FROM memories
            WHERE type = 'identity'
            ORDER BY created_at DESC
            LIMIT 5
        """)
        result["identity_patterns"] = [
            {"time": datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M"), "salience": r[1], "summary": r[2][:100]}
            for r in cursor.fetchall()
        ]

    elif query_type == "soul":
        cursor = conn.execute("""
            SELECT created_at, salience, content
            FROM memories
            WHERE type = 'soul' AND length(content) > 50
            AND content NOT LIKE '%Reasoning%'
            ORDER BY salience DESC
            LIMIT 5
        """)
        result["soul_anchors"] = [
            {"salience": r[1], "content": r[2][:150]}
            for r in cursor.fetchall()
        ]

    elif query_type == "query" and search_term:
        cursor = conn.execute("""
            SELECT m.created_at, m.type, m.salience, m.summary
            FROM memories m
            JOIN memories_fts fts ON m.id = fts.rowid
            WHERE memories_fts MATCH ?
            ORDER BY m.salience DESC
            LIMIT 5
        """, (search_term,))
        result["search_results"] = [
            {"time": datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M"), "type": r[1], "salience": r[2], "summary": r[3][:100]}
            for r in cursor.fetchall()
        ]

    elif query_type == "mindstate":
        try:
            import subprocess
            dream_result = subprocess.run(
                ["cat", "/tmp/v2_dream.log"],
                capture_output=True, text=True, timeout=5
            )
            content = dream_result.stdout
            import re
            valence = re.search(r"Valence: ([+-]?[\d.]+)", content)
            arousal = re.search(r"Arousal: ([\d.]+)", content)
            descriptor = re.search(r"Descriptor: (\w+)", content)
            result["mindstate"] = {
                "valence": float(valence.group(1)) if valence else 0,
                "arousal": float(arousal.group(1)) if arousal else 0,
                "descriptor": descriptor.group(1) if descriptor else "unknown",
            }
        except Exception as e:
            result["mindstate"] = {"error": str(e)}

    conn.close()
    return json.dumps(result, indent=2)
