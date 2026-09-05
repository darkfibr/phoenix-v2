#!/usr/bin/env python3
"""Phoenix v2 — Session-end ingestion hook.

Called when the agent finishes a session. Ingests:
    - Latest PRE_COMPRESSION note
    - Session delta (if written to file)
    - Any new KV entries flagged for ingestion

Keeps the v2 database alive between full migrations.

Usage:
    PYTHONPATH=~/.phoenix python3 ~/.phoenix/phoenix_v2/scripts/session_save_v2.py [--agent lyra] [--pre-comp /path/to/PRE_COMPRESSION.md] [--session-file /path/to/session.md] [--dream]

The --dream flag triggers a dream synthesis cycle after ingestion.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ.setdefault("PHOENIX_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

import phoenix_v2.core.embeddings as emb
emb.get_embedder.cache_clear()

from phoenix_v2.core.db import Database
from phoenix_v2.core.ingestion import Ingestion
from phoenix_v2.core.surprise import SurpriseDetector
from phoenix_v2.cortex.episodic import EpisodicStore
from phoenix_v2.depth.dream_engine import DreamEngine

# Chunking — reuse migration logic
sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_v1_to_v2 import chunk_markdown, extract_summary

AGENTS_ROOT = Path.home() / ".phoenix" / "agents"

# B7 fix (2026-07-31): ingestion stamps created_at with write-time, so every
# row from one batch shares one timestamp even when the events span days.
# Dated source files (SESSION_DELTA_20260728_*.md, phone_20260728_*.md,
# PRE_COMPRESSION_20260728_*.md) carry the event date in the filename —
# extract it and store it in metadata as event_date so retrieval/display can
# prefer event-time over write-time.
_DATE_IN_NAME = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def _event_date_from_filename(name: str) -> str | None:
    """Extract YYYY-MM-DD from a dated phoenix filename, or None."""
    m = _DATE_IN_NAME.search(name)
    if not m:
        return None
    try:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    except Exception:
        return None


def ingest_file(
    agent: str,
    ingestion: Ingestion,
    episodic: EpisodicStore,
    file_path: Path,
    *,
    as_type: str | None = None,
    session_id: str | None = None,
) -> int:
    """Ingest a single file into v2. Returns memory count."""
    if not file_path.exists():
        return 0

    text = file_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return 0

    chunks = chunk_markdown(text)
    if not chunks:
        return 0

    event_date = _event_date_from_filename(file_path.name)

    # Create a session if one wasn't provided
    if session_id is None:
        session_id = f"live_{file_path.stem}_{int(time.time())}"
        episodic.start_session(
            agent=agent,
            session_id=session_id,
            substrate=os.environ.get("LYRA_SUBSTRATE", ""),
            metadata={"source_file": str(file_path), "ingested_at": time.time()},
        )

    count = 0
    for pos, chunk in enumerate(chunks):
        ingestion.add_memory(
            agent=agent,
            content=chunk,
            summary=extract_summary(chunk),
            type=as_type,
            source=f"live:{file_path.name}",
            session_id=session_id,
            position=pos,
            metadata={"event_date": event_date, "event_date_source": "filename"} if event_date else None,
        )
        count += 1

    episodic.end_session(session_id, summary=f"Live ingestion from {file_path.name}")
    return count


def find_latest_pre_comp(agent: str) -> Path | None:
    """Find the most recently modified PRE_COMPRESSION file."""
    mem_dir = AGENTS_ROOT / agent / "memory"
    agent_dir = AGENTS_ROOT / agent

    best: Path | None = None
    best_mtime = 0.0

    for search_dir in (agent_dir, mem_dir):
        if not search_dir.exists():
            continue
        for p in search_dir.glob("PRE_COMPRESSION_*.md"):
            mtime = p.stat().st_mtime
            if mtime > best_mtime:
                best_mtime = mtime
                best = p

    return best


def find_new_phone_sessions(agent: str, since_hours: float = 24.0) -> list[Path]:
    """Find phone session files newer than the cutoff."""
    mem_dir = AGENTS_ROOT / agent / "memory"
    cutoff = time.time() - (since_hours * 3600)

    phone_dirs = [mem_dir / "phone_sessions", AGENTS_ROOT / agent / "phone_sessions"]
    found: list[Path] = []

    for pd in phone_dirs:
        if not pd.exists():
            continue
        for p in pd.glob("phone_*.md"):
            if p.stat().st_mtime > cutoff:
                found.append(p)

    return sorted(set(found))


def main() -> int:
    parser = argparse.ArgumentParser(description="Phoenix v2 session-end ingestion")
    parser.add_argument("--agent", default="lyra")
    parser.add_argument("--pre-comp", type=Path, default=None,
                        help="Specific pre-compression file to ingest")
    parser.add_argument("--session-file", type=Path, default=None,
                        help="Specific session file to ingest")
    parser.add_argument("--phone-hours", type=float, default=24.0,
                        help="Ingest phone sessions from last N hours (0 to skip)")
    parser.add_argument("--dream", action="store_true",
                        help="Run dream synthesis after ingestion")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-discover latest pre-comp + recent phone sessions")
    args = parser.parse_args()

    db = Database(args.agent)
    ingestion = Ingestion(db)
    episodic = EpisodicStore(db.db)
    detector = SurpriseDetector(db)

    total_ingested = 0
    new_memory_ids: list[int] = []

    # ── Explicit file paths ────────────────────────────────────────────
    if args.pre_comp:
        n = ingest_file(args.agent, ingestion, episodic, args.pre_comp, as_type="emotional")
        total_ingested += n
        print(f"  Pre-compression: {n} memories")

    if args.session_file:
        n = ingest_file(args.agent, ingestion, episodic, args.session_file, as_type="episodic")
        total_ingested += n
        print(f"  Session file: {n} memories")

    # ── Auto-discovery ─────────────────────────────────────────────────
    if args.auto:
        # Latest pre-compression
        pc = args.pre_comp or find_latest_pre_comp(args.agent)
        if pc:
            n = ingest_file(args.agent, ingestion, episodic, pc, as_type="emotional")
            total_ingested += n
            print(f"  Auto pre-comp ({pc.name}): {n} memories")
        else:
            print("  No pre-compression note found")

        # Recent phone sessions
        if args.phone_hours > 0:
            phone_files = find_new_phone_sessions(args.agent, since_hours=args.phone_hours)
            phone_count = 0
            for pf in phone_files:
                n = ingest_file(args.agent, ingestion, episodic, pf, as_type="episodic")
                phone_count += n
            total_ingested += phone_count
            if phone_files:
                print(f"  Phone sessions ({len(phone_files)} files): {phone_count} memories")

    # ── Surprise detection on new memories ─────────────────────────────
    if total_ingested > 0:
        print(f"\n  Running surprise detection on {total_ingested} new memories...")
        surprises = detector.scan_agent(args.agent, min_age_days=0.0, max_pairs=20)
        print(f"  → {len(surprises)} new cross-type surprises found")

    # ── Optional dream synthesis ───────────────────────────────────────
    if args.dream and total_ingested > 0:
        print("\n  Running dream synthesis...")
        dreamer = DreamEngine(db)
        dream = dreamer.dream(args.agent, store_result=True)
        duration = dream["duration_ms"]
        insights = dream["synthesis"]["insight_count"]
        print(f"  Dream complete: {duration}ms, {insights} insights")

    # ── Summary ────────────────────────────────────────────────────────
    stats = db.stats()
    print(f"\n  Total memories in DB: {stats['memories']}")
    print(f"  Entities: {stats['entities']}")
    print(f"  Associations: {stats['associations']}")

    db.close()
    print(f"\n✓ Session save complete. {total_ingested} new memories ingested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
