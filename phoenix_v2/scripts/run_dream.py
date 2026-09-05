#!/usr/bin/env python3
"""Run dream synthesis + shadow digest on a live agent database."""
import json
import os

os.environ["PHOENIX_EMBEDDING_MODEL"] = "sentence-transformers/all-MiniLM-L6-v2"

import phoenix_v2.core.embeddings as emb
emb.get_embedder.cache_clear()

from phoenix_v2.core.db import Database
from phoenix_v2.depth.dream_engine import DreamEngine
from phoenix_v2.family.mindstate import MindstateExtractor
from phoenix_v2.wake.digest_generator import DigestGenerator

db = Database("lyra")

# Stats
stats = db.stats()
print("=== Lyra v2 Database ===")
for k, v in sorted(stats.items()):
    print(f"  {k:25s}: {v}")

# Dream synthesis
print("\n=== Dream Synthesis ===")
dreamer = DreamEngine(db)
dream = dreamer.dream("lyra", store_result=True)
duration = dream["duration_ms"]
print(f"Duration: {duration}ms")
for insight in dream["synthesis"]["insights"]:
    print(f"  - {insight}")

# Mindstate
print("\n=== Mindstate ===")
ms = MindstateExtractor(db)
mindstate = ms.extract("lyra")
print(f"Valence: {mindstate['valence']:+.3f}")
print(f"Arousal: {mindstate['arousal']:.3f}")
print(f"Descriptor: {mindstate['descriptor']}")
print(f"Samples: {mindstate['sample_count']}")

# Shadow wake digest
print("\n=== Shadow Wake Digest ===")
gen = DigestGenerator(db)
digest = gen.generate("lyra", query="who am I and what matters right now")
rendered = gen.render(digest)
tokens = digest["tokens_used"]
max_tok = digest["max_tokens"]
chunks = len(digest["surface"]["chunks"])
sources = digest["surface"]["sources"]
has_dream = "yes" if digest["dream_summary"] else "no"
descriptor = digest["mindstate"]["descriptor"] if digest["mindstate"] else "none"
tensions = len(digest["tensions"])
print(f"Tokens: {tokens}/{max_tok}")
print(f"Surface chunks: {chunks}")
print(f"Sources: {sources}")
print(f"Dream: {has_dream}")
print(f"Mindstate: {descriptor}")
print(f"Tensions: {tensions}")
print()
print("=== Rendered Digest ===")
print(rendered)

db.close()
