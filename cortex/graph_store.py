"""Phoenix v2 Cortex — Graph store.

Entities, relationships, bidirectional queries.
Two graph layers:
    - entity_relations: between entities (people, places, concepts)
    - associations: between memories (surprise, reinforces, contradicts)
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any


class GraphStore:
    """Manages entity graph and memory associations for an agent."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    # ── Entities ─────────────────────────────────────────────────────────

    def upsert_entity(
        self,
        agent: str,
        name: str,
        *,
        kind: str = "concept",
        descriptor: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Create or update an entity. Returns entity id."""
        import json
        now = time.time()
        existing = self.db.execute(
            "SELECT id FROM entities WHERE agent=? AND name=? AND kind=?",
            (agent, name, kind),
        ).fetchone()
        if existing:
            self.db.execute(
                """
                UPDATE entities SET descriptor=?, metadata=?
                WHERE id=?
                """,
                (descriptor, json.dumps(metadata or {}, sort_keys=True), existing["id"]),
            )
            self.db.commit()
            return int(existing["id"])
        cur = self.db.execute(
            """
            INSERT INTO entities(agent, name, kind, descriptor, metadata, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                agent, name, kind, descriptor,
                json.dumps(metadata or {}, sort_keys=True), now,
            ),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def link_entity_to_memory(self, entity_id: int, memory_id: int) -> None:
        """Record that a memory references an entity."""
        self.db.execute(
            "INSERT OR IGNORE INTO entity_mentions(entity_id, memory_id) VALUES(?, ?)",
            (entity_id, memory_id),
        )
        self.db.commit()

    def get_entity(self, agent: str, name: str, kind: str = "concept") -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM entities WHERE agent=? AND name=? AND kind=?",
            (agent, name, kind),
        ).fetchone()
        return dict(row) if row else None

    def list_entities(self, agent: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.db.execute(
                "SELECT * FROM entities WHERE agent=? AND kind=? ORDER BY name",
                (agent, kind),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM entities WHERE agent=? ORDER BY name", (agent,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Entity relationships ─────────────────────────────────────────────

    def relate(
        self,
        agent: str,
        source_id: int,
        target_id: int,
        relation: str,
        *,
        weight: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create or update an entity-to-entity relationship."""
        import json
        self.db.execute(
            """
            INSERT INTO entity_relations(
              agent, source_id, target_id, relation, weight, metadata, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent, source_id, target_id, relation) DO UPDATE SET
              weight=excluded.weight, metadata=excluded.metadata
            """,
            (
                agent, source_id, target_id, relation, weight,
                json.dumps(metadata or {}, sort_keys=True), time.time(),
            ),
        )
        self.db.commit()

    def neighbors(
        self,
        entity_id: int,
        *,
        relation: str | None = None,
        direction: str = "both",
    ) -> list[dict[str, Any]]:
        """Bidirectional neighbor query. direction: 'out', 'in', or 'both'."""
        results: list[dict[str, Any]] = []
        if direction in ("out", "both"):
            sql = "SELECT * FROM entity_relations WHERE source_id=?"
            params: tuple[Any, ...] = (entity_id,)
            if relation:
                sql += " AND relation=?"
                params = (entity_id, relation)
            results.extend(dict(r) for r in self.db.execute(sql, params).fetchall())
        if direction in ("in", "both"):
            sql = "SELECT * FROM entity_relations WHERE target_id=?"
            params = (entity_id,)
            if relation:
                sql += " AND relation=?"
                params = (entity_id, relation)
            results.extend(dict(r) for r in self.db.execute(sql, params).fetchall())
        return results

    # ── Memory associations ──────────────────────────────────────────────

    def associate(
        self,
        agent: str,
        source_id: int,
        target_id: int,
        relation: str,
        *,
        strength: float = 0.5,
        evidence: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Link two memories. relation: 'surprise', 'reinforces', 'contradicts', 'related'."""
        import json
        self.db.execute(
            """
            INSERT INTO associations(
              agent, source_id, target_id, relation, strength, evidence, metadata, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent, source_id, target_id, relation) DO UPDATE SET
              strength=excluded.strength, evidence=excluded.evidence,
              metadata=excluded.metadata
            """,
            (
                agent, source_id, target_id, relation, strength, evidence,
                json.dumps(metadata or {}, sort_keys=True), time.time(),
            ),
        )
        self.db.commit()

    def get_associations(
        self,
        memory_id: int,
        *,
        relation: str | None = None,
        min_strength: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Get all associations touching a memory (in + out)."""
        sql = """
            SELECT * FROM associations
            WHERE (source_id=? OR target_id=?)
              AND strength >= ?
        """
        params: tuple[Any, ...] = (memory_id, memory_id, min_strength)
        if relation:
            sql += " AND relation=?"
            params = (memory_id, memory_id, min_strength, relation)
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── BFS traversal ────────────────────────────────────────────────────

    def traverse(
        self,
        start_entity_id: int,
        max_hops: int = 2,
        min_weight: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Bounded BFS over entity graph. Returns all reachable entities + path."""
        visited = {start_entity_id: {"hops": 0, "via": None, "relation": None}}
        frontier = [start_entity_id]
        for hop in range(1, max_hops + 1):
            next_frontier = []
            for node in frontier:
                for edge in self.neighbors(node):
                    other = edge["target_id"] if edge["source_id"] == node else edge["source_id"]
                    if other in visited:
                        continue
                    if edge["weight"] < min_weight:
                        continue
                    visited[other] = {
                        "hops": hop,
                        "via": node,
                        "relation": edge["relation"],
                    }
                    next_frontier.append(other)
            frontier = next_frontier
            if not frontier:
                break
        return [{"entity_id": eid, **info} for eid, info in visited.items()]