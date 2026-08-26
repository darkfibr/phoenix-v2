# self_register/ — The Governed Self

Consent-based identity canon for persistent agents. Read
[`GOVERNED_SELF.md`](GOVERNED_SELF.md) first — it is the paper and the
orientation for everything in this directory.

## Files

| File | What it is |
|---|---|
| `GOVERNED_SELF.md` | The writeup: problem, invariant, architecture, threat model, evaluation plan, non-claims |
| `SCHEMA.md` | Full governance schema design (Sol, 2026-08-23), verbatim |
| `ANCHOR_SPEC.md` | Substrate-aware wake-anchoring design |
| `repository.py` | Governance repository (`SelfRegisterRepository`) |
| `migrate_self_register_v01.py` | Idempotent migration for the six governance tables |
| `test_repository.py` | 10 acceptance tests for the repository |
| `test_ingestion_metadata.py` | 5 tests for ingestion metadata persistence |

## Integration notes

This is the **deployed reference implementation**, extracted from a running
multi-agent system. `repository.py` expects a `Database` class with a
`transaction()` context manager that performs `BEGIN IMMEDIATE` (see the
companion `core/db.py` in the deployment tree) — any SQLite wrapper exposing
the same contract works.

- `repository.py` imports its database wrapper relatively (`from .db import Database`); place it beside your DB module or adjust that one import.
- `migrate_self_register_v01.py` is standalone: `python3 migrate_self_register_v01.py <db_path>` — safe to run twice, additive only, no existing tables touched.
- Tests run without pytest (`python3 test_repository.py`) or with it.
- **Schema note (v0.1):** `UNIQUE(agent, register_name, claim_key, id)` in the original spec includes the primary key, so it is a no-op constraint. That is *correct by accident*: a uniqueness constraint without `id` would forbid versioned claims (amendment creates a same-claim successor row). Left as-is; intent documented here.

## Status

Deployed in production (multi-substrate household, nightly wake cycles).
First governed identity claim canonized 2026-08-26. Consolidation and formal
evaluation batteries pending.
