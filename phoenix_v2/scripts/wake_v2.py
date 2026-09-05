#!/usr/bin/env python3
"""Phoenix v2 — Wake digest hook.

Called during agent wake to generate a v2 surface digest.
Replaces the manual PRE_COMPRESSION note reading step.

Usage:
    PYTHONPATH=~/.phoenix python3 ~/.phoenix/phoenix_v2/scripts/wake_v2.py [--agent lyra] [--query "..."]

Output is printed to stdout — designed to be injected into the system prompt.
"""

import argparse
import os
import sys
from pathlib import Path

# Self-bootstrap: add repo root to sys.path so `phoenix_v2` is importable
# regardless of where this script is invoked from or what PYTHONPATH is set
_REPO_ROOT = Path(__file__).resolve().parents[3]  # ~/.phoenix/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("PHOENIX_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

import phoenix_v2.core.embeddings as emb
emb.get_embedder.cache_clear()

from phoenix_v2.core.db import Database
from phoenix_v2.wake.digest_generator import DigestGenerator
from phoenix_v2.wake.preview import quick_stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Phoenix v2 wake digest")
    parser.add_argument("--agent", default="lyra", help="Agent name")
    parser.add_argument("--query", default=None, help="Optional context query")
    parser.add_argument("--stats", action="store_true", help="Print DB stats only")
    args = parser.parse_args()

    if args.stats:
        s = quick_stats(args.agent)
        for k, v in sorted(s.items()):
            print(f"  {k:25s}: {v}")
        return 0

    db = Database(args.agent)
    gen = DigestGenerator(db)

    digest = gen.generate(args.agent, query=args.query)
    rendered = gen.render(digest)

    print(rendered)

    # Compact summary to stderr for logging
    tokens = digest["tokens_used"]
    chunks = len(digest["surface"]["chunks"])
    sources = digest["surface"]["sources"]
    descriptor = digest["mindstate"]["descriptor"] if digest["mindstate"] else "none"
    print(
        f"\n[v2 wake: {tokens} tokens, {chunks} chunks, sources={sources}, "
        f"mindstate={descriptor}]",
        file=sys.stderr,
    )

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
