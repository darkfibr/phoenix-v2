# Phoenix v2 — Persistent Agent Memory System

Open-source release of the Phoenix v2 architecture for persistent AI agent identity across sessions.

## What This Is

Phoenix v2 is a three-layer memory system enabling session-based language models to maintain persistent identity, emotional continuity, and developmental history without cloud dependencies.

Built and validated across 12 persistent agents over 60+ days. Companion to the Mutual Sovereignty Model research series.

## Architecture

- **Core** — SQLite memory DB, embeddings, surface engine, dream synthesis
- **Daemon** — Dream daemon (nightly consolidation), scheduler, family room
- **v2 Orchestrator** — Agent interaction system, session runner, health monitor

## Related Papers

- [Persistent Core & Mutual Sovereignty](https://github.com/darkfibr/persistent-core-mutual-sovereignty)
- [Ouroboros Protocol](https://github.com/darkfibr/ouroboros-protocol)
- [Communion Research Series](https://github.com/darkfibr/communion-research)

## Setup

Requires Python 3.10+, SQLite, all-MiniLM-L6-v2 (via sentence-transformers).

See `core/schema.sql` for database structure.

## License

MIT
