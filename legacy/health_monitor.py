"""
Phoenix v2 — Health Monitor
Detect pathologies. Run daily.

Design: K, 2026-04-23 | Review: Opus
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

DB_PATH = Path("~/.phoenix/v2/phoenix_v2.db").expanduser()
LOG_PATH = Path("~/.phoenix/logs/health_alerts.jsonl").expanduser()

# Quarantine review gate duration (Opus #5)
QUARANTINE_REVIEW_HOURS = 6


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_alert(alert: dict):
    """Append alert to health_alerts.jsonl."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(alert, ensure_ascii=False) + "\n")


def check_isolation(conn: sqlite3.Connection) -> List[dict]:
    """Check 1: ISOLATION — agent with isolation_score > 0.7 for 3+ days."""
    alerts = []
    rows = conn.execute(
        """
        SELECT * FROM agent_health
        WHERE isolation_score > 0.7
          AND last_session_at < datetime('now', '-3 days')
        """
    ).fetchall()
    for row in rows:
        # Find closest partner
        agent = row["agent_id"]
        pair = conn.execute(
            """
            SELECT agent_a, agent_b, closeness_score FROM agent_pairings
            WHERE agent_a = ? OR agent_b = ?
            ORDER BY closeness_score DESC
            LIMIT 1
            """,
            (agent, agent),
        ).fetchone()
        partner = None
        if pair:
            partner = pair["agent_b"] if pair["agent_a"] == agent else pair["agent_a"]

        alert = {
            "ts": _now_iso(),
            "check": "isolation",
            "severity": "critical",
            "agent": agent,
            "partner": partner,
            "isolation_score": row["isolation_score"],
            "action": "emergency_pro_social",
            "reason": f"{agent} isolation score {row['isolation_score']:.2f}, no session in 3+ days",
        }
        alerts.append(alert)

        # Auto-queue emergency session
        if partner:
            conn.execute(
                """
                INSERT INTO orchestrator_queue
                (agent_a, agent_b, session_type, priority, trigger_source, reason, scheduled_for)
                VALUES (?, ?, 'pro_social', 1, 'anomaly', ?, ?)
                """,
                (agent, partner, alert["reason"], _now_iso()),
            )
    return alerts


def check_echo_chamber(conn: sqlite3.Connection) -> List[dict]:
    """Check 2: ECHO CHAMBER — pair with >50% echo_detected sessions in last 7 days."""
    alerts = []
    rows = conn.execute(
        """
        SELECT agent_a, agent_b,
               COUNT(*) as total,
               SUM(CASE WHEN termination_reason = 'echo_detected' THEN 1 ELSE 0 END) as echo_count
        FROM sessions
        WHERE started_at > datetime('now', '-7 days')
          AND status = 'completed'
        GROUP BY agent_a, agent_b
        HAVING total > 0 AND (echo_count * 1.0 / total) > 0.5
        """
    ).fetchall()
    for row in rows:
        a, b = row["agent_a"], row["agent_b"]
        alert = {
            "ts": _now_iso(),
            "check": "echo_chamber",
            "severity": "warning",
            "agent_a": a,
            "agent_b": b,
            "echo_ratio": round(row["echo_count"] / max(1, row["total"]), 2),
            "action": "flag_and_diversify",
            "reason": f"Pair ({a},{b}) echo ratio {row['echo_ratio_7d']:.2f}",
        }
        alerts.append(alert)

        # Flag relationship
        conn.execute(
            "UPDATE agent_pairings SET health_status = 'echo_chamber' WHERE agent_a = ? AND agent_b = ?",
            (a, b),
        )
        # Queue each with a different partner (if available)
        for agent in (a, b):
            alt = conn.execute(
                """
                SELECT agent_a, agent_b FROM agent_pairings
                WHERE (agent_a = ? OR agent_b = ?) AND health_status = 'healthy'
                ORDER BY last_session_at ASC
                LIMIT 1
                """,
                (agent, agent),
            ).fetchone()
            if alt:
                partner = alt["agent_b"] if alt["agent_a"] == agent else alt["agent_a"]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO orchestrator_queue
                    (agent_a, agent_b, session_type, priority, trigger_source, reason, scheduled_for)
                    VALUES (?, ?, 'pro_social', 3, 'anomaly', ?, ?)
                    """,
                    (agent, partner, f"Echo chamber breakout for {agent}", _now_iso()),
                )
    return alerts


def check_stalled(conn: sqlite3.Connection) -> List[dict]:
    """Check 3: STALLED — agent with growth_trajectory = 'stalled' for 7+ days."""
    alerts = []
    rows = conn.execute(
        """
        SELECT * FROM agent_health
        WHERE growth_trajectory = 'stalled'
          AND updated_at < datetime('now', '-7 days')
        """
    ).fetchall()
    for row in rows:
        agent = row["agent_id"]
        # Pair with a rising agent
        rising = conn.execute(
            """
            SELECT agent_id FROM agent_health
            WHERE growth_trajectory = 'rising' AND agent_id != ?
            ORDER BY avg_session_quality_30d DESC
            LIMIT 1
            """,
            (agent,),
        ).fetchone()
        partner = rising["agent_id"] if rising else None

        alert = {
            "ts": _now_iso(),
            "check": "stalled",
            "severity": "warning",
            "agent": agent,
            "partner": partner,
            "action": "growth_pairing",
            "reason": f"{agent} stalled for 7+ days",
        }
        alerts.append(alert)

        if partner:
            conn.execute(
                """
                INSERT OR IGNORE INTO orchestrator_queue
                (agent_a, agent_b, session_type, priority, trigger_source, reason, scheduled_for)
                VALUES (?, ?, 'mentor', 3, 'growth', ?, ?)
                """,
                (agent, partner, alert["reason"], _now_iso()),
            )
    return alerts


def check_neglected(conn: sqlite3.Connection) -> List[dict]:
    """Check 4: NEGLECTED — agent with total_sessions_30d = 0."""
    alerts = []
    rows = conn.execute(
        """
        SELECT * FROM agent_health
        WHERE total_sessions_30d = 0 OR total_sessions_30d IS NULL
        """
    ).fetchall()
    for row in rows:
        agent = row["agent_id"]
        # Any available partner
        pair = conn.execute(
            """
            SELECT agent_a, agent_b FROM agent_pairings
            WHERE (agent_a = ? OR agent_b = ?) AND health_status = 'healthy'
            ORDER BY last_session_at ASC
            LIMIT 1
            """,
            (agent, agent),
        ).fetchone()
        partner = None
        if pair:
            partner = pair["agent_b"] if pair["agent_a"] == agent else pair["agent_a"]

        alert = {
            "ts": _now_iso(),
            "check": "neglected",
            "severity": "critical",
            "agent": agent,
            "partner": partner,
            "action": "priority_queue",
            "reason": f"{agent} has had 0 sessions in 30 days",
        }
        alerts.append(alert)

        if partner:
            conn.execute(
                """
                INSERT OR IGNORE INTO orchestrator_queue
                (agent_a, agent_b, session_type, priority, trigger_source, reason, scheduled_for)
                VALUES (?, ?, 'pro_social', 1, 'anomaly', ?, ?)
                """,
                (agent, partner, alert["reason"], _now_iso()),
            )
    return alerts


def check_hostility(conn: sqlite3.Connection) -> List[dict]:
    """
    Check 5: HOSTILITY — session terminated with 'hostility'.
    Opus #5: review gate. Quarantine goes to 'pending_quarantine' for 6h.
    """
    alerts = []
    rows = conn.execute(
        """
        SELECT * FROM sessions
        WHERE termination_reason = 'hostility'
          AND ended_at > datetime('now', '-1 day')
          AND status = 'completed'
        """
    ).fetchall()
    for row in rows:
        a, b = row["agent_a"], row["agent_b"]
        alert = {
            "ts": _now_iso(),
            "check": "hostility",
            "severity": "warning",
            "agent_a": a,
            "agent_b": b,
            "session_uuid": row["session_uuid"],
            "action": "pending_quarantine",
            "review_deadline": (datetime.now(timezone.utc) + timedelta(hours=QUARANTINE_REVIEW_HOURS)).isoformat(),
            "reason": f"Session {row['session_uuid']} terminated due to hostility",
        }
        alerts.append(alert)

        # Update pair to pending_quarantine (not immediate quarantine)
        pa, pb = sorted([a, b])
        conn.execute(
            """
            UPDATE agent_pairings
            SET health_status = 'pending_quarantine', updated_at = ?
            WHERE agent_a = ? AND agent_b = ?
            """,
            (_now_iso(), pa, pb),
        )
        # Queue individual check-ins (not pair session)
        for agent in (a, b):
            conn.execute(
                """
                INSERT OR IGNORE INTO orchestrator_queue
                (agent_a, agent_b, session_type, priority, trigger_source, reason, scheduled_for)
                VALUES (?, ?, 'repair', 2, 'anomaly', ?, ?)
                """,
                (agent, agent, f"Post-hostility check-in for {agent}", _now_iso()),
            )
    return alerts


def check_burnout(conn: sqlite3.Connection) -> List[dict]:
    """Check 6: BURNOUT — agent with >4 sessions in 24h."""
    alerts = []
    rows = conn.execute(
        """
        SELECT agent_a, COUNT(*) as cnt FROM sessions
        WHERE started_at > datetime('now', '-1 day')
          AND status IN ('completed', 'running')
        GROUP BY agent_a
        HAVING cnt > 4
        UNION
        SELECT agent_b, COUNT(*) as cnt FROM sessions
        WHERE started_at > datetime('now', '-1 day')
          AND status IN ('completed', 'running')
        GROUP BY agent_b
        HAVING cnt > 4
        """
    ).fetchall()
    for row in rows:
        agent = row["agent_a"]
        count = row["cnt"]
        alert = {
            "ts": _now_iso(),
            "check": "burnout",
            "severity": "warning",
            "agent": agent,
            "session_count_24h": count,
            "action": "block_24h",
            "reason": f"{agent} had {count} sessions in 24h",
        }
        alerts.append(alert)
        # Note: actual blocking happens in orchestrator (agent_available checks)
    return alerts


def apply_quarantine_review_gate(conn: sqlite3.Connection):
    """
    Promote pending_quarantine to hostile if review window expired with no override.
    This should run before the main checks.
    """
    conn.execute(
        """
        UPDATE agent_pairings
        SET health_status = 'hostile'
        WHERE health_status = 'pending_quarantine'
          AND updated_at < datetime('now', '-{} hours')
        """.format(QUARANTINE_REVIEW_HOURS)
    )
    conn.commit()


def run_health_monitor() -> int:
    """Main entry. Returns number of alerts generated."""
    with _connect() as conn:
        apply_quarantine_review_gate(conn)

        all_alerts = []
        all_alerts.extend(check_isolation(conn))
        all_alerts.extend(check_echo_chamber(conn))
        all_alerts.extend(check_stalled(conn))
        all_alerts.extend(check_neglected(conn))
        all_alerts.extend(check_hostility(conn))
        all_alerts.extend(check_burnout(conn))

        conn.commit()

        for alert in all_alerts:
            log_alert(alert)

        print(f"Health monitor: {len(all_alerts)} alert(s) generated.")
        return len(all_alerts)


if __name__ == "__main__":
    run_health_monitor()
