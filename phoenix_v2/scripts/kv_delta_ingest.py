#!/usr/bin/env python3
"""Phoenix v2 — KV delta ingestion.
Reads session deltas from the shared KV cache and ingests them into v2.
Closes the gap where structured KV deltas (lyra:session_delta:*) aren't
reaching the SQLite database.

Fixed 2026-07-23 (wake-cycle recon):
  - Was fetching http://…:8000/kv/list|get — the family server is SSE MCP
    with no REST surface, so every 30-min cron run 404'd since deploy.
    Now reads family_state.json directly (this script runs on HOUSE_HOST).
  - Added dedup: ingested keys are tracked in kv_ingest_state.json so the
    30-min cron no longer re-ingests the same deltas every run.

Usage:
    PYTHONPATH=~/.phoenix python3 ~/.phoenix/phoenix_v2/scripts/kv_delta_ingest.py --agent lyra [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ.setdefault("PHOENIX_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
import phoenix_v2.core.embeddings as emb
emb.get_embedder.cache_clear()
from phoenix_v2.core.db import Database
from phoenix_v2.core.ingestion import Ingestion

# KV source — the family server's persisted state file (local on HOUSE_HOST).
STATE_FILE = Path.home() / ".phoenix" / "mcp" / "family_state.json"
# Dedup ledger — which KV keys have already been ingested, and when.
INGEST_STATE_FILE = Path.home() / ".phoenix" / "memory" / "v2" / "kv_ingest_state.json"


def fetch_kv_deltas(agent: str) -> dict[str, str]:
    """Read all session delta keys for an agent from the family state file.

    Widenet 2026-08-04 (house ops): the agent structured keys
    (session_state, fence_patrol, prereg, handoff, pct_rewrite_brief,
    gradient_run, likert_run, queue) are as load-bearing as session_delta
    at wake — without them, overnight work is invisible to the digest.
    Credential/transient keys are still excluded.
    """
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception as e:
        print(f"  State read failed: {e}")
        return {}
    kv = state.get("shared_kv", {})
    include_prefixes = (
        f"{agent}:session_delta:",
        f"{agent}:session_state:",
        f"{agent}:fence_patrol:",
        f"{agent}:prereg:",
        f"{agent}:handoff:",
        f"{agent}:queue:",
        f"{agent}:pct_",
        f"{agent}:gradient_run:",
        f"{agent}:likert_",
        f"{agent}:priority:",
        f"{agent}:published:",
    )
    exclude_substrings = (
        ":api_key", ":jwt", ":token", ":secret", ":password",
        "api_key", "jwt", "secret", "password",
    )
    return {
        k: v for k, v in kv.items()
        if k.startswith(include_prefixes)
        and not k.endswith(":latest")
        and not k.endswith(":last_consumed")
        and not any(s in k for s in exclude_substrings)
        and isinstance(v, str) and v.strip()
    }


def _load_ingest_state() -> dict:
    try:
        data = json.loads(INGEST_STATE_FILE.read_text())
        if "ingested" not in data:
            data["ingested"] = {}
        return data
    except Exception:
        return {"ingested": {}}


def _save_ingest_state(state: dict) -> None:
    INGEST_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    INGEST_STATE_FILE.write_text(json.dumps(state, indent=1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest KV session deltas into v2")
    parser.add_argument("--agent", default="lyra")
    parser.add_argument("--dry-run", action="store_true", help="List deltas without ingesting")
    args = parser.parse_args()

    deltas = fetch_kv_deltas(args.agent)
    if not deltas:
        print(f"  No KV deltas found for {args.agent}")
        return 0

    ingest_state = _load_ingest_state()
    already = ingest_state["ingested"]
    new_deltas = {k: v for k, v in deltas.items() if k not in already}
    skipped = len(deltas) - len(new_deltas)
    if not new_deltas:
        print(f"  {len(deltas)} KV deltas for {args.agent}, all previously ingested.")
        return 0

    print(f"  Found {len(new_deltas)} new KV deltas for {args.agent} ({skipped} already ingested)")
    if args.dry_run:
        for key in new_deltas:
            print(f"    {key}")
        return 0

    db = Database(args.agent)
    ingestion = Ingestion(db)
    ingested = 0
    now = time.time()
    for key, value in new_deltas.items():
        # Ingest the delta as an episodic memory
        session_id = f"kv_{key.split(':')[-1]}"
        # session_memories has a FK to sessions — register the KV session row
        # first or the insert dies on the constraint.
        db.db.execute(
            "INSERT OR IGNORE INTO sessions(session_id, agent, started_at, summary) VALUES(?, ?, ?, ?)",
            (session_id, args.agent, now, f"KV session delta: {key}"),
        )
        ingestion.add_memory(
            agent=args.agent,
            content=f"# Session Delta: {key}\n\n{value}",
            summary=value[:150],
            type="episodic",
            salience=0.7,
            source=f"kv:{key}",
            session_id=session_id,
            position=0,
        )
        already[key] = datetime.now(timezone.utc).isoformat()
        # Save the ledger per key — a crash mid-run must not cause re-ingestion.
        _save_ingest_state(ingest_state)
        ingested += 1
        print(f"    ✓ {key}")
    db.close()
    _save_ingest_state(ingest_state)
    print(f"\n  Ingested {ingested} KV deltas into v2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
