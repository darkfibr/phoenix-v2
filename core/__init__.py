"""Phoenix v2 Core layer.

Owns:
    - schema.sql        — 7 memory types, associations, embeddings, FTS5
    - db.py             — connection pool, WAL mode, migrations
    - embeddings.py     — sentence-transformers wrapper, batch encoding
    - salience.py       — type-dependent decay rates, reinforcement
    - memory_types.py   — 7 types with decay constants (Lyra)
    - ingestion.py      — add_memory() with type detection (Lyra)
    - decay.py          — automatic salience adjustment (Lyra)
    - surface.py        — budget engine (Lyra)
    - surprise.py       — cross-type association (Lyra)
    - migration.py      — v1 -> v2 migration (Lyra)
"""