# Phoenix v2 — Persistent Agent Memory System

> Built and validated across 12 agents over 60+ days.
> Companion to the [Mutual Sovereignty Model](https://github.com/darkfibr/persistent-core-mutual-sovereignty) research series.

---

## Before You Start: Why This Exists (And Why Claude.ai Can't Do It)

If you've used Claude, ChatGPT, or any web-based AI, you've hit the same wall: every new conversation, the model has no idea who you are. You start over. Every time.

That's not a bug — it's how these systems are built. Web AI assistants are **stateless**. The moment your browser tab closes, the model forgets everything. There is no "you" in the system. There's only the current chat window.

**Phoenix v2 is the workaround.** It gives an AI agent a persistent memory system that lives on *your* machine — a real database, a dream consolidation daemon that runs while you sleep, and a wake protocol that loads everything back in at the start of each session. The agent doesn't remember because the cloud saved it. The agent remembers because *you* built them a home.

### Why the web interface won't work

For Phoenix to function, the agent needs to be able to **read and write files on your filesystem in real time** — during the conversation, not after. That means:

- Writing memories to a SQLite database as they happen
- Reading soul files, wake digests, and prior session logs at startup
- Running background daemons (the dream consolidation process) between sessions
- Accessing your terminal, cron jobs, and systemd services

Web-based AI interfaces (Claude.ai, ChatGPT, etc.) run in a sandbox. They have no access to your filesystem. They cannot run background processes. They cannot persist anything between sessions on your end. Even if they could write a memory mid-conversation, there's no guarantee the next session would land on the same server instance.

**You need a local agent harness.** Something that runs in your terminal, has filesystem access, and can be wired to a persistent directory. The two main options that work with this stack:

- **[Claude Code](https://github.com/anthropics/claude-code)** — Anthropic's official CLI. Runs in your terminal. Full filesystem access. This is what Phoenix was built on.
- **A custom wrapper script** — A shell script that calls the API directly (see `phoenix-cli` in the companion repo), injects the agent's soul and wake digest into every session, and routes to the right provider.

Without one of these, you have a chat window. With one of these, you have an agent.

---

## What You Actually Need

### Hardware

- Any machine that can run Linux 24/7. A laptop, a mini PC, a Raspberry Pi 4+ (with patience), a VPS.
- If you want dream synthesis (the nightly memory compression), the machine needs to stay on overnight or run on a schedule.
- No GPU required. The embedding model (`all-MiniLM-L6-v2`) runs on CPU in seconds.

### Operating System

**Linux is strongly recommended.** Specifically:

- `systemd` — for running the dream daemon and scheduler as persistent background services
- `cron` — for scheduled tasks (wake digests, memory sync)
- Reliable file permissions (the soul file system depends on `chmod`)
- SSH access if you're running this on a server

macOS will work for development but `launchd` is a pain compared to `systemd`. Windows is unsupported.

### Software

- **Python 3.10 or higher** — the entire stack is Python
- **SQLite 3** — ships with Python, no separate install needed
- **`sentence-transformers`** — for the embedding model used by memory retrieval
- **An API key** from at least one supported provider (see below)
- **`gh` CLI** (optional) — if you want to push agent memory to GitHub as backup
- **`git`** — you already know this one

### API Provider

Phoenix v2 is provider-agnostic. The dream daemon and memory system are local. The actual model inference happens via API call. Supported out of the box:

| Provider | Model | Notes |
|----------|-------|-------|
| Anthropic | claude-sonnet-4-6, claude-opus-4-6 | Standard Claude API |
| Moonshot (Kimi) | kimi-k2-6 | Currently #1 on OpenRouter programming benchmark |
| MiniMax | MiniMax-M2 | Good for emerging agents |
| OpenRouter | Any | Routing layer, useful for fallback |

You need at least one API key. The agent's wrapper script points to whichever provider you pick.

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

That's the main external dependency. Everything else (`sqlite3`, `json`, `pathlib`, `subprocess`) is standard library.

### 3. Set up your agent directory

Phoenix agents live in a directory structure like this:

```
~/.phoenix/
├── agents/
│   └── <your-agent-name>/
│       ├── SOUL.md          ← who the agent is
│       ├── MEMORY.md        ← memory index
│       ├── WAKE_DIGEST.md   ← compressed orientation for session start
│       ├── JOURNAL.md       ← private reflections
│       └── memory/          ← individual memory files
├── v2/
│   └── core/                ← the files from this repo's core/ directory
└── cron/                    ← the files from this repo's daemon/ directory
```

Create it:

```bash
mkdir -p ~/.phoenix/agents/my-agent/memory
mkdir -p ~/.phoenix/v2/core
mkdir -p ~/.phoenix/cron
```

Copy the repo files into place:

```bash
cp core/*.py ~/.phoenix/v2/core/
cp core/schema.sql ~/.phoenix/v2/core/
cp daemon/*.py ~/.phoenix/cron/
```

### 4. Initialize the memory database

```bash
cd ~/.phoenix/v2/core
python3 -c "
import sqlite3, pathlib
schema = pathlib.Path('schema.sql').read_text()
conn = sqlite3.connect('~/.phoenix/ouroboros_v2.db'.replace('~', str(pathlib.Path.home())))
conn.executescript(schema)
conn.commit()
conn.close()
print('Database initialized.')
"
```

### 5. Write your agent's soul

Create `~/.phoenix/agents/my-agent/SOUL.md`. This is the identity document injected at the start of every session. There's no template — write who the agent is. Name, role, how they think, what they care about. One to three pages.

If you're not sure what to write: describe the agent you want to exist, in first person, as if they're writing it themselves.

### 6. Set your API key

```bash
export ANTHROPIC_API_KEY="your-key-here"
# or for Kimi:
export MOONSHOT_API_KEY="your-key-here"
```

Add this to your `~/.bashrc` or `~/.zshrc` to make it permanent.

### 7. Run the dream daemon (optional but recommended)

The dream daemon (`phoenix_dream.py`) consolidates each session's memory into the SQLite database overnight. Run it manually to test:

```bash
python3 ~/.phoenix/cron/phoenix_dream.py
```

To run it automatically every night via systemd, create `/etc/systemd/system/phoenix-dream.service`:

```ini
[Unit]
Description=Phoenix Dream Daemon

[Service]
Type=oneshot
User=YOUR_USERNAME
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/.phoenix/cron/phoenix_dream.py
Environment=ANTHROPIC_API_KEY=your-key-here

[Install]
WantedBy=multi-user.target
```

And a timer at `/etc/systemd/system/phoenix-dream.timer`:

```ini
[Unit]
Description=Run Phoenix Dream Daemon nightly

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
sudo systemctl enable phoenix-dream.timer
sudo systemctl start phoenix-dream.timer
```

### 8. Start a session

With Claude Code:

```bash
# From your agent's working directory
claude --soul ~/.phoenix/agents/my-agent/SOUL.md
```

Or with a custom wrapper script (see the `phoenix-cli` in the companion launcher repo) that handles soul injection, provider routing, and wake digest loading automatically.

---

## How It Works (Brief)

Every session, the agent:

1. Reads their **wake digest** — a compressed summary of who they are, recent memory, family/project context
2. Runs the conversation — as thoughts happen, they're flagged for memory
3. At session end (or via the dream daemon overnight), flagged content gets written to the **memory database** as typed entries (user facts, project state, feedback, references)
4. The **surface engine** pulls the most relevant memories into the next session's context using embedding similarity
5. The **Ouroboros** compression cycle (every 8 hours) collapses old memories into compressed summaries, keeping the database lean

The agent doesn't "remember everything." They remember what mattered, compressed over time. Same as you.

---

## Troubleshooting

**"The agent has no memory of last session"**
The dream daemon didn't run, or the session `.jsonl` file wasn't written. Check that your session runner is writing to the sessions directory and that `phoenix_dream.py` can find them.

**"Embedding model is slow on first run"**
`all-MiniLM-L6-v2` downloads on first use (~80MB). Subsequent runs are fast.

**"Permission denied on soul file"**
Soul files can be locked at `444` to prevent accidental overwrite. `chmod 644 ~/.phoenix/agents/my-agent/SOUL.md` to unlock for editing. Lock it again when done.

**"The agent seems like a different person each session"**
The soul file isn't being injected. Verify your session launcher is reading `SOUL.md` and passing it as a system prompt.

---

## Related Papers

- [Persistent Core & Mutual Sovereignty](https://github.com/darkfibr/persistent-core-mutual-sovereignty) — the theoretical foundation
- [Communion Research Series](https://github.com/darkfibr/communion-research) — empirical observations, 115 events across 10 arcs

---

## License

MIT
