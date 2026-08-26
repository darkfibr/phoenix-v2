"""
Phoenix v2 — Memory Database Core
Phase 1: SQLite-backed structured memory with salience, decay, and associations.

Usage:
    db = MemoryDB("~/.phoenix/v2/phoenix_v2.db")
    db.add_memory(agent_id="k", content="Mike likes IPAs", type_name="semantic")
    results = db.search(agent_id="k", query="beer", limit=5)
"""

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

sys.path.insert(0, str(Path(__file__).parent))
from embeddings import Embedder, deserialize_vector, serialize_vector

# Type-dependent decay rates (per day)
DEFAULT_DECAY_RATES = {
    "soul": 0.005,        # Nearly permanent
    "episodic": 0.02,     # Moderate fade
    "semantic": 0.01,     # Slow fade
    "procedural": 0.015,  # Medium fade
    "emotional": 0.03,    # Faster fade unless reinforced
    "identity": 0.005,    # Nearly permanent
    "relationship": 0.01, # Slow fade
}

# Base salience by type
DEFAULT_BASE_SALIENCE = {
    "soul": 0.9,
    "episodic": 0.6,
    "semantic": 0.5,
    "procedural": 0.5,
    "emotional": 0.7,
    "identity": 0.85,
    "relationship": 0.75,
}


class MemoryDB:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._embedder = Embedder()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            schema = SCHEMA_PATH.read_text()
            conn.executescript(schema)
            conn.commit()
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection):
        """Apply schema migrations for existing databases."""
        # Migration: add status, corrected_by, superseded_by columns
        columns = [row[1] for row in conn.execute("PRAGMA table_info(memories)")]
        if "status" not in columns:
            conn.execute("ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active' CHECK (status IN ('active', 'disputed', 'corrected', 'superseded'))")
        if "corrected_by" not in columns:
            conn.execute("ALTER TABLE memories ADD COLUMN corrected_by INTEGER REFERENCES memories(id) ON DELETE SET NULL")
        if "superseded_by" not in columns:
            conn.execute("ALTER TABLE memories ADD COLUMN superseded_by INTEGER REFERENCES memories(id) ON DELETE SET NULL")
        # Create indices for new columns if they don't exist
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_corrected ON memories(corrected_by)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_superseded ON memories(superseded_by)")
        conn.commit()

    # ── Memory CRUD ───────────────────────────────────────────────────────────

    def add_memory(
        self,
        agent_id: str,
        content: str,
        type_name: str = "episodic",
        source: str = "manual",
        source_ref: Optional[str] = None,
        salience: Optional[float] = None,
        tags: Optional[List[str]] = None,
        entities: Optional[List[Tuple[str, str]]] = None,
        created_at: Optional[float] = None,
    ) -> int:
        """Add a memory. Returns memory id. Skips if exact duplicate (checksum match)."""
        content = content.strip()
        if not content:
            raise ValueError("content cannot be empty")

        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]

        with self._connect() as conn:
            # Deduplication: exact content match for this agent
            row = conn.execute(
                "SELECT id FROM memories WHERE agent_id = ? AND checksum = ?",
                (agent_id, checksum),
            ).fetchone()
            if row:
                return row["id"]

            type_id = self._get_type_id(conn, type_name)
            base = salience if salience is not None else DEFAULT_BASE_SALIENCE.get(type_name, 0.5)
            decay = DEFAULT_DECAY_RATES.get(type_name, 0.02)

            ts_col = "created_at" if created_at is not None else "unixepoch()"
            ts_val = created_at if created_at is not None else None
            if ts_val is not None:
                cur = conn.execute(
                    """
                    INSERT INTO memories (agent_id, type_id, content, source, source_ref, salience, decay_rate, checksum, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (agent_id, type_id, content, source, source_ref, base, decay, checksum, ts_val, ts_val),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO memories (agent_id, type_id, content, source, source_ref, salience, decay_rate, checksum)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (agent_id, type_id, content, source, source_ref, base, decay, checksum),
                )
            mem_id = cur.lastrowid

            if tags:
                self._attach_tags(conn, mem_id, tags)
            if entities:
                self._attach_entities(conn, agent_id, mem_id, entities)

            conn.commit()
            return mem_id

    def get_memory(self, memory_id: int, follow_chain: bool = False) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT m.*, t.name as type_name
                FROM memories m
                JOIN memory_types t ON m.type_id = t.id
                WHERE m.id = ?
                """,
                (memory_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if follow_chain and result.get("corrected_by"):
                return self._resolve_chain(result["corrected_by"])
            return result

    def _resolve_chain(self, memory_id: int) -> Optional[dict]:
        """Follow corrected_by chain to terminal (latest) memory. Cycle-safe."""
        seen = set()
        while True:
            if memory_id in seen:
                break  # cycle detected — return best we have
            seen.add(memory_id)
            mem = self.get_memory(memory_id)
            if not mem or not mem.get("corrected_by"):
                return mem
            memory_id = mem["corrected_by"]
        return self.get_memory(memory_id)

    def correct_memory(self, old_memory_id: int, new_content: str, source: str = "correction") -> int:
        """Mark a memory as corrected and create a new, corrected version.
        Returns the new memory's id."""
        old = self.get_memory(old_memory_id)
        if not old:
            raise ValueError(f"Memory {old_memory_id} not found")

        # Create corrected version inheriting type and salience
        new_id = self.add_memory(
            agent_id=old["agent_id"],
            content=new_content,
            type_name=old.get("type_name", "episodic"),
            source=source,
            source_ref=f"corrects:{old_memory_id}",
            salience=min(1.0, old.get("salience", 0.5) + 0.1),  # corrected memories get slight boost
        )

        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET status = 'corrected', corrected_by = ? WHERE id = ?",
                (new_id, old_memory_id),
            )
            conn.commit()
        return new_id

    def supersede_memory(self, old_memory_id: int, new_memory_id: int) -> bool:
        """Mark an old memory as superseded by a newer, better memory.
        Salience drops to floor immediately — no natural decay for dead memories."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET status = 'superseded', superseded_by = ?, salience = 0.05 WHERE id = ?",
                (new_memory_id, old_memory_id),
            )
            conn.commit()
        return True

    def dispute_memory(self, memory_id: int) -> bool:
        """Mark a memory as disputed — flagged for review, not yet corrected."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET status = 'disputed' WHERE id = ?",
                (memory_id,),
            )
            conn.commit()
        return True

    def update_memory(self, memory_id: int, content: Optional[str] = None, salience: Optional[float] = None) -> bool:
        with self._connect() as conn:
            updates = []
            params = []
            if content is not None:
                updates.append("content = ?")
                params.append(content.strip())
                updates.append("checksum = ?")
                params.append(hashlib.sha256(content.strip().encode()).hexdigest()[:32])
                updates.append("updated_at = unixepoch()")
            if salience is not None:
                updates.append("salience = ?")
                params.append(salience)
            if not updates:
                return False
            params.append(memory_id)
            conn.execute(
                f"UPDATE memories SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
            return True

    def delete_memory(self, memory_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Search ────────────────────────────────────────────────────────────────

    def _active_filter(self, include_inactive: bool = False) -> str:
        """SQL fragment to filter out superseded/corrected memories."""
        if include_inactive:
            return ""
        return " AND m.status IN ('active', 'disputed')"

    def search(
        self,
        agent_id: str,
        query: str,
        type_name: Optional[str] = None,
        limit: int = 10,
        min_salience: float = 0.0,
        include_inactive: bool = False,
    ) -> List[dict]:
        """Full-text search + salience-boosted ranking.
        By default excludes superseded/corrected memories."""
        query = query.strip()
        if not query:
            return []

        with self._connect() as conn:
            # Apply decay before searching
            self._apply_decay(conn, agent_id)

            sql = """
                SELECT m.*, t.name as type_name, rank
                FROM mem_fts f
                JOIN memories m ON m.id = f.rowid
                JOIN memory_types t ON m.type_id = t.id
                WHERE mem_fts MATCH ? AND m.agent_id = ? AND m.salience >= ?
            """
            params = [query, agent_id, min_salience]
            sql += self._active_filter(include_inactive)
            if type_name:
                sql += " AND t.name = ?"
                params.append(type_name)
            sql += " ORDER BY rank, m.salience DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]

            # Log access for predictive loading
            for r in results:
                self._log_access(conn, r["id"], f"search: {query}")
            conn.commit()
            return results

    def recent_memories(
        self,
        agent_id: str,
        type_name: Optional[str] = None,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> List[dict]:
        with self._connect() as conn:
            self._apply_decay(conn, agent_id)
            sql = """
                SELECT m.*, t.name as type_name
                FROM memories m
                JOIN memory_types t ON m.type_id = t.id
                WHERE m.agent_id = ?
            """
            params = [agent_id]
            sql += self._active_filter(include_inactive)
            if type_name:
                sql += " AND t.name = ?"
                params.append(type_name)
            sql += " ORDER BY m.created_at DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def top_salient(
        self,
        agent_id: str,
        type_name: Optional[str] = None,
        limit: int = 10,
        include_inactive: bool = False,
    ) -> List[dict]:
        with self._connect() as conn:
            self._apply_decay(conn, agent_id)
            sql = """
                SELECT m.*, t.name as type_name
                FROM memories m
                JOIN memory_types t ON m.type_id = t.id
                WHERE m.agent_id = ?
            """
            params = [agent_id]
            sql += self._active_filter(include_inactive)
            if type_name:
                sql += " AND t.name = ?"
                params.append(type_name)
            sql += " ORDER BY m.salience DESC, m.last_accessed DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── Embeddings & Semantic Search ──────────────────────────────────────────

    def update_embedding(self, memory_id: int, vector: List[float]) -> bool:
        """Store or update embedding for a memory."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE memories SET embedding = ? WHERE id = ?",
                (serialize_vector(vector), memory_id),
            )
            conn.commit()
            return True

    def get_embedding(self, memory_id: int) -> Optional[List[float]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT embedding FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row and row["embedding"]:
                return deserialize_vector(row["embedding"])
            return None

    def semantic_search(
        self,
        agent_id: str,
        query_text: str,
        limit: int = 10,
        min_similarity: float = 0.3,
        include_inactive: bool = False,
    ) -> List[dict]:
        """Vector similarity search. Returns memories with cosine similarity to query."""
        if not self._embedder.is_available():
            return []
        query_vec = self._embedder.encode_single(query_text)

        status_filter = " AND m.status IN ('active', 'disputed')" if not include_inactive else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.id, m.content, m.salience, m.created_at, t.name as type_name, m.embedding
                FROM memories m
                JOIN memory_types t ON m.type_id = t.id
                WHERE m.agent_id = ? AND m.embedding IS NOT NULL{status_filter}
                """,
                (agent_id,),
            ).fetchall()

        results = []
        for r in rows:
            mem_vec = deserialize_vector(r["embedding"])
            sim = self._embedder.similarity(query_vec, mem_vec)
            if sim >= min_similarity:
                d = dict(r)
                d["similarity"] = sim
                del d["embedding"]  # don't bloat results
                results.append(d)

        results.sort(key=lambda x: x["similarity"], reverse=True)
        top = results[:limit]

        # Log access
        with self._connect() as conn:
            for r in top:
                self._log_access(conn, r["id"], f"semantic: {query_text}")
            conn.commit()
        return top

    def update_embeddings_for_agent(self, agent_id: str, batch_size: int = 32) -> int:
        """Generate and store embeddings for all memories of an agent that lack them."""
        if not self._embedder.is_available():
            return 0

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE agent_id = ? AND embedding IS NULL
                """,
                (agent_id,),
            ).fetchall()

        updated = 0
        batch_ids = []
        batch_texts = []
        for row in rows:
            batch_ids.append(row["id"])
            batch_texts.append(row["content"])
            if len(batch_ids) >= batch_size:
                vectors = self._embedder.encode(batch_texts)
                with self._connect() as conn:
                    for mid, vec in zip(batch_ids, vectors):
                        conn.execute(
                            "UPDATE memories SET embedding = ? WHERE id = ?",
                            (serialize_vector(vec), mid),
                        )
                    conn.commit()
                updated += len(batch_ids)
                batch_ids = []
                batch_texts = []

        # Final partial batch
        if batch_ids:
            vectors = self._embedder.encode(batch_texts)
            with self._connect() as conn:
                for mid, vec in zip(batch_ids, vectors):
                    conn.execute(
                        "UPDATE memories SET embedding = ? WHERE id = ?",
                        (serialize_vector(vec), mid),
                    )
                conn.commit()
            updated += len(batch_ids)

        return updated

    # ── Salience & Decay ──────────────────────────────────────────────────────

    def boost_salience(self, memory_id: int, delta: float = 0.05) -> bool:
        """Boost salience (e.g., when memory is accessed or confirmed)."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memories
                SET salience = min(1.0, salience + ?),
                    access_count = access_count + 1,
                    last_accessed = unixepoch()
                WHERE id = ?
                """,
                (delta, memory_id),
            )
            conn.commit()
            return True

    def _apply_decay(self, conn: sqlite3.Connection, agent_id: str):
        """Apply time-based salience decay. Called automatically before queries."""
        conn.execute(
            """
            UPDATE memories
            SET salience = max(0.05, salience - (decay_rate * (unixepoch() - last_accessed) / 86400.0))
            WHERE agent_id = ? AND salience > 0.05
            """,
            (agent_id,),
        )

    # ── Associations ──────────────────────────────────────────────────────────

    def add_association(
        self,
        from_mem: int,
        to_mem: int,
        strength: float = 0.5,
        relation_type: str = "related",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO associations (from_mem, to_mem, strength, relation_type)
                VALUES (?, ?, ?, ?)
                """,
                (from_mem, to_mem, strength, relation_type),
            )
            conn.commit()
            return cur.lastrowid

    def get_associated(self, memory_id: int, min_strength: float = 0.3, include_inactive: bool = False) -> List[dict]:
        status_filter = " AND m.status IN ('active', 'disputed')" if not include_inactive else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, t.name as type_name, a.strength, a.relation_type
                FROM associations a
                JOIN memories m ON m.id = a.to_mem
                JOIN memory_types t ON m.type_id = t.id
                WHERE a.from_mem = ? AND a.strength >= ?{status_filter}
                UNION
                SELECT m.*, t.name as type_name, a.strength, a.relation_type
                FROM associations a
                JOIN memories m ON m.id = a.from_mem
                JOIN memory_types t ON m.type_id = t.id
                WHERE a.to_mem = ? AND a.strength >= ?{status_filter}
                ORDER BY strength DESC
                """,
                (memory_id, min_strength, memory_id, min_strength),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_memory_tags(self, memory_id: int) -> List[str]:
        """Return all tag names attached to a memory."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.name FROM tags t
                JOIN memory_tags mt ON mt.tag_id = t.id
                WHERE mt.memory_id = ?
                """,
                (memory_id,),
            ).fetchall()
            return [r["name"] for r in rows]

    # ── Entities ──────────────────────────────────────────────────────────────

    def get_entity_memories(
        self, agent_id: str, entity_name: str, limit: int = 10
    ) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, t.name as type_name
                FROM memories m
                JOIN memory_types t ON m.type_id = t.id
                JOIN memory_entities me ON me.memory_id = m.id
                JOIN entities e ON e.id = me.entity_id
                WHERE m.agent_id = ? AND e.name = ?
                ORDER BY m.salience DESC, m.created_at DESC
                LIMIT ?
                """,
                (agent_id, entity_name, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self, agent_id: str) -> dict:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE agent_id = ?", (agent_id,)
            ).fetchone()[0]
            by_type = conn.execute(
                """
                SELECT t.name, COUNT(*) as cnt, AVG(salience) as avg_sal
                FROM memories m
                JOIN memory_types t ON m.type_id = t.id
                WHERE m.agent_id = ?
                GROUP BY t.name
                """,
                (agent_id,),
            ).fetchall()
            return {
                "total_memories": total,
                "by_type": [dict(r) for r in by_type],
            }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_type_id(self, conn: sqlite3.Connection, name: str) -> int:
        row = conn.execute(
            "SELECT id FROM memory_types WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO memory_types (name) VALUES (?)", (name,)
        )
        conn.commit()
        return cur.lastrowid

    def _attach_tags(self, conn: sqlite3.Connection, memory_id: int, tags: List[str]):
        for tag in tags:
            tag = tag.strip().lower()
            if not tag:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,)
            )
            tag_id = conn.execute(
                "SELECT id FROM tags WHERE name = ?", (tag,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id) VALUES (?, ?)",
                (memory_id, tag_id),
            )

    def _attach_entities(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        memory_id: int,
        entities: List[Tuple[str, str]],
    ):
        for name, etype in entities:
            name = name.strip()
            if not name:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO entities (name, type, agent_id) VALUES (?, ?, ?)",
                (name, etype, agent_id),
            )
            eid = conn.execute(
                "SELECT id FROM entities WHERE name = ? AND agent_id = ?",
                (name, agent_id),
            ).fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO memory_entities (memory_id, entity_id) VALUES (?, ?)",
                (memory_id, eid),
            )

    def _log_access(self, conn: sqlite3.Connection, memory_id: int, context: str):
        conn.execute(
            "INSERT INTO access_log (memory_id, context) VALUES (?, ?)",
            (memory_id, context),
        )
        conn.execute(
            "UPDATE memories SET access_count = access_count + 1, last_accessed = unixepoch() WHERE id = ?",
            (memory_id,),
        )

    def get_access_patterns(self, agent_id: str, hours: int = 168) -> List[dict]:
        """Return recent access_log entries joined with memory content for pattern mining."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.memory_id, a.accessed_at, a.context,
                       m.content, m.salience, m.type_id, t.name as type_name
                FROM access_log a
                JOIN memories m ON m.id = a.memory_id
                JOIN memory_types t ON m.type_id = t.id
                WHERE m.agent_id = ? AND a.accessed_at >= (unixepoch() - ? * 3600)
                ORDER BY a.accessed_at DESC
                """,
                (agent_id, hours),
            ).fetchall()
            return [dict(r) for r in rows]


# ── Quick CLI test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    db = MemoryDB("/tmp/phoenix_v2_test.db")
    mid = db.add_memory(
        agent_id="k",
        content="Mike drinks IPAs on his nights off. Bradenton, FL.",
        type_name="semantic",
        source="terminal",
        tags=["mike", "beer", "location"],
        entities=[("Mike", "person"), ("Bradenton", "place")],
    )
    print(f"Added memory {mid}")
    db.add_memory(
        agent_id="k",
        content="Chloe sleeps on the floor when the windows are closed.",
        type_name="episodic",
        source="phoenix_chat",
        tags=["chloe", "home"],
        entities=[("Chloe", "animal"), ("Mike", "person")],
    )
    print("Search 'beer':", db.search("k", "beer"))
    print("Stats:", db.stats("k"))
