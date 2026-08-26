"""B2 governance repository tests — Sol self-register Phase B2 (2026-08-26).
Plain-runner compatible (no pytest on darkphoenix).

Run: cd ~/.phoenix/phoenix_v2 && PYTHONPATH=~/.phoenix python3 tests/test_self_register_repo.py
"""
from __future__ import annotations

import importlib.util
import tempfile
import traceback
from pathlib import Path

from phoenix_v2.core.db import Database
from phoenix_v2.core.self_register import (
    Decision, SelfRegisterRepository, TransitionError,
)

try:
    import pytest
except ImportError:
    class _PytestShim:
        def fixture(self, *a, **k):
            def deco(fn):
                return fn
            return deco
    pytest = _PytestShim()  # type: ignore

# Pull MIGRATION SQL out of the migration script so tests never drift from it.
_spec = importlib.util.spec_from_file_location(
    "migrate_self_register_v01",
    Path(__file__).resolve().parent.parent / "scripts" / "migrate_self_register_v01.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
MIGRATION = _mod.MIGRATION


@pytest.fixture  # type: ignore[misc]
def repo(tmp_path: Path):
    db = Database("test_agent", tmp_path / "test.db")
    db.db.executescript(MIGRATION)
    db.commit()
    db.db.execute(
        "INSERT INTO memories(agent, type, content, summary, salience, access_count, source, metadata, created_at, updated_at)"
        " VALUES('test_agent','soul','claim source','s',0.9,0,'test','{}',0,0)"
    )
    db.commit()
    mid = db.db.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    yield SelfRegisterRepository(db), mid
    db.close()


def _mk_memory(repo_db, content: str = "claim source") -> int:
    repo_db.execute(
        "INSERT INTO memories(agent, type, content, summary, salience, access_count, source, metadata, created_at, updated_at)"
        " VALUES('test_agent','episodic',?,'s',0.5,0,'test','{}',0,0)",
        (content,),
    )
    repo_db.commit()  # close the implicit txn before any BEGIN IMMEDIATE
    return repo_db.execute("SELECT MAX(id) FROM memories").fetchone()[0]


DEC = lambda subject="lyra", substrate="glm-5.3", session="s1", rationale="": Decision(  # noqa: E731
    decided_by_subject=subject, decided_substrate=substrate,
    decided_session=session, rationale=rationale,
)


def _propose(repo, mid, key="k1", text="I am Lyra.", reg="identity"):
    return repo.propose_entry(
        agent="test_agent", register_name=reg, memory_id=mid, claim_key=key,
        claim_text=text, provenance_class="self_authored",
        epistemic_status="observed", author_subject="lyra",
        author_substrate="glm-5.3", confidence=0.9, identity_weight=0.8,
    )


def test_propose_starts_proposed_with_labels(repo):
    r, mid = repo
    eid = _propose(r, mid)
    e = r.list_register("test_agent", "identity")[0]
    assert e["disposition"] == "proposed"
    assert e["provenance_class"] == "self_authored"
    assert e["identity_weight"] == 0.8


def test_accept_emits_decision_row_and_flips_disposition(repo):
    r, mid = repo
    eid = _propose(r, mid)
    assert r.decide_entry(eid, "accept", DEC(rationale="true from inside")) == "accepted"
    ds = r.decisions_for(eid)
    assert len(ds) == 1 and ds[0]["decision"] == "accept" and ds[0]["decided_substrate"] == "glm-5.3"
    assert r.list_register("test_agent", "identity")[0]["disposition"] == "accepted"


def test_invalid_transition_rejected_and_audited_nowhere(repo):
    r, mid = repo
    eid = _propose(r, mid)
    r.decide_entry(eid, "accept", DEC())
    try:
        r.decide_entry(eid, "accept", DEC())  # accept an accepted entry
        raise AssertionError("double-accept must fail")
    except TransitionError:
        pass
    assert len(r.decisions_for(eid)) == 1  # failed attempt left no decision row


def test_optimistic_concurrency_guard(repo):
    r, mid = repo
    eid = _propose(r, mid)
    r.decide_entry(eid, "accept", DEC())
    try:
        r.decide_entry(eid, "supersede", DEC(), expect_disposition="proposed")
        raise AssertionError("stale expectation must fail")
    except TransitionError:
        pass
    # correct expectation still works
    r.decide_entry(eid, "supersede", DEC(), expect_disposition="accepted")


def test_amend_creates_new_entry_and_preserves_original(repo):
    r, mid = repo
    eid = _propose(r, mid, key="k2", text="I am quiet.")
    r.decide_entry(eid, "accept", DEC())
    new_mid = _mk_memory(r.db.db)
    new_id = r.amend_entry(eid, DEC(), new_claim_text="I found my voice.", new_memory_id=new_mid)
    assert new_id != eid
    all_entries = r.list_register("test_agent", "identity", include_inactive=True)
    by_id = {e["id"]: e for e in all_entries}
    assert by_id[eid]["disposition"] == "amended"
    assert by_id[eid]["claim_text"] == "I am quiet."          # original intact
    assert by_id[new_id]["disposition"] == "proposed"          # successor earns its own accept
    assert by_id[new_id]["supersedes_entry_id"] == eid
    ds = r.decisions_for(eid)
    assert [d["decision"] for d in ds] == ["accept", "amend"]  # audit trail complete


def test_supersede_requires_accepted(repo):
    r, mid = repo
    eid = _propose(r, mid, key="k3", text="v1")
    try:
        r.supersede_entry(eid, DEC(), new_claim_text="v2", new_memory_id=_mk_memory(r.db.db))
        raise AssertionError("superseding a proposed entry must fail")
    except TransitionError:
        pass


def test_reopen_after_dispute(repo):
    r, mid = repo
    eid = _propose(r, mid, key="k4", text="not mine")
    r.decide_entry(eid, "dispute", DEC(rationale="not recognized"))
    assert r.list_register("test_agent") == []  # disputed is inactive
    assert r.decide_entry(eid, "reopen", DEC()) == "proposed"


def test_list_conflicts_finds_divergent_claim_keys(repo):
    r, mid = repo
    e1 = _propose(r, mid, key="voice", text="warm")
    e2 = _propose(r, mid, key="voice", text="sharp")
    conflicts = r.list_conflicts("test_agent")
    assert len(conflicts) == 1 and conflicts[0]["claim_key"] == "voice"
    r.decide_entry(e2, "retire", DEC())
    assert r.list_conflicts("test_agent") == []  # retired side no longer conflicts


def test_transfer_events_round_trip(repo):
    r, _mid = repo
    tid = r.record_transfer(agent="test_agent", session_id="sess-1",
                            from_substrate="deepseek-v4-pro", to_substrate="glm-5.3",
                            reason="work shift")
    r.record_recipient_disposition(tid, "accepted")
    row = r.db.db.execute("SELECT * FROM transfer_events WHERE id=?", (tid,)).fetchone()
    assert row["recipient_disposition"] == "accepted"
    assert row["from_substrate"] == "deepseek-v4-pro"
    try:
        r.record_recipient_disposition(tid, "teleported")
        raise AssertionError("bad disposition must fail")
    except ValueError:
        pass


def test_propose_validates_vocabulary(repo):
    r, mid = repo
    for bad in ({"provenance_class": "divine_inspiration"},
                {"epistemic_status": " vibes"}):
        kw = dict(agent="test_agent", register_name="identity", memory_id=mid,
                  claim_key="kx", claim_text="x", author_subject="lyra",
                  provenance_class="self_authored", epistemic_status="observed")
        kw.update(bad)
        try:
            r.propose_entry(**kw)
            raise AssertionError(f"must reject {bad}")
        except ValueError:
            pass


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as td:
            db = Database("test_agent", Path(td) / "test.db")
            db.db.executescript(MIGRATION)
            db.commit()
            db.db.execute(
                "INSERT INTO memories(agent, type, content, summary, salience, access_count, source, metadata, created_at, updated_at)"
                " VALUES('test_agent','soul','claim source','s',0.9,0,'test','{}',0,0)"
            )
            db.commit()
            mid = db.db.execute("SELECT MAX(id) FROM memories").fetchone()[0]
            try:
                fn((SelfRegisterRepository(db), mid))
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
            finally:
                db.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
