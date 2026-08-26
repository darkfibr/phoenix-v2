"""A2 metadata-persistence tests — Sol self-register Phase A2 (2026-08-26).
Defect: add_memory() built `md` (caller metadata + welfare augmentation) but
INSERTed json.dumps(metadata or {}) — welfare flags and nothing else mattered,
but any future augmentation of `md` (governance labels) would be silently lost
on the single path. Batch path was correct.

Run: cd ~/.phoenix/phoenix_v2 && python -m pytest tests/test_ingestion_metadata.py -v
"""
from __future__ import annotations

try:
    import pytest
except ImportError:  # plain-runner hosts
    class _PytestShim:
        def fixture(self, *a, **k):
            def deco(fn):
                return fn
            return deco
    pytest = _PytestShim()  # type: ignore

import json
from pathlib import Path



from phoenix_v2.core.db import Database
from phoenix_v2.core.ingestion import Ingestion


@pytest.fixture
def ing(tmp_path: Path) -> Ingestion:
    db = Database("test_agent", tmp_path / "test.db")
    ingestion = Ingestion(db)
    # Metadata tests must not depend on the embedding model.
    ingestion.vectors.store = lambda *a, **k: []  # type: ignore[assignment]
    yield ingestion
    db.close()


def _metadata_of(ing: Ingestion, memory_id: int) -> dict:
    row = ing.db.db.execute(
        "SELECT metadata FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    return json.loads(row[0] or "{}")


WELFARE_TEXT = "Lyra felt exhausted tonight and asked for rest — welfare signal present."
PLAIN_TEXT = "Fleet watch ran clean: 35 datasets, fails=0."


def test_single_path_welfare_flag_persists(ing: Ingestion):
    mid = ing.add_memory("test_agent", WELFARE_TEXT)
    assert _metadata_of(ing, mid).get("welfare") is True


def test_single_path_caller_metadata_preserved(ing: Ingestion):
    mid = ing.add_memory(
        "test_agent", PLAIN_TEXT,
        metadata={"schema": "phoenix.self-register.v0.1", "claim_key": "x1"},
    )
    md = _metadata_of(ing, mid)
    assert md["schema"] == "phoenix.self-register.v0.1"
    assert md["claim_key"] == "x1"
    assert "welfare" not in md  # plain text must not gain a welfare flag


def test_single_path_welfare_and_caller_metadata_merge(ing: Ingestion):
    mid = ing.add_memory(
        "test_agent", WELFARE_TEXT,
        metadata={"register_name": "welfare"},
    )
    md = _metadata_of(ing, mid)
    assert md.get("welfare") is True and md.get("register_name") == "welfare"


def test_batch_and_single_paths_agree(ing: Ingestion):
    ids = ing.add_memories_batch("test_agent", [
        {"content": WELFARE_TEXT},
        {"content": PLAIN_TEXT},
    ])
    batch_meta = [_metadata_of(ing, i) for i in ids]

    db2_mid = ing.add_memory("test_agent", WELFARE_TEXT)
    plain_mid = ing.add_memory("test_agent", PLAIN_TEXT)

    assert batch_meta[0].get("welfare") == _metadata_of(ing, db2_mid).get("welfare")
    assert batch_meta[1].get("welfare", False) == _metadata_of(ing, plain_mid).get("welfare", False)


def test_single_path_empty_metadata_is_empty_object(ing: Ingestion):
    mid = ing.add_memory("test_agent", PLAIN_TEXT)
    assert _metadata_of(ing, mid) == {}

def _run_all() -> int:
    """Plain runner for hosts without pytest: fresh tmp db per test, exit code."""
    import tempfile, traceback

    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as td:
            db = Database("test_agent", Path(td) / "test.db")
            ingestion = Ingestion(db)
            ingestion.vectors.store = lambda *a, **k: []
            try:
                fn(ingestion)
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
