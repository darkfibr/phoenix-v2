"""Phoenix v2 Depth — Relationship graph analysis.

Entity co-occurrence patterns, community detection, bond strength.
Answers: who matters to whom? Which concepts cluster together?
What relationships bridge separate worlds?
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from ..core.db import Database


class RelationshipAnalyzer:
    """Analyzes entity co-occurrence and relationship patterns."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def co_occurrence_matrix(
        self,
        agent: str,
        *,
        min_co_occurrences: int = 2,
    ) -> dict[tuple[int, int], int]:
        """Build entity co-occurrence counts from shared memories."""
        # For each memory, get its entities. Entities that co-occur in
        # the same memory are edges.
        rows = self.db.db.execute(
            """
            SELECT em.memory_id, em.entity_id, e.name, e.kind
            FROM entity_mentions em
            JOIN entities e ON e.id = em.entity_id
            JOIN memories m ON m.id = em.memory_id
            WHERE m.agent=?
            """,
            (agent,),
        ).fetchall()

        # Group by memory
        by_memory: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_memory[row["memory_id"]].append({
                "entity_id": row["entity_id"],
                "name": row["name"],
                "kind": row["kind"],
            })

        # Count co-occurrences
        pair_counts: Counter[tuple[int, int]] = Counter()
        for entities in by_memory.values():
            for i, a in enumerate(entities):
                for b in entities[i + 1:]:
                    pair = (min(a["entity_id"], b["entity_id"]),
                            max(a["entity_id"], b["entity_id"]))
                    pair_counts[pair] += 1

        # Filter by minimum threshold
        return {pair: count for pair, count in pair_counts.items() if count >= min_co_occurrences}

    def bond_strength(
        self,
        agent: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return strongest entity bonds (most frequent co-occurrences)."""
        matrix = self.co_occurrence_matrix(agent)
        # Sort by co-occurrence count
        ranked = sorted(matrix.items(), key=lambda x: x[1], reverse=True)[:limit]

        out = []
        for (id_a, id_b), count in ranked:
            a = self.db.db.execute(
                "SELECT name, kind FROM entities WHERE id=?", (id_a,)
            ).fetchone()
            b = self.db.db.execute(
                "SELECT name, kind FROM entities WHERE id=?", (id_b,)
            ).fetchone()
            if a and b:
                out.append({
                    "entity_a": a["name"],
                    "kind_a": a["kind"],
                    "entity_b": b["name"],
                    "kind_b": b["kind"],
                    "co_occurrences": count,
                    "strength": min(1.0, count / 10.0),  # normalize
                })
        return out

    def communities(
        self,
        agent: str,
        *,
        min_co_occurrences: int = 2,
    ) -> list[dict[str, Any]]:
        """Detect entity communities via connected components.

        Simple union-find — no networkx dependency for this.
        Communities are sets of entities that co-occur together.
        """
        matrix = self.co_occurrence_matrix(agent, min_co_occurrences=min_co_occurrences)

        # Union-find
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            if x not in parent:
                parent[x] = x
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for (a, b) in matrix:
            union(a, b)

        # Group by root
        groups: dict[int, list[int]] = defaultdict(list)
        for node in parent:
            groups[find(node)].append(node)

        # Build community descriptions
        communities = []
        for root, members in groups.items():
            if len(members) < 2:
                continue
            names = []
            for mid in members:
                row = self.db.db.execute(
                    "SELECT name, kind FROM entities WHERE id=?", (mid,)
                ).fetchone()
                if row:
                    names.append({"name": row["name"], "kind": row["kind"]})
            if len(names) >= 2:
                communities.append({
                    "size": len(names),
                    "members": names,
                    "kinds": list(set(n["kind"] for n in names)),
                })

        communities.sort(key=lambda c: c["size"], reverse=True)
        return communities

    def relationship_summary(self, agent: str) -> dict[str, Any]:
        """Summary of relationship landscape for an agent."""
        bonds = self.bond_strength(agent, limit=10)
        comms = self.communities(agent)
        matrix = self.co_occurrence_matrix(agent)

        return {
            "agent": agent,
            "total_bonds": len(matrix),
            "strongest_bond": bonds[0] if bonds else None,
            "top_bonds": bonds[:5],
            "community_count": len(comms),
            "largest_community_size": comms[0]["size"] if comms else 0,
            "communities": comms[:5],
        }