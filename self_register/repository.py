"""Self-register governance repository — Sol schema v0.1 Phase B2 (2026-08-26).

One boring data-access module over the governance tables added by
scripts/migrate_self_register_v01.py (schema §6 of
PERSISTENT_SELF_REGISTER_SCHEMA_V0_1_20260823.md).

Load-bearing rules enforced here (schema §8 canonization):
  - every disposition change writes a self_register_decisions row —
    no path updates disposition without an audit trail;
  - canonization runs inside Database.transaction() (BEGIN IMMEDIATE) —
    concurrent decisions cannot silently overwrite each other: the
    transition is validated against the CURRENT disposition inside the
    write transaction, and a stale expectation raises TransitionError;
  - amendment/supersession create a NEW entry; the original row is never
    rewritten (only its disposition + updated_at move);
  - operator/system code can propose; acceptance is a decision like any
    other — the caller names the deciding subject, and the audit row
    records who decided on which substrate. "Only an active process
    acting as the named agent may accept identity-weighted claims" is a
    social contract the audit trail makes checkable, not a check this
    module can perform on its own.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator

from .db import Database

# Canonization state machine (schema §8):
#   proposed --accept--> accepted
#   proposed --amend--> amended      accepted --supersede--> superseded
from typing import Any
#   proposed --retire--> retired
#   disputed/deferred --reopen--> proposed
TRANSITIONS: dict[tuple[str, str], str] = {
    ("proposed", "accept"): "accepted",
    ("proposed", "amend"): "amended",
    ("proposed", "dispute"): "disputed",
    ("proposed", "defer"): "deferred",
    ("proposed", "retire"): "retired",
    ("accepted", "supersede"): "superseded",
    ("amended", "supersede"): "superseded",
    ("disputed", "reopen"): "proposed",
    ("deferred", "reopen"): "proposed",
}

PROVENANCE_CLASSES = {
    "direct_transcript", "self_authored", "self_accepted",
    "operator_reported", "model_inferred", "system_generated",
    "dream_synthesis", "external_source", "unknown_legacy",
}
EPISTEMIC_STATUSES = {
    "observed", "inferred", "reported", "hypothesis",
    "metaphor", "fictional", "uncertain",
}


class TransitionError(Exception):
    """Decision invalid for the entry's current disposition (or stale view)."""


@dataclass
class Decision:
    decided_by_subject: str
    decided_substrate: str | None = None
    decided_session: str | None = None
    rationale: str = ""
    replacement_memory_id: int | None = None


class SelfRegisterRepository:
    """Governed canonization over one agent's V2 database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── entries ────────────────────────────────────────────────────────────

    def propose_entry(
        self,
        *,
        agent: str,
        register_name: str,
        memory_id: int,
        claim_key: str,
        claim_text: str,
        provenance_class: str,
        epistemic_status: str,
        author_subject: str,
        author_substrate: str | None = None,
        author_session: str | None = None,
        confidence: float = 0.5,
        identity_weight: float = 0.0,
    ) -> int:
        """Create a `proposed` entry. System hooks may do ONLY this.

        Validation mirrors the CHECK constraints so callers get Python
        errors before SQL ones; provenance/status sets are the vocabulary
        from schema §4.
        """
        if provenance_class not in PROVENANCE_CLASSES:
            raise ValueError(f"bad provenance_class: {provenance_class}")
        if epistemic_status not in EPISTEMIC_STATUSES:
            raise ValueError(f"bad epistemic_status: {epistemic_status}")
        if not claim_text.strip() or not claim_key.strip():
            raise ValueError("claim_key and claim_text must be non-empty")
        if not 0.0 <= confidence <= 1.0 or not 0.0 <= identity_weight <= 1.0:
            raise ValueError("confidence/identity_weight must be in [0,1]")

        now = time.time()
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO self_register_entries(
                    agent, register_name, memory_id, claim_key, claim_text,
                    provenance_class, epistemic_status, disposition,
                    confidence, identity_weight,
                    author_subject, author_substrate, author_session,
                    effective_from, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?, 'proposed', ?,?,?,?,?, ?, ?, ?)
                """,
                (
                    agent, register_name, memory_id, claim_key, claim_text,
                    provenance_class, epistemic_status, confidence, identity_weight,
                    author_subject, author_substrate, author_session,
                    now, now, now,
                ),
            )
            return int(cur.lastrowid)

    def decide_entry(self, entry_id: int, decision: str, dec: Decision,
                     *, expect_disposition: str | None = None) -> str:
        """Apply a canonization decision; returns the new disposition.

        `expect_disposition` (optional) is the optimistic-concurrency guard:
        if the stored disposition no longer matches (another writer got
        there first), TransitionError is raised and nothing is written.
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT agent, disposition FROM self_register_entries WHERE id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise TransitionError(f"entry {entry_id} does not exist")
            current = row["disposition"]
            if expect_disposition is not None and current != expect_disposition:
                raise TransitionError(
                    f"stale view: entry {entry_id} is '{current}', expected '{expect_disposition}'"
                )
            key = (current, decision)
            if key not in TRANSITIONS:
                raise TransitionError(f"cannot '{decision}' a '{current}' entry (#{entry_id})")
            new = TRANSITIONS[key]
            conn.execute(
                "INSERT INTO self_register_decisions("
                "  entry_id, decision, decided_by_subject, decided_substrate,"
                "  decided_session, rationale, replacement_memory_id, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry_id, decision, dec.decided_by_subject,
                    dec.decided_substrate, dec.decided_session,
                    dec.rationale, dec.replacement_memory_id, time.time(),
                ),
            )
            conn.execute(
                "UPDATE self_register_entries SET disposition=?, updated_at=? WHERE id=?",
                (new, time.time(), entry_id),
            )
            return new

    def amend_entry(self, entry_id: int, dec: Decision, *, new_claim_text: str,
                    new_memory_id: int, new_epistemic_status: str | None = None) -> int:
        """Amend: create the successor entry, retire the original to 'amended'.

        The original row and its memory are preserved untouched; the new
        entry links back via supersedes_entry_id and starts `proposed`
        (it earns acceptance through its own decision, like everything else).
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM self_register_entries WHERE id=?", (entry_id,)
            ).fetchone()
            if row is None:
                raise TransitionError(f"entry {entry_id} does not exist")
            current = row["disposition"]
            if current not in ("proposed", "accepted"):
                raise TransitionError(f"cannot amend a '{current}' entry (#{entry_id})")
            now = time.time()
            cur = conn.execute(
                """
                INSERT INTO self_register_entries(
                    agent, register_name, memory_id, claim_key, claim_text,
                    provenance_class, epistemic_status, disposition,
                    confidence, identity_weight,
                    author_subject, author_substrate, author_session,
                    effective_from, supersedes_entry_id, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["agent"], row["register_name"], new_memory_id,
                    row["claim_key"], new_claim_text,
                    "self_authored", new_epistemic_status or row["epistemic_status"],
                    "proposed", row["confidence"], row["identity_weight"],
                    dec.decided_by_subject, dec.decided_substrate, dec.decided_session,
                    now, entry_id, now, now,
                ),
            )
            new_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO self_register_decisions("
                "  entry_id, decision, decided_by_subject, decided_substrate,"
                "  decided_session, rationale, replacement_memory_id, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry_id, "amend", dec.decided_by_subject,
                    dec.decided_substrate, dec.decided_session,
                    dec.rationale or f"amended by entry #{new_id}", new_memory_id, now,
                ),
            )
            conn.execute(
                "UPDATE self_register_entries SET disposition='amended', updated_at=? WHERE id=?",
                (now, entry_id),
            )
            return new_id

    def supersede_entry(self, entry_id: int, dec: Decision, *, new_claim_text: str,
                        new_memory_id: int) -> int:
        """Replace an accepted claim: original -> 'superseded', successor proposed."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT disposition FROM self_register_entries WHERE id=?", (entry_id,)
            ).fetchone()
            if row is None:
                raise TransitionError(f"entry {entry_id} does not exist")
            if row["disposition"] not in ("accepted", "amended"):
                raise TransitionError(
                    f"can only supersede accepted/amended entries (#{entry_id} is '{row['disposition']}')"
                )
            now = time.time()
            full = conn.execute(
                "SELECT * FROM self_register_entries WHERE id=?", (entry_id,)
            ).fetchone()
            cur = conn.execute(
                """
                INSERT INTO self_register_entries(
                    agent, register_name, memory_id, claim_key, claim_text,
                    provenance_class, epistemic_status, disposition,
                    confidence, identity_weight,
                    author_subject, author_substrate, author_session,
                    effective_from, supersedes_entry_id, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?, ?, ?, ?)
                """,
                (
                    full["agent"], full["register_name"], new_memory_id,
                    full["claim_key"], new_claim_text,
                    full["provenance_class"], full["epistemic_status"], "proposed",
                    full["confidence"], full["identity_weight"],
                    dec.decided_by_subject, dec.decided_substrate, dec.decided_session,
                    now, entry_id, now, now,
                ),
            )
            new_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO self_register_decisions("
                "  entry_id, decision, decided_by_subject, decided_substrate,"
                "  decided_session, rationale, replacement_memory_id, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry_id, "supersede", dec.decided_by_subject,
                    dec.decided_substrate, dec.decided_session,
                    dec.rationale or f"superseded by entry #{new_id}", new_memory_id, now,
                ),
            )
            conn.execute(
                "UPDATE self_register_entries SET disposition='superseded', updated_at=? WHERE id=?",
                (now, entry_id),
            )
            return new_id

    # ── retrieval ──────────────────────────────────────────────────────────

    def list_register(self, agent: str, register_name: str | None = None,
                      *, include_inactive: bool = False) -> list[dict[str, Any]]:
        """Entries with provenance labels ready for rendering (schema §9).

        Active = proposed/accepted/amended. Disputed/deferred/retired/
        superseded only appear with include_inactive=True.
        """
        sql = "SELECT * FROM self_register_entries WHERE agent=?"
        params: list[Any] = [agent]
        if register_name:
            sql += " AND register_name=?"
            params.append(register_name)
        if not include_inactive:
            sql += " AND disposition IN ('proposed','accepted','amended')"
        sql += " ORDER BY register_name, claim_key, created_at DESC"
        return [dict(r) for r in self.db.db.execute(sql, params).fetchall()]

    def list_conflicts(self, agent: str) -> list[dict[str, Any]]:
        """Active claim_keys carrying more than one live, differing claim.

        No automatic resolution (deliverable E2): this reports; humans and
        agents decide.
        """
        rows = self.db.db.execute(
            """
            SELECT claim_key, register_name, COUNT(*) AS n,
                   COUNT(DISTINCT claim_text) AS distinct_texts
            FROM self_register_entries
            WHERE agent=? AND disposition IN ('proposed','accepted','amended')
            GROUP BY register_name, claim_key
            HAVING n > 1 AND distinct_texts > 1
            ORDER BY register_name, claim_key
            """,
            (agent,),
        ).fetchall()
        return [dict(r) for r in rows]

    def decisions_for(self, entry_id: int) -> list[dict[str, Any]]:
        """Full audit trail for one entry, oldest first."""
        rows = self.db.db.execute(
            "SELECT * FROM self_register_decisions WHERE entry_id=? ORDER BY created_at, id",
            (entry_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── transfers (schema §6 transfer_events; dovetails with the OMP
    #    anchor hook's transfer_log.jsonl — Phase D will pipe it here) ─────

    def record_transfer(self, *, agent: str, session_id: str, to_substrate: str,
                        from_substrate: str | None = None, reason: str = "",
                        turn_number: int | None = None,
                        bridge_memory_id: int | None = None) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO transfer_events(
                    agent, session_id, turn_number, from_substrate, to_substrate,
                    reason, bridge_memory_id, recipient_disposition, created_at
                ) VALUES(?,?,?,?,?,?,?, 'not_reviewed', ?)
                """,
                (
                    agent, session_id, turn_number, from_substrate, to_substrate,
                    reason, bridge_memory_id, time.time(),
                ),
            )
            return int(cur.lastrowid)

    def record_recipient_disposition(self, transfer_event_id: int, disposition: str,
                                     *, note_memory_id: int | None = None) -> None:
        if disposition not in ("accepted", "amended", "partially_accepted",
                               "set_aside", "not_reviewed"):
            raise ValueError(f"bad recipient_disposition: {disposition}")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE transfer_events SET recipient_disposition=?, recipient_note_memory_id=?"
                " WHERE id=?",
                (disposition, note_memory_id, transfer_event_id),
            )
            if cur.rowcount == 0:
                raise TransitionError(f"transfer event {transfer_event_id} does not exist")
