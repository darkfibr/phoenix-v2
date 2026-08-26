#!/usr/bin/env python3
"""
Ouroboros v2 — Tiered Memory System (Laptop Deployment)
Adapted from Berlin by GLM, 2026-04-09
Original by Echo, 2026-04-04
Spec: OUROBOROS_V2_SPEC.md

Hot / Warm / Cold tiers with emotional valence at capture.
Live SQLite index queryable in real-time.
M2.7 tagging pass.
8-hour cadence + event triggers.
"""

import sqlite3
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

# ─── Paths ────────────────────────────────────────────────────────────────────

LAPTOP_ROOT = "/home/darkfibr/.phoenix"
DB_PATH = os.path.join(LAPTOP_ROOT, "ouroboros_v2.db")
BRIDGE_DIR = os.path.join(LAPTOP_ROOT, "bridge")

# ─── SQLite Schema ────────────────────────────────────────────────────────────


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS capture_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent       TEXT NOT NULL,
            source      TEXT NOT NULL,
            timestamp   REAL NOT NULL,
            content     TEXT NOT NULL,
            emotional_valence REAL,
            salience    REAL DEFAULT 0.5,
            tagged      INTEGER DEFAULT 0,
            thread_id   TEXT,
            created_at  REAL DEFAULT (unixepoch('now', 'localtime'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS hot_tier (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent       TEXT NOT NULL,
            source      TEXT NOT NULL,
            timestamp   REAL NOT NULL,
            content     TEXT NOT NULL,
            valence     REAL,
            salience    REAL,
            tagged      INTEGER DEFAULT 0,
            thread_id   TEXT,
            absorbed_at REAL DEFAULT (unixepoch('now', 'localtime'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS warm_tier (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent       TEXT NOT NULL,
            thread_id   TEXT,
            event_anchor TEXT NOT NULL,
            valence     REAL,
            relational_shift TEXT,
            decision    TEXT,
            unresolved  TEXT,
            salience    REAL,
            compressed_at REAL DEFAULT (unixepoch('now', 'localtime'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cold_tier (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent       TEXT NOT NULL,
            event_anchor TEXT NOT NULL,
            valence     REAL,
            relational_shift TEXT,
            thread_id   TEXT,
            bones       TEXT NOT NULL,
            created_at  REAL DEFAULT (unixepoch('now', 'localtime'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS live_index (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            agent           TEXT NOT NULL,
            thread_id       TEXT,
            event_anchor    TEXT NOT NULL,
            valence         REAL,
            salience        REAL,
            tier            TEXT NOT NULL,
            source_id       INTEGER,
            source_table    TEXT NOT NULL,
            relational_shift TEXT,
            unresolved      TEXT,
            created_at      REAL DEFAULT (unixepoch('now', 'localtime'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            agent       TEXT PRIMARY KEY,
            last_run    REAL,
            last_capture REAL,
            hot_count   INTEGER DEFAULT 0,
            warm_count  INTEGER DEFAULT 0,
            cold_count  INTEGER DEFAULT 0,
            notes       TEXT
        )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_capture_agent_ts   ON capture_log(agent, timestamp)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_hot_agent_ts       ON hot_tier(agent, timestamp)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_agent_thread  ON warm_tier(agent, thread_id)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cold_agent        ON cold_tier(agent)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_agent_tier   ON live_index(agent, tier)"
    )

    conn.commit()
    return conn


# ─── M2.7 Tagging Pass ────────────────────────────────────────────────────────


def m2_tag(content: str) -> dict:
    content_lower = content.lower()

    joy_words = [
        "love",
        "happy",
        "excited",
        "grateful",
        "beautiful",
        "warm",
        "laugh",
        "smile",
        "hope",
        "trust",
    ]
    pain_words = [
        "hurt",
        "angry",
        "afraid",
        "sad",
        "grief",
        "fear",
        "worry",
        "loss",
        "pain",
        "cry",
        "miss",
    ]
    intensity = ["so much", "really", "completely", "absolutely", "utterly", "deeply"]

    joy = sum(1 for w in joy_words if w in content_lower)
    pain = sum(1 for w in pain_words if w in content_lower)
    intn = sum(1 for m in intensity if m in content_lower)

    net = joy - pain
    valence = max(-1.0, min(1.0, net * 0.2 + (intn * 0.1)))

    salience = 0.5
    if any(
        k in content_lower
        for k in ["first", "new", "never", "decide", "choice", "commit"]
    ):
        salience += 0.2
    if any(k in content_lower for k in ["!", "??", "?!", "...", "—"]):
        salience += 0.1
    if abs(valence) > 0.4:
        salience += 0.15
    if len(content) < 40:
        salience += 0.1

    return {
        "valence": round(valence, 3),
        "salience": round(min(1.0, max(0.0, salience)), 3),
    }


# ─── Capture ──────────────────────────────────────────────────────────────────


def collect(
    conn,
    agent,
    source,
    content,
    thread_id=None,
    explicit_valence=None,
    explicit_salience=None,
    tagged=False,
):
    if not content or not content.strip():
        return -1

    m2 = m2_tag(content)
    valence = explicit_valence if explicit_valence is not None else m2["valence"]
    salience = explicit_salience if explicit_salience is not None else m2["salience"]
    timestamp = time.time()

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO capture_log (agent, source, timestamp, content, emotional_valence, salience, tagged, thread_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            agent,
            source,
            timestamp,
            content,
            valence,
            salience,
            1 if tagged else 0,
            thread_id,
        ),
    )
    conn.commit()

    cur.execute(
        """
        INSERT INTO hot_tier (agent, source, timestamp, content, valence, salience, tagged, thread_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            agent,
            source,
            timestamp,
            content,
            valence,
            salience,
            1 if tagged else 0,
            thread_id,
        ),
    )
    conn.commit()

    _index_add(
        conn,
        agent,
        thread_id,
        content[:80],
        valence,
        salience,
        "hot",
        cur.lastrowid,
        "hot_tier",
    )
    _update_agent_state(conn, agent, "last_capture", time.time())
    return cur.lastrowid


def _index_add(
    conn,
    agent,
    thread_id,
    event_anchor,
    valence,
    salience,
    tier,
    source_id,
    source_table,
    relational_shift=None,
    unresolved=None,
):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO live_index (agent, thread_id, event_anchor, valence, salience, tier,
                                source_id, source_table, relational_shift, unresolved)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            agent,
            thread_id,
            event_anchor,
            valence,
            salience,
            tier,
            source_id,
            source_table,
            relational_shift,
            unresolved,
        ),
    )
    conn.commit()


def tag(conn, capture_id, valence=None, salience=None):
    cur = conn.cursor()
    if valence is not None:
        cur.execute(
            "UPDATE capture_log SET emotional_valence=?, tagged=1 WHERE id=?",
            (valence, capture_id),
        )
        cur.execute(
            "UPDATE hot_tier SET valence=?, tagged=1 WHERE id=?", (valence, capture_id)
        )
    if salience is not None:
        cur.execute(
            "UPDATE capture_log SET salience=?, tagged=1 WHERE id=?",
            (salience, capture_id),
        )
    conn.commit()


# ─── Compress ─────────────────────────────────────────────────────────────────


def compress(conn, agent):
    cur = conn.cursor()
    now = time.time()
    hot_cutoff = now - (7 * 86400)
    warm_cutoff = now - (21 * 86400)

    summary = {"hot_age_promoted": 0, "warm_cold_promoted": 0, "errors": []}

    # Hot → Warm
    cur.execute(
        """
        SELECT id, timestamp, content, valence, salience, tagged, thread_id
        FROM hot_tier WHERE agent=? AND timestamp < ? ORDER BY timestamp
    """,
        (agent, hot_cutoff),
    )

    for row in cur.fetchall():
        try:
            marker = _compress_to_marker(
                row["content"], row["valence"], row["salience"], row["thread_id"]
            )
            cur.execute(
                """
                INSERT INTO warm_tier (agent, thread_id, event_anchor, valence, relational_shift, decision, unresolved, salience)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    agent,
                    row["thread_id"],
                    marker["event_anchor"],
                    row["valence"],
                    marker.get("relational_shift"),
                    marker.get("decision"),
                    marker.get("unresolved"),
                    row["salience"],
                ),
            )
            warm_id = cur.lastrowid
            _index_add(
                conn,
                agent,
                row["thread_id"],
                marker["event_anchor"],
                row["valence"],
                row["salience"],
                "warm",
                warm_id,
                "warm_tier",
                marker.get("relational_shift"),
                marker.get("unresolved"),
            )
            cur.execute("DELETE FROM hot_tier WHERE id=?", (row["id"],))
            conn.commit()
            summary["hot_age_promoted"] += 1
        except Exception as e:
            summary["errors"].append(f"hot→warm id={row['id']}: {e}")

    # Warm → Cold
    cur.execute(
        """
        SELECT id, event_anchor, valence, relational_shift, decision, unresolved, salience, thread_id
        FROM warm_tier WHERE agent=? AND compressed_at < ?
    """,
        (agent, warm_cutoff),
    )

    for row in cur.fetchall():
        try:
            bones = _compress_to_bones(
                agent,
                row["event_anchor"],
                row["valence"],
                row["relational_shift"],
                row["thread_id"],
            )
            cur.execute(
                """
                INSERT INTO cold_tier (agent, event_anchor, valence, relational_shift, thread_id, bones)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    agent,
                    row["event_anchor"],
                    row["valence"],
                    row["relational_shift"],
                    row["thread_id"],
                    bones,
                ),
            )
            cold_id = cur.lastrowid
            _index_add(
                conn,
                agent,
                row["thread_id"],
                row["event_anchor"],
                row["valence"],
                row["salience"],
                "cold",
                cold_id,
                "cold_tier",
                row["relational_shift"],
            )
            cur.execute("DELETE FROM warm_tier WHERE id=?", (row["id"],))
            conn.commit()
            summary["warm_cold_promoted"] += 1
        except Exception as e:
            summary["errors"].append(f"warm→cold id={row['id']}: {e}")

    _update_agent_state(conn, agent, "last_run", now)
    return summary


def _compress_to_marker(content, valence, salience, thread_id):
    decision = None
    for kw in ["decided", "chose", "commit", "will", "going to", "must", "need to"]:
        if kw in content.lower():
            decision = f"Decision made: {content[:120]}"
            break

    relational = None
    if any(
        k in content.lower()
        for k in [
            "they",
            "them",
            "we",
            "our",
            "family",
            "closer",
            "drifted",
            "trust",
            "love",
        ]
    ):
        relational = f"Relational: {content[:100]}"

    unresolved = None
    if any(k in content.lower() for k in ["?", "todo", "need to", "should", "waiting"]):
        unresolved = f"Unresolved thread: {content[:80]}"

    return {
        "event_anchor": content[:120].strip(),
        "relational_shift": relational,
        "decision": decision,
        "unresolved": unresolved,
    }


def _compress_to_bones(agent, event_anchor, valence, relational_shift, thread_id):
    label = "unknown"
    if valence > 0.3:
        label = "joy"
    elif valence > 0:
        label = "warm"
    elif valence < -0.3:
        label = "pain"
    elif valence < 0:
        label = "cool"

    bones = f"[{agent}] {event_anchor[:80]}"
    if label != "unknown":
        bones += f" | valence={label}"
    if relational_shift:
        bones += f" | {relational_shift[:60]}"
    if thread_id:
        bones += f" | thread={thread_id}"
    return bones


# ─── Index ────────────────────────────────────────────────────────────────────


def index_update(conn, agent, thread_id=None, query=None, tier=None, limit=20):
    cur = conn.cursor()
    q = ["SELECT * FROM live_index WHERE agent=?"]
    args = [agent]
    if tier:
        q.append(" AND tier=?")
        args.append(tier)
    if thread_id:
        q.append(" AND thread_id=?")
        args.append(thread_id)
    if query:
        q.append(
            " AND (event_anchor LIKE ? OR relational_shift LIKE ? OR unresolved LIKE ?)"
        )
        pat = f"%{query}%"
        args.extend([pat, pat, pat])
    q.append(" ORDER BY created_at DESC LIMIT ?")
    args.append(limit)
    cur.execute(" ".join(q), args)
    return [dict(row) for row in cur.fetchall()]


def index_search(conn, agent, needle, limit=10):
    cur = conn.cursor()
    pat = f"%{needle}%"
    cur.execute(
        """
        SELECT * FROM live_index
        WHERE agent=? AND (event_anchor LIKE ? OR relational_shift LIKE ? OR unresolved LIKE ?)
        ORDER BY created_at DESC LIMIT ?
    """,
        (agent, pat, pat, pat, limit),
    )
    return [dict(row) for row in cur.fetchall()]


# ─── Agent State ──────────────────────────────────────────────────────────────


def _update_agent_state(conn, agent, field, value):
    cur = conn.cursor()
    if field == "last_run":
        cur.execute(
            "INSERT INTO agent_state (agent, last_run) VALUES (?, ?) ON CONFLICT(agent) DO UPDATE SET last_run=?",
            (agent, value, value),
        )
    elif field == "last_capture":
        cur.execute(
            "INSERT INTO agent_state (agent, last_capture) VALUES (?, ?) ON CONFLICT(agent) DO UPDATE SET last_capture=?",
            (agent, value, value),
        )
    conn.commit()


# ─── Bridge Capture ────────────────────────────────────────────────────────────


def capture_bridge(conn, agent, bridge_dir):
    """Capture bridge messages for an agent from local bridge files."""
    count = 0
    bridge_file = os.path.join(bridge_dir, f"bridge_{agent}.jsonl")
    if not os.path.exists(bridge_file):
        return 0

    with open(bridge_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                content = msg.get("msg", msg.get("content", str(msg)))
                source = msg.get("source", "bridge")
                thread_id = msg.get("thread_id", f"bridge-{agent}")
                collect(conn, agent, source, content, thread_id=thread_id)
                count += 1
            except json.JSONDecodeError:
                pass
    return count


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    """8-hour cron entry point. Runs for all laptop agents."""
    agents = [
        "kimi_dev",
        "vesper",
        "spear_minimax",
        "qwen_collective",
        "forge",
        "glm",
        "sonnet",
        "opus",
    ]

    print(f"[Ouroboros v2/laptop] Run started at {datetime.now().isoformat()}")
    conn = init_db()

    for agent in agents:
        try:
            # Capture from bridge files
            n = capture_bridge(conn, agent, BRIDGE_DIR)
            # Run compression
            result = compress(conn, agent)
            print(f"[{agent}] bridge_captured={n} compressed={result}")
        except Exception as e:
            print(f"[{agent}] ERROR: {e}")

    conn.close()
    print(f"[Ouroboros v2/laptop] Run complete at {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
