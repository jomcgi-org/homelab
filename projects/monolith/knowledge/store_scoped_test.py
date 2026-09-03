"""Tests for scoped assertions, disputes, and provenance in the store."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.indexing import index_note_from_raw
from knowledge.models import AtomRawProvenance, Dispute, Note, RawInput
from knowledge.store import (
    KnowledgeStore,
    open_dispute_note_ids,
    provenance_for_notes,
)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.db'}")
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


def _upsert(store: KnowledgeStore, metadata: ParsedFrontmatter) -> None:
    store.upsert_note(
        note_id="scoped",
        path="scoped.md",
        content_hash="hash",
        title="Scoped",
        metadata=metadata,
        chunks=[{"index": 0, "section_header": "", "text": "body text"}],
        vectors=[[0.0] * 1024],
        links=[],
        content="body text",
    )


def test_upsert_sets_scoped_columns(session):
    store = KnowledgeStore(session)
    valid_from = datetime(2026, 9, 1, tzinfo=timezone.utc)
    valid_until = datetime(2026, 10, 1, tzinfo=timezone.utc)
    observed_at = datetime(2026, 9, 2, tzinfo=timezone.utc)

    _upsert(
        store,
        ParsedFrontmatter(
            scope="org:factory",
            verification_state="verified",
            confidence=0.9,
            valid_from=valid_from,
            valid_until=valid_until,
            observed_at=observed_at,
        ),
    )

    note = session.exec(select(Note).where(Note.note_id == "scoped")).one()
    assert note.scope == "org:factory"
    assert note.verification_state == "verified"
    assert note.confidence == 0.9
    assert note.valid_from.replace(tzinfo=timezone.utc) == valid_from
    assert note.valid_until.replace(tzinfo=timezone.utc) == valid_until
    assert note.observed_at.replace(tzinfo=timezone.utc) == observed_at


@pytest.mark.asyncio
async def test_reindex_without_scoped_keys_preserves_existing_values(session):
    store = KnowledgeStore(session)
    observed_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    _upsert(
        store,
        ParsedFrontmatter(
            scope="personal:alice",
            verification_state="verified",
            confidence=0.8,
            observed_at=observed_at,
        ),
    )

    embedder = MagicMock()

    async def embed_batch(texts):
        return [[0.0] * 1024 for _ in texts]

    embedder.embed_batch = embed_batch
    await index_note_from_raw(
        store,
        embedder,
        note_id="scoped",
        rel_path="scoped.md",
        raw="---\nid: scoped\ntitle: Scoped\n---\n\nnew body\n",
    )

    note = session.exec(select(Note).where(Note.note_id == "scoped")).one()
    assert note.scope == "personal:alice"
    assert note.verification_state == "verified"
    assert note.confidence == 0.8
    assert note.observed_at.replace(tzinfo=timezone.utc) == observed_at


def test_disputes_and_provenance_are_batched_by_note(session):
    note = Note(
        note_id="scoped",
        path="scoped.md",
        title="Scoped",
        content_hash="hash",
    )
    raw = RawInput(
        raw_id="raw-1",
        path="raws/raw-1.md",
        source="collector",
        content_hash="raw-1",
    )
    session.add(note)
    session.add(raw)
    session.commit()
    session.refresh(note)
    session.refresh(raw)
    session.add(
        AtomRawProvenance(
            atom_fk=note.id,
            raw_fk=raw.id,
            gardener_version="test-version",
        )
    )
    session.add(Dispute(note_id="scoped", reason="contradictory evidence"))
    session.add(Dispute(note_id="closed", reason="resolved", state="confirmed"))
    session.commit()

    assert open_dispute_note_ids(session, ["scoped", "closed", "missing"]) == {"scoped"}
    assert provenance_for_notes(session, [note.id]) == {
        note.id: [
            {
                "raw_id": "raw-1",
                "source": "collector",
                "gardener_version": "test-version",
            }
        ]
    }


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self


@pytest.mark.parametrize(
    ("open_dispute_ids", "expected_disputed"),
    [([], False), (["scoped"], True)],
)
def test_search_results_include_scoped_fields_dispute_and_provenance(
    open_dispute_ids, expected_disputed
):
    valid_from = datetime(2026, 9, 1, tzinfo=timezone.utc)
    note_row = SimpleNamespace(
        id=11,
        note_id="scoped",
        title="Scoped",
        path="scoped.md",
        type="fact",
        tags=["test"],
        score=0.9,
        scope="repo:owner/repo",
        verification_state="verified",
        confidence=0.7,
        valid_from=valid_from,
        valid_until=None,
        observed_at=None,
    )
    chunk_row = SimpleNamespace(
        note_fk=11, section_header="## Evidence", chunk_text="supported claim"
    )
    provenance_row = SimpleNamespace(
        atom_fk=11,
        raw_id="raw-1",
        source="collector",
        gardener_version="v1",
    )
    session = MagicMock()
    session.execute.side_effect = [
        _Rows([note_row]),
        _Rows([chunk_row]),
        _Rows([]),
        _Rows(open_dispute_ids),
        _Rows([provenance_row]),
    ]

    results = KnowledgeStore(session).search_notes_with_context([0.0] * 1024)

    assert session.execute.call_count == 5
    assert results[0]["scope"] == "repo:owner/repo"
    assert results[0]["verification_state"] == "verified"
    assert results[0]["confidence"] == 0.7
    assert results[0]["valid_from"] == "2026-09-01T00:00:00+00:00"
    assert results[0]["valid_until"] is None
    assert results[0]["observed_at"] is None
    assert results[0]["disputed"] is expected_disputed
    assert results[0]["provenance"] == [
        {"raw_id": "raw-1", "source": "collector", "gardener_version": "v1"}
    ]
