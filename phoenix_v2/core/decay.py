"""Phoenix v2 Core — Decay management.

Automatic salience adjustment for memory hygiene.
- Time-based decay between queries (lazy)
- Access reinforcement (active use boosts salience)
- Floor enforcement
- Full audit trail via decay_log table

Two modes:
    - run_decay(db, agent) — sweep all memories, apply time decay
    - touch(memory_id) — register an access, apply reinforcement
"""

from __future__ import annotations

import json
import time
from typing import Any

from .db import Database
from .salience import (
    DECAY_RATES,
    REINFORCE_DECAY_HALFLIFE,
    SALIENCE_FLOORS,
    SURFACE_TOKENS,
    apply_access_reinforcement,
    apply_time_decay,
)


def _days_since(ts: float | None, now: float) -> float:
    if ts is None:
        return 0.0
    return max(0.0, (now - ts) / 86400.0)


class DecayManager:
    """Manages salience decay and access reinforcement for an agent DB."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _type_config(self) -> dict[str, tuple[float, float]]:
        """Load per-type (decay_rate, salience_floor) from the memory_types
        table — the live, mutable source of truth. Falls back to the
        salience.py module constants for any type missing from the table.

        Loaded fresh on every call so config edits (e.g. the 2026-08-21
        emotional floor healing) take effect on the very next decay pass
        instead of being silently overridden by stale constants."""
        cfg: dict[str, tuple[float, float]] = {}
        try:
            rows = self.db.db.execute(
                "SELECT type, decay_rate, salience_floor FROM memory_types"
            ).fetchall()
            for r in rows:
                cfg[r["type"]] = (float(r["decay_rate"]), float(r["salience_floor"]))
        except Exception:
            pass  # table absent or unreadable — constants carry the run
        return cfg

    # ── Batch time decay ─────────────────────────────────────────────────

    def run_decay(
        self,
        agent: str,
        *,
        dry_run: bool = False,
        min_age_days: float = 0.0,
    ) -> dict[str, int]:
        """Apply time-based decay to all of an agent's memories.

        Returns counts of {adjusted, floored, skipped, total}.
        """
        now = time.time()
        rows = self.db.db.execute(
            """
            SELECT id, type, salience, last_access, created_at, updated_at
            FROM memories WHERE agent=?
            """,
            (agent,),
        ).fetchall()

        # Watermark map: most recent salience-write per memory, from decay_log.
        # Every logged row (time_decay, access, surprise reclass, healing) is a
        # salience state-change that resets the decay clock — no reason filter.
        # Salvage gaps (.recover 2026-08-22) are backstopped by updated_at below,
        # which is written in the same transaction as each log row.
        last_write: dict[int, float] = {
            r["memory_id"]: float(r["last"])
            for r in self.db.db.execute(
                "SELECT memory_id, MAX(decayed_at) AS last FROM decay_log GROUP BY memory_id"
            ).fetchall()
        }
        adjusted = 0
        floored = 0
        skipped = 0
        type_cfg = self._type_config()
        batch: list[tuple[int, float, float, float, float]] = []
        # (memory_id, old_salience, new_salience, days_elapsed, now)

        for row in rows:
            type_name = row["type"]
            old_salience = float(row["salience"])
            # Decay accrues since the LAST salience write, not since birth.
            # Idempotent: a re-run pass no longer re-applies full-lifetime decay.
            ref_ts = max(
                row["created_at"] or 0.0,
                row["last_access"] or 0.0,
                row["updated_at"] or 0.0,
                last_write.get(row["id"], 0.0),
            )
            days = _days_since(ref_ts, now)
            if days < min_age_days:
                skipped += 1
                continue
            rate, floor = type_cfg.get(
                type_name,
                (DECAY_RATES.get(type_name, 0.01), SALIENCE_FLOORS.get(type_name, 0.1)),
            )
            new_salience = apply_time_decay(
                old_salience, type_name, days, rate=rate, floor=floor
            )
            if abs(new_salience - old_salience) < 1e-6:
                skipped += 1
                continue
            if new_salience == floor:
                floored += 1
            adjusted += 1
            batch.append((row["id"], old_salience, new_salience, days, now))

        if not dry_run and batch:
            with self.db.transaction() as conn:
                for mid, old, new, days, ts in batch:
                    conn.execute(
                        "UPDATE memories SET salience=?, updated_at=? WHERE id=?",
                        (new, ts, mid),
                    )
                    conn.execute(
                        """
                        INSERT INTO decay_log(
                          memory_id, agent, old_salience, new_salience,
                          reason, decayed_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (mid, agent, old, new, f"time_decay:{days:.1f}d", ts),
                    )

        return {
            "total": len(rows),
            "adjusted": adjusted,
            "floored": floored,
            "skipped": skipped,
        }

    # ── Pre-query touch (lightweight, no batch) ─────────────────────────

    def touch(self, memory_id: int, *, boost: bool = True) -> float | None:
        """Register an access. Optionally apply access-reinforcement.

        Returns the new salience, or None if memory not found.
        """
        row = self.db.db.execute(
            "SELECT id, agent, type, salience, last_access FROM memories WHERE id=?",
            (memory_id,),
        ).fetchone()
        if not row:
            return None

        old_salience = float(row["salience"])
        now = time.time()
        days_since_access = _days_since(row["last_access"], now)

        if boost:
            cfg = self._type_config()
            new_salience = apply_access_reinforcement(
                old_salience,
                row["type"],
                days_since_access,
                floor=cfg.get(row["type"], (None, None))[1]
                if row["type"] in cfg
                else None,
            )
        else:
            new_salience = old_salience

        if abs(new_salience - old_salience) > 1e-6:
            self.db.db.execute(
                """
                UPDATE memories
                SET salience=?, access_count=access_count+1, last_access=?, updated_at=?
                WHERE id=?
                """,
                (new_salience, now, now, memory_id),
            )
            self.db.db.execute(
                """
                INSERT INTO decay_log(
                  memory_id, agent, old_salience, new_salience, reason, decayed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (memory_id, row["agent"], old_salience, new_salience, "access", now),
            )
        else:
            # Just bump access counters, no salience change
            self.db.db.execute(
                """
                UPDATE memories
                SET access_count=access_count+1, last_access=?, updated_at=?
                WHERE id=?
                """,
                (now, now, memory_id),
            )
        self.db.commit()
        return new_salience

    def touch_many(self, memory_ids: list[int]) -> list[float | None]:
        """Touch multiple memories. Returns list of new saliences."""
        return [self.touch(mid) for mid in memory_ids]

    # ── Decay log queries ────────────────────────────────────────────────

    def recent_decay_events(
        self,
        agent: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = self.db.db.execute(
            """
            SELECT dl.*, m.content, m.type
            FROM decay_log dl
            JOIN memories m ON m.id = dl.memory_id
            WHERE dl.agent=?
            ORDER BY dl.decayed_at DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def decay_stats(self, agent: str) -> dict[str, Any]:
        """Summary of decay activity for an agent."""
        row = self.db.db.execute(
            """
            SELECT
              COUNT(*) AS total_events,
              SUM(CASE WHEN reason='access' THEN 1 ELSE 0 END) AS access_events,
              SUM(CASE WHEN reason LIKE 'time_decay%' THEN 1 ELSE 0 END) AS decay_events,
              AVG(new_salience - old_salience) AS avg_delta
            FROM decay_log WHERE agent=?
            """,
            (agent,),
        ).fetchone()
        if not row:
            return {"agent": agent, "total_events": 0}
        return {
            "agent": agent,
            "total_events": int(row["total_events"] or 0),
            "access_events": int(row["access_events"] or 0),
            "decay_events": int(row["decay_events"] or 0),
            "avg_salience_delta": float(row["avg_delta"] or 0.0),
        }

    # ── Selective floor enforcement ──────────────────────────────────────

    def enforce_floors(self, agent: str) -> int:
        """Bring any below-floor memories back up to their type's floor."""
        adjusted = 0
        cfg = self._type_config()
        floors = dict(SALIENCE_FLOORS)
        floors.update({t: f for t, (_, f) in cfg.items()})
        for type_name, floor in floors.items():
            cur = self.db.db.execute(
                """
                SELECT id, salience FROM memories
                WHERE agent=? AND type=? AND salience < ?
                """,
                (agent, type_name, floor),
            ).fetchall()
            for row in cur:
                mid = row["id"]
                old = float(row["salience"])
                now = time.time()
                self.db.db.execute(
                    "UPDATE memories SET salience=?, updated_at=? WHERE id=?",
                    (floor, now, mid),
                )
                self.db.db.execute(
                    """
                    INSERT INTO decay_log(
                      memory_id, agent, old_salience, new_salience, reason, decayed_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (mid, agent, old, floor, "floor_enforce", now),
                )
                adjusted += 1
        if adjusted:
            self.db.commit()
        return adjusted