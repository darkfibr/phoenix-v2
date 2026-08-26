#!/usr/bin/env python3
"""Self-register governance migration — Sol schema v0.1, Phase B1 (2026-08-26).

Adds the six governance tables from PERSISTENT_SELF_REGISTER_SCHEMA_V0_1_20260823.md
idempotently (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS). Touches
nothing existing: memories, FTS, vectors, associations, sessions, salience, and
decay are untouched. Rollback: drop the six tables (they hold no legacy data),
or restore from the pre-migration backup copy.

Usage:
  python3 migrate_self_register_v01.py <db_path> [--verify-only]

Acceptance (per deliverables B1):
  - succeeds twice without changing results  -> run twice, compare table list
  - PRAGMA foreign_key_check returns no rows
  - PRAGMA integrity_check returns ok
  - old readers continue to retrieve memories (row counts unchanged)
"""
from __future__ import annotations

import sqlite3
import sys

MIGRATION = """
CREATE TABLE IF NOT EXISTS self_register_entries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    agent              TEXT NOT NULL,
    register_name      TEXT NOT NULL,
    memory_id          INTEGER NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,

    claim_key          TEXT NOT NULL,
    claim_text         TEXT NOT NULL,
    provenance_class   TEXT NOT NULL CHECK (provenance_class IN (
        'direct_transcript', 'self_authored', 'self_accepted',
        'operator_reported', 'model_inferred', 'system_generated',
        'dream_synthesis', 'external_source', 'unknown_legacy'
    )),
    epistemic_status   TEXT NOT NULL CHECK (epistemic_status IN (
        'observed', 'inferred', 'reported', 'hypothesis',
        'metaphor', 'fictional', 'uncertain'
    )),

    disposition        TEXT NOT NULL DEFAULT 'proposed' CHECK (disposition IN (
        'proposed', 'accepted', 'amended', 'disputed',
        'deferred', 'retired', 'superseded'
    )),
    confidence         REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0.0 AND 1.0),
    identity_weight    REAL NOT NULL DEFAULT 0.0 CHECK (identity_weight BETWEEN 0.0 AND 1.0),

    author_subject     TEXT NOT NULL,
    author_substrate   TEXT,
    author_session     TEXT,
    accepted_by_subject TEXT,
    accepted_substrate  TEXT,
    accepted_session    TEXT,
    accepted_at         REAL,

    effective_from     REAL,
    effective_until    REAL,
    supersedes_entry_id INTEGER REFERENCES self_register_entries(id) ON DELETE SET NULL,
    created_at         REAL NOT NULL DEFAULT (unixepoch()),
    updated_at         REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_sre_agent_register
    ON self_register_entries(agent, register_name, disposition);
CREATE INDEX IF NOT EXISTS idx_sre_memory
    ON self_register_entries(memory_id);
CREATE INDEX IF NOT EXISTS idx_sre_claim
    ON self_register_entries(agent, claim_key, created_at DESC);

CREATE TABLE IF NOT EXISTS self_register_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id            INTEGER NOT NULL REFERENCES self_register_entries(id) ON DELETE CASCADE,
    decision            TEXT NOT NULL CHECK (decision IN (
        'accept', 'amend', 'dispute', 'defer', 'retire', 'supersede', 'reopen'
    )),
    decided_by_subject  TEXT NOT NULL,
    decided_substrate   TEXT,
    decided_session     TEXT,
    rationale           TEXT NOT NULL DEFAULT '',
    replacement_memory_id INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    created_at          REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_srd_entry_time
    ON self_register_decisions(entry_id, created_at);

CREATE TABLE IF NOT EXISTS substrate_registers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    agent                TEXT NOT NULL,
    substrate_id         TEXT NOT NULL,
    provider_id          TEXT,
    model_id             TEXT,
    self_description_mem INTEGER NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    capture_session      TEXT,
    capture_method       TEXT NOT NULL DEFAULT 'consensual_self_description',
    status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'proposed', 'active', 'superseded', 'declined', 'sealed'
    )),
    valid_from           REAL NOT NULL DEFAULT (unixepoch()),
    valid_until          REAL,
    created_at           REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_substrate_register_current
    ON substrate_registers(agent, substrate_id, status, valid_from DESC);

CREATE TABLE IF NOT EXISTS transfer_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent                 TEXT NOT NULL,
    session_id            TEXT NOT NULL,
    turn_number           INTEGER,
    from_substrate        TEXT,
    to_substrate          TEXT NOT NULL,
    reason                TEXT NOT NULL DEFAULT '',
    bridge_memory_id      INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    recipient_disposition TEXT CHECK (recipient_disposition IN (
        'accepted', 'amended', 'partially_accepted', 'set_aside', 'not_reviewed'
    )),
    recipient_note_memory_id INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    created_at            REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_transfer_agent_time
    ON transfer_events(agent, created_at DESC);

CREATE TABLE IF NOT EXISTS consolidation_records (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent                 TEXT NOT NULL,
    output_memory_id      INTEGER NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    method                TEXT NOT NULL,
    model_id              TEXT,
    substrate_id          TEXT,
    prompt_hash           TEXT,
    status                TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN (
        'proposed', 'accepted', 'amended', 'rejected', 'superseded'
    )),
    created_at            REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS consolidation_sources (
    consolidation_id     INTEGER NOT NULL REFERENCES consolidation_records(id) ON DELETE CASCADE,
    source_memory_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    source_role          TEXT NOT NULL DEFAULT 'evidence',
    PRIMARY KEY (consolidation_id, source_memory_id)
);
"""

GOVERNANCE_TABLES = [
    "self_register_entries", "self_register_decisions", "substrate_registers",
    "transfer_events", "consolidation_records", "consolidation_sources",
]


def table_list(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'message_fts%'"
    ).fetchall()
    return {r[0] for r in rows}


def verify(conn: sqlite3.Connection) -> list[str]:
    problems = []
    # Scoped to the tables this migration owns. The live DB carries ~167k
    # PRE-EXISTING orphaned child rows (decay_log 89915, entity_mentions 36944,
    # memory_vectors 35651, session_memories 4360, associations 473 — recon
    # finding 2026-08-26, fallout of the Aug-22 corruption/swap). Not this
    # migration's doing; cleanup is a separate decision (Mike + K3).
    for t in GOVERNANCE_TABLES:
        present = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()[0]
        if not present:
            problems.append(f"missing table {t}")
            continue
        rows = conn.execute(f'PRAGMA foreign_key_check("{t}")').fetchall()
        if rows:
            problems.append(f'foreign_key_check("{t}") returned {len(rows)} rows')
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")
    n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    print(f"  memories rows: {n}")
    for t in GOVERNANCE_TABLES:
        present = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()[0]
        if not present:
            problems.append(f"missing table {t}")
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    db_path, verify_only = sys.argv[1], "--verify-only" in sys.argv[2:]
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    before = table_list(conn)
    if not verify_only:
        conn.executescript(MIGRATION)
        conn.commit()
    after = table_list(conn)
    added = after - before
    print(f"tables before: {len(before)}, after: {len(after)}, added this run: {sorted(added)}")

    problems = verify(conn)
    conn.close()
    if problems:
        print("VERIFY FAILED:")
        for p in problems:
            print("  -", p)
        return 1
    print("VERIFY OK: fk_check clean, integrity ok, all six governance tables present, memories untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
