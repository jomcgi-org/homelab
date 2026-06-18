"""Unit tests for knowledge.models — NoteLink validation and Chunk._parse_embedding."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import UniqueConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.models import AtomRawProvenance, Chunk, Note, NoteLink, RawInput


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


class TestNoteLinkDiscriminatedUnion:
    """NoteLink enforces a discriminated-union invariant in __init__.

    kind='link' must have edge_type=None.
    kind='edge' must have a non-None edge_type.
    These are enforced manually because SQLModel table models skip pydantic
    validators in __init__ (see comment in models.py).
    """

    def test_link_with_edge_type_raises(self):
        """kind='link' with edge_type set must raise ValueError."""
        with pytest.raises(ValueError, match="kind='link' requires edge_type=None"):
            NoteLink(
                src_note_fk=1,
                target_id="B",
                kind="link",
                edge_type="refines",
            )

    def test_edge_without_edge_type_raises(self):
        """kind='edge' with edge_type=None must raise ValueError."""
        with pytest.raises(ValueError, match="kind='edge' requires"):
            NoteLink(
                src_note_fk=1,
                target_id="B",
                kind="edge",
                edge_type=None,
            )

    def test_valid_link_succeeds(self):
        """kind='link' with edge_type=None is accepted."""
        note_link = NoteLink(
            src_note_fk=1,
            target_id="B",
            kind="link",
            edge_type=None,
        )
        assert note_link.kind == "link"
        assert note_link.edge_type is None
        assert note_link.target_id == "B"

    def test_valid_edge_succeeds(self):
        """kind='edge' with a valid edge_type is accepted."""
        note_link = NoteLink(
            src_note_fk=1,
            target_id="parent",
            kind="edge",
            edge_type="refines",
        )
        assert note_link.kind == "edge"
        assert note_link.edge_type == "refines"
        assert note_link.target_id == "parent"

    def test_link_with_all_valid_edge_types_raises(self):
        """Every known edge_type triggers ValueError when kind='link'."""
        edge_types = [
            "refines",
            "generalizes",
            "related",
            "contradicts",
            "derives_from",
            "supersedes",
        ]
        for et in edge_types:
            with pytest.raises(ValueError, match="kind='link' requires edge_type=None"):
                NoteLink(
                    src_note_fk=1,
                    target_id="x",
                    kind="link",
                    edge_type=et,
                )

    def test_all_valid_edge_types_accepted_with_edge_kind(self):
        """Every known edge_type is accepted when kind='edge'."""
        edge_types = [
            "refines",
            "generalizes",
            "related",
            "contradicts",
            "derives_from",
            "supersedes",
        ]
        for et in edge_types:
            link = NoteLink(
                src_note_fk=1,
                target_id="x",
                kind="edge",
                edge_type=et,
            )
            assert link.edge_type == et


class TestChunkParseEmbedding:
    """Chunk._parse_embedding parses JSON strings and passes lists through unchanged."""

    def test_json_string_is_parsed_to_list(self):
        """A JSON-encoded string is deserialised to a Python list."""
        raw = json.dumps([0.1, 0.2, 0.3])
        result = Chunk._parse_embedding(raw)
        assert result == [0.1, 0.2, 0.3]

    def test_list_input_passes_through_unchanged(self):
        """A list value is returned as-is without modification."""
        data = [0.5] * 8
        result = Chunk._parse_embedding(data)
        assert result is data

    def test_json_string_with_full_dimension_vector(self):
        """A 1024-element JSON embedding string is parsed correctly."""
        vec = [float(i) / 1024 for i in range(1024)]
        raw = json.dumps(vec)
        result = Chunk._parse_embedding(raw)
        assert result == vec
        assert len(result) == 1024

    def test_non_string_non_list_passes_through(self):
        """Non-string, non-list values (e.g. None) pass through for pydantic to handle."""
        result = Chunk._parse_embedding(None)
        assert result is None


def test_raw_input_roundtrip(session):
    ri = RawInput(
        raw_id="abc123",
        path="_raw/2026/04/09/abc1-my-note.md",
        source="vault-drop",
        original_path="inbox/my-note.md",
        content="# Hello\n\nBody.",
        content_hash="abc123",
    )
    session.add(ri)
    session.commit()

    loaded = session.get(RawInput, ri.id)
    assert loaded is not None
    assert loaded.raw_id == "abc123"
    assert loaded.source == "vault-drop"
    assert loaded.extra == {}


def test_atom_raw_provenance_roundtrip(session):
    note = Note(
        note_id="hello-world",
        path="_processed/atoms/hello-world.md",
        title="Hello World",
        content_hash="def456",
        type="atom",
    )
    raw = RawInput(
        raw_id="abc123",
        path="_raw/2026/04/09/abc1-my-note.md",
        source="vault-drop",
        content="Body.",
        content_hash="abc123",
    )
    session.add_all([note, raw])
    session.commit()

    prov = AtomRawProvenance(
        atom_fk=note.id,
        raw_fk=raw.id,
        gardener_version="claude-sonnet-4-6@v1",
    )
    session.add(prov)
    session.commit()

    loaded = session.get(AtomRawProvenance, prov.id)
    assert loaded is not None
    assert loaded.atom_fk == note.id
    assert loaded.raw_fk == raw.id


def test_repo_doc_and_chunk_roundtrip(session):
    from knowledge.models import RepoDoc, RepoDocChunk

    doc = RepoDoc(path="docs/security.md", content_hash="abc123", title="Security")
    session.add(doc)
    session.commit()
    session.refresh(doc)

    chunk = RepoDocChunk(
        repo_doc_fk=doc.id,
        chunk_index=0,
        section_header="# Security",
        chunk_text="never hardcode secrets",
        embedding=[0.1] * 1024,
    )
    session.add(chunk)
    session.commit()
    session.refresh(chunk)

    assert chunk.repo_doc_fk == doc.id
    assert len(chunk.embedding) == 1024
    assert doc.path == "docs/security.md"


def test_atom_raw_provenance_rejects_both_null():
    with pytest.raises(ValueError, match="at least one of atom_fk or raw_fk"):
        AtomRawProvenance(
            atom_fk=None,
            raw_fk=None,
            gardener_version="claude-sonnet-4-6@v1",
        )


class TestDefaultFactoryTimestamps:
    """Models with default_factory timestamps auto-populate UTC datetimes on construction.

    Verifies commit 1a7de3b0: created_at/indexed_at use default_factory so
    explicit NULL is never sent to Postgres, bypassing the DEFAULT now() column.
    """

    def test_note_created_at_auto_populated(self):
        """Note.created_at is a UTC datetime when no explicit value is given."""
        note = Note(
            note_id="auto-ts-note",
            path="_processed/atoms/auto-ts-note.md",
            title="Auto TS",
            content_hash="abc",
            type="atom",
        )
        assert isinstance(note.created_at, datetime)
        assert note.created_at.tzinfo == timezone.utc

    def test_note_indexed_at_auto_populated(self):
        """Note.indexed_at is a UTC datetime when no explicit value is given."""
        note = Note(
            note_id="auto-ts-note2",
            path="_processed/atoms/auto-ts-note2.md",
            title="Auto TS 2",
            content_hash="def",
            type="atom",
        )
        assert isinstance(note.indexed_at, datetime)
        assert note.indexed_at.tzinfo == timezone.utc

    def test_note_timestamps_are_independent_instances(self):
        """Each Note construction produces independent timestamp values (default_factory, not default)."""
        n1 = Note(
            note_id="ts-a",
            path="_processed/atoms/ts-a.md",
            title="A",
            content_hash="h1",
            type="atom",
        )
        n2 = Note(
            note_id="ts-b",
            path="_processed/atoms/ts-b.md",
            title="B",
            content_hash="h2",
            type="atom",
        )
        # default_factory=lambda: datetime.now(timezone.utc) is called fresh
        # for each construction, so values differ.  Using != (not `is not`)
        # catches the classic bug where default= would share a single evaluated
        # object — Pydantic copies on validation so identity always differs, but
        # value equality correctly distinguishes the two cases.
        assert n1.created_at != n2.created_at
        assert n1.indexed_at != n2.indexed_at

    def test_raw_input_created_at_auto_populated(self):
        """RawInput.created_at is a UTC datetime when no explicit value is given."""
        ri = RawInput(
            raw_id="ri-auto-ts",
            path="_raw/2026/04/10/ri-auto-ts.md",
            source="vault-drop",
            content="Body.",
            content_hash="ri-auto-ts",
        )
        assert isinstance(ri.created_at, datetime)
        assert ri.created_at.tzinfo == timezone.utc

    def test_atom_raw_provenance_created_at_auto_populated(self):
        """AtomRawProvenance.created_at is a UTC datetime when no explicit value is given."""
        prov = AtomRawProvenance(
            raw_fk=1,
            gardener_version="v1",
        )
        assert isinstance(prov.created_at, datetime)
        assert prov.created_at.tzinfo == timezone.utc

    def test_explicit_created_at_is_respected(self):
        """Passing an explicit created_at overrides the default_factory."""
        explicit_ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        ri = RawInput(
            raw_id="ri-explicit-ts",
            path="_raw/2026/04/10/ri-explicit-ts.md",
            source="vault-drop",
            content="Body.",
            content_hash="ri-explicit-ts",
            created_at=explicit_ts,
        )
        assert ri.created_at == explicit_ts


def test_note_model_has_optional_layout_columns():
    note = Note(note_id="n1", path="x.md", title="X", content_hash="h")
    assert note.layout_x is None
    assert note.layout_y is None
    note.layout_x = 0.1
    note.layout_y = -0.2
    assert note.layout_x == 0.1
    assert note.layout_y == -0.2


def test_note_visibility_accepts_public_private_or_none():
    Note(note_id="a", path="a.md", title="A", content_hash="h", visibility="public")
    Note(note_id="b", path="b.md", title="B", content_hash="h", visibility="private")
    Note(note_id="c", path="c.md", title="C", content_hash="h", visibility=None)


def test_note_visibility_rejected_at_db_constraint(session):
    """The Literal is type-only; the SQL CHECK is what enforces at write time.

    Insert a bogus value via raw SQL and assert the constraint fires. The
    CheckConstraint is declared on the Note model so SQLModel.metadata.create_all
    emits it for SQLite too — keeping this test honest in CI without needing a
    real Postgres instance.
    """
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO notes "
                "(note_id, path, title, content_hash, visibility) "
                "VALUES (:id, :p, :t, :h, :v)"
            ),
            {"id": "x", "p": "x.md", "t": "X", "h": "h", "v": "yellow"},
        )
        session.flush()


def test_gap_has_note_id_unique_constraint():
    """note_id is the projection-layer identity — must be UNIQUE in the schema."""
    from knowledge.models import Gap

    constraints = [c for c in Gap.__table_args__ if isinstance(c, UniqueConstraint)]
    column_sets = [tuple(c.columns.keys()) for c in constraints]
    assert ("note_id",) in column_sets, (
        f"Gap must have UniqueConstraint on note_id; got {column_sets}"
    )
