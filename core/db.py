"""Phoenix v2 Core — Database connection management.

WAL mode, connection pool, migrations, integrity checks.
Per-agent databases at ~/.phoenix/memory/v2/<agent>.db
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Schema lives next to this module
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Default DB root
DEFAULT_DB_ROOT = Path.home() / ".phoenix" / "memory" / "v2"


def load_schema() -> str:
    """Read schema.sql from disk."""
    return SCHEMA_PATH.read_text(encoding="utf-8")


class Database:
    """Single-agent Phoenix v2 database.

    Opens one SQLite file, applies schema, manages connection.
    For multi-agent support, create one Database per agent.
    """

    def __init__(
        self,
        agent: str,
        db_path: Path | None = None,
        *,
        readonly: bool = False,
    ) -> None:
        self.agent = agent
        self.db_path = db_path or (DEFAULT_DB_ROOT / f"{agent}.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{self.db_path}"
        if readonly:
            uri += "?mode=ro"
        self.db = sqlite3.connect(uri, uri=True, timeout=10.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            # Ensure WAL mode (idempotent — no-op if already WAL)
            mode = self.db.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() != "wal":
                self.db.execute("PRAGMA journal_mode=WAL")
            # Only run schema creation on first-ever DB creation.
            # Checking sqlite_master is safe under concurrent access:
            # the first process acquires the write lock for executescript,
            # any racing process blocks on busy_timeout then finds schema exists.
            needs_schema = self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories' LIMIT 1"
            ).fetchone() is None
            if needs_schema:
                self.db.executescript(load_schema())
                self.db.commit()
            self._ensure_agent_row()

    def _ensure_agent_row(self) -> None:
        """Register this agent in the agents table if not present."""
        existing = self.db.execute(
            "SELECT name FROM agents WHERE name=?", (self.agent,)
        ).fetchone()
        if not existing:
            self.db.execute(
                """
                INSERT INTO agents(name, model, substrate, role, created_at)
                VALUES(?, 'unknown', 'unknown', '', ?)
                """,
                (self.agent, time.time()),
            )
            self.db.commit()

    def close(self) -> None:
        """Close connection. WAL self-manages via auto-checkpoint (default 1000 pages)."""
        try:
            self.db.close()
        except sqlite3.DatabaseError:
            pass

    def commit(self) -> None:
        self.db.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write transaction with rollback on error."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def integrity_check(self) -> bool:
        row = self.db.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")

    def stats(self) -> dict[str, Any]:
        """Return table counts + WAL status for health checks."""
        counts = {}
        for table in (
            "agents", "memory_types", "memories", "memory_vectors",
            "associations", "entities", "entity_mentions", "entity_relations",
            "sessions", "session_memories", "decay_log",
        ):
            row = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(row[0]) if row else 0
        wal = self.db.execute("PRAGMA journal_mode").fetchone()
        counts["journal_mode"] = wal[0] if wal else "unknown"
        return counts


def open_agent_db(agent: str, db_root: Path | None = None) -> Database:
    """Convenience factory for the standard per-agent DB location."""
    if db_root is not None:
        return Database(agent, db_root / f"{agent}.db")
    return Database(agent)


def list_agents(db_root: Path | None = None) -> list[str]:
    """Enumerate all per-agent DB files."""
    root = db_root or DEFAULT_DB_ROOT
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.db"))