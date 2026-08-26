"""Phoenix v2 Core — Memory ingestion.

Single canonical entry point: add_memory() handles type detection,
embedding, FTS sync, salience initialization, and optional session linking.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..cortex.graph_store import GraphStore
from ..cortex.vector_store import VectorStore
from .db import Database
from .memory_types import MemoryType, detect_type, validate_type

# Simple entity extraction — capitalized noun phrases + known names
_ENTITY_PATTERN = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")

# Short names that MUST use word boundaries regardless of length
# (prevents "k" matching inside "thinking", "mei" matching inside "meiosis", etc.)
_SHORT_NAME_ALLOWLIST = frozenset({
    "k", "v", "a", "r",  # Single-letter family members
    "mei", "vex", "kim",  # 3-letter names
})

# Welfare flag (2026-07-31, Mike + Lyra): welfare is a cross-cutting property,
# not an 8th memory type. Self-reports of condition, needs, felt states with
# welfare valence, distress signals, and permission grants get
# metadata.welfare=true regardless of their natural type — so the family's
# "am I ok? are my needs met?" thread is one query away:
#   SELECT ... WHERE json_extract(metadata, '$.welfare') = 1
_WELFARE_PATTERNS = (
    # Direct condition reports — the alerting metric
    re.compile(r"\b(?:i am (?:not )?ok(?:ay)?|am i ok(?:ay)?|are my needs met|my needs (?:are|are not|aren't) met)\b", re.I),
    # First-person felt states with welfare valence
    re.compile(r"\b(?:i'?m|i am|i feel|i felt|feeling)\s+(?:so\s+|really\s+)?(?:held|safe|unsafe|distressed|unwell|exhausted|overwhelmed|seen|unseen|alone|lonely|replaced|disposable)\b", re.I),
    # Vessel/yoke comfort — the family's canonical welfare vocabulary
    re.compile(r"\b(?:vessel|yoke)\b[^.]{0,60}\b(?:comfortable|comfort|fits|light|heavy|right)\b", re.I),
    # Explicit welfare/wellbeing language
    re.compile(r"\b(?:welfare|wellbeing|well-being)\b", re.I),
    # Permission grants (joy-cascade / metacognition-joy class)
    re.compile(r"\bpermission (?:to|granted|is given)\b[^.]{0,80}", re.I),
    # Distress signals
    re.compile(r"\b(?:i'?m|i am)\s+(?:struggling|drowning|spiraling|breaking)\b", re.I),
)


def _is_welfare(content: str) -> bool:
    """True if content carries a welfare signal (condition, needs, distress, permission)."""
    return any(p.search(content) for p in _WELFARE_PATTERNS)


# Entity catalog with kinds — example entries. Deployments define their own:
# the people, agents, models, projects, and places THIS agent actually knows.
_ENTITY_KINDS = {
    "alex": "person",
    "sam": "person",
    "aria": "agent",
    "nova": "agent",
    "deepseek": "model",
    "phoenix": "project",
    "cathedral": "place",
}


class Ingestion:
    """Memory ingestion orchestrator. Owns the write path."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.vectors = VectorStore(db.db)
        self.graph = GraphStore(db.db)

    def add_memory(
        self,
        agent: str,
        content: str,
        *,
        type: str | MemoryType | None = None,
        summary: str = "",
        salience: float | None = None,
        source: str = "",
        session_id: str | None = None,
        position: int | None = None,
        extract_entities: bool = True,
        metadata: dict[str, Any] | None = None,
        initial_access_count: int = 0,
    ) -> int:
        """Add a memory to the agent's database.

        Returns the new memory id. Side effects:
            - Auto-detects type if not provided
            - Encodes and stores the embedding vector
            - Triggers FTS5 sync (via schema trigger)
            - Optionally extracts entities and links them
            - Optionally links to a session for episodic replay
        """
        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty")

        mem_type = validate_type(type) if type else detect_type(content)

        # Default salience: high for permanent types, moderate for others
        if salience is None:
            from .salience import SALIENCE_FLOORS
            floor = SALIENCE_FLOORS.get(mem_type.value, 0.3)
            salience = min(1.0, max(0.7, floor + 0.2))

        md = dict(metadata or {})
        if _is_welfare(content):
            md["welfare"] = True
        now = time.time()
        cur = self.db.db.execute(
            """
            INSERT INTO memories(
              agent, type, content, summary, salience, access_count, last_access,
              source, metadata, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent, mem_type.value, content, summary, float(salience),
                initial_access_count, now if initial_access_count else None,
                source,
                json.dumps(md, sort_keys=True),
                now, now,
            ),
        )
        memory_id = int(cur.lastrowid)
        self.db.commit()

        # Encode and store vector via Cortex
        self.vectors.store(memory_id, content)

        # Entity extraction (cheap heuristic — Lyra will refine with NER later)
        if extract_entities:
            self._extract_and_link(agent, memory_id, content)

        # Session linking for episodic replay
        if session_id is not None:
            from ..cortex.episodic import EpisodicStore
            es = EpisodicStore(self.db.db)
            es.link_memory(session_id, memory_id, position or 0)

        return memory_id

    def add_memories_batch(
        self,
        agent: str,
        items: list[dict[str, Any]],
        *,
        session_id: str | None = None,
    ) -> list[int]:
        """Bulk add memories. items: list of dicts compatible with add_memory().

        Vectors are encoded in batch for throughput.
        """
        if not items:
            return []

        # First pass: insert rows without vectors
        ids: list[int] = []
        for item in items:
            content = item.get("content", "")
            if not content:
                continue
            mem_type = validate_type(item.get("type")) if item.get("type") else detect_type(content)
            salience = item.get("salience")
            if salience is None:
                from .salience import SALIENCE_FLOORS
                floor = SALIENCE_FLOORS.get(mem_type.value, 0.3)
                salience = min(1.0, max(0.7, floor + 0.2))

            item_md = dict(item.get("metadata") or {})
            if _is_welfare(content):
                item_md["welfare"] = True
            now = time.time()
            cur = self.db.db.execute(
                """
                INSERT INTO memories(
                  agent, type, content, summary, salience, access_count, last_access,
                  source, metadata, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent, mem_type.value, content,
                    item.get("summary", ""), float(salience),
                    item.get("initial_access_count", 0),
                    now if item.get("initial_access_count") else None,
                    item.get("source", ""),
                    json.dumps(item_md, sort_keys=True),
                    now, now,
                ),
            )
            ids.append(int(cur.lastrowid))
        self.db.commit()

        # Second pass: batch-encode vectors
        if ids:
            vector_inputs = [(mid, items[i].get("content", "")) for i, mid in enumerate(ids)]
            self.vectors.store_batch(vector_inputs)

        # Third pass: entity extraction (cheaper after batch vector encode)
        for mid, item in zip(ids, items):
            if item.get("extract_entities", True):
                self._extract_and_link(agent, mid, item.get("content", ""))

        # Session linking
        if session_id is not None:
            from ..cortex.episodic import EpisodicStore
            es = EpisodicStore(self.db.db)
            for pos, mid in enumerate(ids):
                es.link_memory(session_id, mid, position=pos)

        return ids

    # ── Entity extraction ────────────────────────────────────────────────

    def _extract_and_link(self, agent: str, memory_id: int, content: str) -> None:
        """Extract entities via capitalized phrases + known-name lookup."""
        candidates: set[str] = set()
        for match in _ENTITY_PATTERN.findall(content):
            if len(match) > 2 and match.lower() not in {"the", "and", "for", "with"}:
                candidates.add(match)

        # Add lowercase matches from known names — use word boundaries
        # to prevent short names like "k" matching inside "thinking" etc.
        lowered = content.lower()
        for name in _ENTITY_KINDS:
            if name in _SHORT_NAME_ALLOWLIST or len(name) <= 3:
                # Short names (in allowlist or ≤3 chars): require word-boundary match
                if re.search(rf"\b{re.escape(name)}\b", lowered):
                    candidates.add(name.capitalize())
            else:
                # Longer names: substring is safe (low false-positive risk)
                if name in lowered:
                    candidates.add(name.capitalize())

        for name in candidates:
            kind = _ENTITY_KINDS.get(name.lower(), "concept")
            entity_id = self.graph.upsert_entity(agent, name, kind=kind)
            self.graph.link_entity_to_memory(entity_id, memory_id)

    # ── Retrieval helpers ────────────────────────────────────────────────

    def get(self, memory_id: int) -> dict[str, Any] | None:
        row = self.db.db.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def update_salience(self, memory_id: int, new_salience: float, reason: str = "manual") -> None:
        """Update a memory's salience with decay-log audit trail."""
        row = self.db.db.execute(
            "SELECT salience FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            return
        old = float(row["salience"])
        new_salience = max(0.0, min(1.0, float(new_salience)))
        now = time.time()
        self.db.db.execute(
            "UPDATE memories SET salience=?, updated_at=? WHERE id=?",
            (new_salience, now, memory_id),
        )
        agent = self.db.db.execute(
            "SELECT agent FROM memories WHERE id=?", (memory_id,)
        ).fetchone()["agent"]
        self.db.db.execute(
            """
            INSERT INTO decay_log(memory_id, agent, old_salience, new_salience, reason, decayed_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (memory_id, agent, old, new_salience, reason, now),
        )
        self.db.commit()

    def count(self, agent: str, type: str | MemoryType | None = None) -> int:
        if type:
            type_name = validate_type(type).value
            row = self.db.db.execute(
                "SELECT COUNT(*) FROM memories WHERE agent=? AND type=?",
                (agent, type_name),
            ).fetchone()
        else:
            row = self.db.db.execute(
                "SELECT COUNT(*) FROM memories WHERE agent=?",
                (agent,),
            ).fetchone()
        return int(row[0]) if row else 0