import json
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.models import RepoDoc, RepoDocChunk
from knowledge.repo_docs import (
    ManifestEntry,
    apply_deletions,
    load_manifest,
    plan_reconcile,
    upsert_doc,
)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def test_load_manifest_parses_ndjson(tmp_path: Path):
    p = tmp_path / "m.ndjson"
    p.write_text(
        "\n".join(
            json.dumps(o, sort_keys=True)
            for o in [
                {"path": "docs/a.md", "sha256": "h1", "title": "A", "content": "# A"},
                {
                    "path": "CLAUDE.md",
                    "sha256": "h2",
                    "title": "Root",
                    "content": "# Root",
                },
            ]
        )
        + "\n"
    )
    entries = load_manifest(p)
    assert [e.path for e in entries] == ["docs/a.md", "CLAUDE.md"]
    assert entries[0] == ManifestEntry(
        path="docs/a.md", sha256="h1", title="A", content="# A"
    )


def test_load_manifest_missing_file_returns_empty(tmp_path: Path):
    assert load_manifest(tmp_path / "nope.ndjson") == []


def _entry(path, content, sha):
    return ManifestEntry(path=path, sha256=sha, title=path, content=content)


def test_plan_reconcile_classifies_new_changed_deleted(session):
    session.add(RepoDoc(path="keep.md", content_hash="same", title="keep"))
    session.add(RepoDoc(path="gone.md", content_hash="x", title="gone"))
    session.commit()

    entries = [
        _entry("keep.md", "# keep", "same"),  # unchanged -> skip
        _entry("new.md", "# new\n\nbody", "n1"),  # new -> index
    ]
    plan = plan_reconcile(session, entries)

    assert {e.path for e, _ in plan.to_upsert} == {"new.md"}
    assert plan.to_delete == ["gone.md"]
    _, chunks = plan.to_upsert[0]
    assert chunks and chunks[0]["text"]


def test_plan_reconcile_detects_changed_hash(session):
    session.add(RepoDoc(path="c.md", content_hash="old", title="c"))
    session.commit()
    plan = plan_reconcile(session, [_entry("c.md", "# c\n\nnew text", "new")])
    assert {e.path for e, _ in plan.to_upsert} == {"c.md"}


def test_upsert_doc_inserts(session):
    entry = _entry("docs/x.md", "# X\n\nalpha", "h")
    chunks = [{"index": 0, "section_header": "# X", "text": "alpha"}]

    upsert_doc(session, entry, chunks, [[0.2] * 1024])

    doc = session.query(RepoDoc).filter_by(path="docs/x.md").one()
    assert doc.content_hash == "h"
    rows = session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).all()
    assert len(rows) == 1 and len(rows[0].embedding) == 1024


def test_upsert_doc_replaces_chunks_on_change(session):
    e1 = _entry("y.md", "# Y\n\nv1", "h1")
    upsert_doc(
        session, e1, [{"index": 0, "section_header": "", "text": "v1"}], [[0.1] * 1024]
    )
    e2 = _entry("y.md", "# Y\n\nv2 longer", "h2")
    upsert_doc(
        session,
        e2,
        [{"index": 0, "section_header": "", "text": "v2 longer"}],
        [[0.9] * 1024],
    )
    doc = session.query(RepoDoc).filter_by(path="y.md").one()
    rows = session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).all()
    assert doc.content_hash == "h2"
    assert len(rows) == 1 and rows[0].chunk_text == "v2 longer"


def test_apply_deletions_removes_doc_and_chunks(session):
    e = _entry("z.md", "# Z\n\nzz", "h")
    upsert_doc(
        session, e, [{"index": 0, "section_header": "", "text": "zz"}], [[0.3] * 1024]
    )
    doc_id = session.query(RepoDoc).filter_by(path="z.md").one().id

    assert apply_deletions(session, ["z.md"]) == 1

    assert session.query(RepoDoc).filter_by(path="z.md").first() is None
    assert session.query(RepoDocChunk).filter_by(repo_doc_fk=doc_id).count() == 0


def test_reconcile_resumes_from_committed_docs(session):
    # Per-doc commit makes a backfill resumable: simulate a run that committed
    # doc A and was then interrupted (pod rollout) before doc B. The next run's
    # plan must skip the already-committed A and only do B.
    upsert_doc(
        session,
        _entry("a.md", "# A\n\nalpha", "ha"),
        [{"index": 0, "section_header": "", "text": "alpha"}],
        [[0.4] * 1024],
    )

    plan = plan_reconcile(
        session,
        [
            _entry("a.md", "# A\n\nalpha", "ha"),  # unchanged -> already committed
            _entry("b.md", "# B\n\nbeta", "hb"),  # new -> still needs work
        ],
    )

    assert {e.path for e, _ in plan.to_upsert} == {"b.md"}
    assert plan.to_delete == []
