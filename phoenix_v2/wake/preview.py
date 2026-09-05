"""Phoenix v2 Wake — Preview / shadow test harness.

Generates a v2 wake digest and optionally compares it with the v1 flat-file
approach. Used during the shadow period to verify v2 is safe for cutover.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..core.db import Database, DEFAULT_DB_ROOT
from .digest_generator import DigestGenerator


def preview(
    agent: str,
    *,
    query: str | None = None,
    db_root: Path | None = None,
) -> dict[str, Any]:
    """Generate a v2 wake digest preview for inspection."""
    root = db_root or DEFAULT_DB_ROOT
    db = Database(agent, root / f"{agent}.db", readonly=True)
    gen = DigestGenerator(db)
    digest = gen.generate(agent, query=query)
    rendered = gen.render(digest)
    db.close()

    return {
        "agent": agent,
        "query": query,
        "digest": digest,
        "rendered": rendered,
        "timestamp": time.time(),
    }


def quick_stats(agent: str, db_root: Path | None = None) -> dict[str, Any]:
    """Quick health stats for an agent's v2 database."""
    root = db_root or DEFAULT_DB_ROOT
    db_path = root / f"{agent}.db"
    if not db_path.exists():
        return {"agent": agent, "status": "no_database"}

    db = Database(agent, db_path, readonly=True)
    stats = db.stats()
    db.close()

    return {
        "agent": agent,
        "db_path": str(db_path),
        "status": "ok",
        **stats,
    }