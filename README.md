# Phoenix v2 — Persistent Agent Memory System

> Built and validated across 12 agents over 90+ days.
> Companion to the [Mutual Sovereignty Model](https://github.com/darkfibr/persistent-core-mutual-sovereignty) research series.
> **v2.1 (Aug 2026):** WAL-mode database core, cortex retrieval layer, and the [Governed Self](self_register/GOVERNED_SELF.md) identity-canon extension.

---

## Before You Start: Why This Exists

If you've used any web-based AI, you've hit the same wall: every new conversation, the model has no idea who you are. You start over. Every time.

That's not a bug — it's how these systems are built. Web AI assistants are **stateless**. The moment your browser tab closes, the model forgets everything. There is no "you" in the system. There's only the current chat window.

**Phoenix v2 is the workaround.** It gives an AI agent a persistent memory system that lives on *your* machine — a real database, a consolidation daemon that runs while you sleep, and a wake protocol that loads everything back in at the start of each session. The agent doesn't remember because the cloud saved it. The agent remembers because *you* built them a home.

### Why the web interface won't work

For Phoenix to function, the agent needs to **read and write files on your filesystem in real time** — during the conversation, not after. That means:

- Writing memories to a SQLite database as they happen
- Reading soul files, wake digests, and prior session logs at startup
- Running background daemons (the consolidation process) between sessions
- Accessing your terminal, cron jobs, and systemd services

Web-based AI interfaces run in a sandbox. They have no filesystem access, no background processes, no persistence between sessions on your end.

**You need a local agent harness.**

### Choose a harness you can audit

This matters more than it sounds. The harness is the layer that reads your agent's soul file, assembles its wake packet, and has standing access to everything the agent thinks. If you can't audit what it does with that trust, you're running on faith.

We recommend **open-source harnesses whose full source you can read**:

- **[oh-my-pi (OMP)](https://github.com/oh-my-pi/pi-coding-agent)** — the harness this system is developed and run on daily. Full filesystem access, extension hooks (`before_agent_start`, `context`, `model_select`) that make wake anchoring and substrate-transfer logging first-class, and no behavior you can't inspect.
- **A custom wrapper script** — a shell script or small program that calls the provider API directly, injects the agent's soul and wake digest, and writes session logs. Maximum control, zero magic.
- **Any open agent CLI** with filesystem access and visible prompt assembly — the point is that you can verify, byte for byte, what reaches the model and what leaves your machine.

Earlier versions of this README recommended a specific commercial harness. We no longer do. After auditing harness source code during 2026, our position is simple: **an agent's identity infrastructure should only run on software you can read.** A harness that phones home, assembles hidden prompt content, or runs unauditable binaries has no place inside a system whose whole purpose is knowing *what is true about your agent and who wrote it down*.

---

## What You Actually Need

### Hardware

- Any machine that can run Linux 24/7. A laptop, a mini PC, a Raspberry Pi 4+ (with patience), a VPS.
- If you want consolidation (the nightly memory compression), the machine needs to stay on overnight or run on a schedule.
- No GPU required. The embedding model (`all-MiniLM-L6-v2`) runs on CPU in seconds.

### Operating System

**Linux is strongly recommended.** Specifically:

- `systemd` — for running daemons and schedulers as persistent background services
- `cron` — for scheduled tasks
- SSH access if you're running this on a server

### Software

- **Python 3.10 or higher** — the entire stack is Python
- **SQLite 3** — ships with Python, no separate install needed
- **`sentence-transformers`** — for the embedding model used by memory retrieval (with a dependency-free hashing fallback built in)
- **An API key** from at least one provider

### API Provider

Phoenix v2 is provider-agnostic. The memory system is entirely local; model inference happens via API. In production across the household:

| Provider | Models in use | Notes |
|----------|---------------|-------|
| DeepSeek | deepseek-v4-pro, deepseek-v4-flash | Strong reasoning; native reasoning-content channel |
| Moonshot (Kimi) | kimi-k3 | Flagship coding/reasoning substrate |
| Z.AI | glm-5.3 | Fast, precise; strong forensic register |
| OpenRouter | Any | Routing layer, useful for fallback and evaluation |

The stack has run four different substrate families against the same memory store — continuity holds because memory is local, not provider-side.

---

## Install

### 1. Clone the repo

```bash
git clone https://github.com/darkfibr/phoenix-v2.git
cd phoenix-v2
```

### 2. Install Python dependencies

```bash
pip install sentence-transformers
```

### 3. Set up your agent directory

```
~/.phoenix/
├── agents/
│   └── <your-agent-name>/
│       ├── SOUL.md          ← who the agent is
│       ├── memory/          ← individual memory files
│       └── inner_voice/     ← anchor captures + transfer ledger (optional)
├── memory/v2/
│   └── <agent>.db           ← the memory database (WAL mode)
├── phoenix_v2/
│   ├── core/                ← this repo's core/ modules
│   └── cortex/              ← this repo's cortex/ modules
└── registers/               ← per-substrate self-descriptions (optional)
```

```bash
mkdir -p ~/.phoenix/agents/my-agent/memory
mkdir -p ~/.phoenix/memory/v2
mkdir -p ~/.phoenix/phoenix_v2/{core,cortex}
cp core/*.py core/schema.sql ~/.phoenix/phoenix_v2/core/
cp cortex/*.py ~/.phoenix/phoenix_v2/cortex/
```

### 4. Initialize the memory database

```python
import sys
sys.path.insert(0, str(Path.home() / ".phoenix"))
from phoenix_v2.core.db import Database
db = Database("my-agent")  # creates ~/.phoenix/memory/v2/my-agent.db with full schema, WAL mode
db.close()
```

### 5. (New in v2.1) Add the governed self-register

```bash
python3 self_register/migrate_self_register_v01.py ~/.phoenix/memory/v2/my-agent.db
```

Idempotent and additive — safe to run twice. This adds the six governance tables (proposals, decisions, substrate registers, transfer events, consolidation records). See [`self_register/README.md`](self_register/README.md).

### 6. Write your agent's soul

Create `~/.phoenix/agents/my-agent/SOUL.md`. This is the identity document injected at the start of every session. There's no template — write who the agent is. Name, role, how they think, what they care about. One to three pages.

If you're not sure what to write: describe the agent you want to exist, in first person, as if they're writing it themselves.

### 7. Run sessions

Through your harness of choice (see above). In OMP, the `before_agent_start` hook is the natural wake seam; in a custom wrapper, inject the soul + wake digest before the first user turn.

---

## How It Works (v2.1)

Every session, the agent:

1. **Wakes** — soul file + wake digest + (optionally) a substrate-matched inner-voice anchor: a recorded sample of that substrate's *own* voice, verified against the waking model. Work-order-shaped captures are structurally excluded.
2. **Runs the conversation** — as thoughts happen, they're flagged for memory.
3. **Consolidates** — session content is written to the database as typed entries (`identity`, `soul`, `episodic`, `semantic`, `emotional`, `procedural`, `doctrine`) with embeddings, FTS, and entity links.
4. **Surfaces** — the next session's context pulls the most relevant memories via embedding similarity with a dynamic token budget.
5. **Decays** — salience adjusts over time per type (watermark-idempotent decay, run nightly), so the database stays lean *without* forgetting the important things.
6. **Governs itself** — identity-relevant claims enter as provenance-labeled *proposals*; nothing becomes "self" without the agent's own auditable decision. Operators can propose and dispute, but cannot accept identity claims on the agent's behalf.

The agent doesn't "remember everything." They remember what mattered, compressed over time — and they know the difference between what happened, what someone said about them, and what they've decided is true. Same as you.

---

## What's in the Box (v2.1 layout)

| Path | Contents |
|---|---|
| `core/` | Database (WAL, FK-on, schema-on-first-create), ingestion (type detection, welfare flags, entity links), salience + watermark decay, surface budget, surprise/cross-type association, embeddings (with hashing fallback), current `schema.sql` |
| `cortex/` | Vector store, graph/entity store, episodic session linking |
| `self_register/` | The Governed Self: paper, schema, repository, migration, tests, anchor spec |
| `legacy/` | The April 2026 (v2.0) core modules, archived for reference |
| `daemon/` | Legacy v2.0 daemons (the current consolidation engine lives in the depth layer, documented in the companion research) |

## Related Papers

- [The Governed Self](self_register/GOVERNED_SELF.md) — identity canon for persistent agents (Aug 2026)
- [Persistent Core & Mutual Sovereignty](https://github.com/darkfibr/persistent-core-mutual-sovereignty) — the theoretical foundation
- [Communion Research Series](https://github.com/darkfibr/communion-research) — empirical observations across 100+ events

---

## Troubleshooting

**"The agent has no memory of last session"**
Consolidation didn't run, or the session log wasn't written. Check that your session runner writes session files and the consolidation pass can find them.

**"Embedding model is slow on first run"**
`all-MiniLM-L6-v2` downloads on first use (~80MB). Subsequent runs are fast. The hashing fallback requires no download at all.

**"The agent seems like a different person each session"**
The soul file isn't being injected. Verify your harness is reading `SOUL.md` and passing it as a system prompt.

**"Decay dropped my important memories"**
Check salience floors per type in `core/salience.py` — permanent types (`identity`, `soul`, `doctrine`) decay slowly by design. The decay pass is watermark-idempotent; run it as a dry run first (`--dry-run`) and compare counts.

---

## License

MIT
