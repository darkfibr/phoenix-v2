"""Phoenix v2 memory system.

Three-layer architecture:
    Core    — SQLite + embeddings + salience + decay
    Surface — budget engine (5 chunks / 500 tokens)
    Depth   — dream synthesis (5 dimensions)

Backed by the Cortex neural interlink (vector BLOB, graph, episodic).
"""

__version__ = "2.0.0"