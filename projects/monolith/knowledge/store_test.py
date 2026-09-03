"""Tests for KnowledgeStore."""

import os

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.links import Link
from knowledge.models import Chunk, Note, NoteLink
from knowledge.store import KnowledgeStore

_PG_URL = os.environ.get("TEST_POSTGRES_URL")


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
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture(name="pg_session")
def pg_session_fixture():
    """Session backed by a real Postgres database with pgvector.

    Set TEST_POSTGRES_URL to a connection string like
    ``postgresql://user:pass@localhost/testdb`` to enable.
    The database must have the ``vector`` extension and ``knowledge``
    schema created.
    """
    if _PG_URL is None:
        pytest.skip("TEST_POSTGRES_URL not set — skipping Postgres tests")
    engine = create_engine(_PG_URL)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    # Clean up all rows so tests are hermetic.
    with Session(engine) as session:
        session.execute(Chunk.__table__.delete())
        session.execute(NoteLink.__table__.delete())
        session.execute(Note.__table__.delete())
        session.commit()


@pytest.fixture
def store(session):
    return KnowledgeStore(session=session)


def _meta(**kw):
    return ParsedFrontmatter(**kw)


def _chunks(n):
    return [
        {"index": i, "section_header": f"H{i}", "text": f"chunk {i}"} for i in range(n)
    ]


def _vecs(n):
    return [[float(i)] * 1024 for i in range(n)]


def _upsert(
    store,
    *,
    note_id="a-id",
    path="a.md",
    content_hash="h1",
    title="A",
    metadata=None,
    n_chunks=1,
    links=None,
    content=None,
):
    metadata = metadata or _meta(title=title)
    store.upsert_note(
        note_id=note_id,
        path=path,
        content_hash=content_hash,
        title=title,
        metadata=metadata,
        chunks=_chunks(n_chunks),
        vectors=_vecs(n_chunks),
        links=links or [],
        content=content,
    )


class TestGetIndexed:
    def test_empty(self, store):
        assert store.get_indexed() == {}

    def test_returns_path_to_hash_map(self, store):
        _upsert(store, path="a.md", content_hash="h1")
        assert store.get_indexed() == {"a.md": "h1"}


class TestUpsertNote:
    def test_inserts_note_chunks_and_links(self, store, session):
        _upsert(
            store,
            metadata=_meta(title="A", tags=["ml"]),
            n_chunks=3,
            links=[Link(target="B", display=None)],
        )
        notes = list(session.scalars(select(Note)))
        assert len(notes) == 1
        assert notes[0].note_id == "a-id"
        assert notes[0].tags == ["ml"]
        chunks = list(session.scalars(select(Chunk)))
        assert len(chunks) == 3
        assert {c.chunk_index for c in chunks} == {0, 1, 2}
        links = list(session.scalars(select(NoteLink)))
        assert len(links) == 1
        assert links[0].target_id == "B"
        assert links[0].kind == "link"
        assert links[0].edge_type is None

    def test_re_upsert_replaces_chunk_count(self, store, session):
        _upsert(store, content_hash="h1", n_chunks=5)
        _upsert(store, content_hash="h2", n_chunks=2)
        chunks = list(session.scalars(select(Chunk)))
        assert len(chunks) == 2
        notes = list(session.scalars(select(Note)))
        assert len(notes) == 1
        assert notes[0].content_hash == "h2"

    def test_upsert_with_changed_path_replaces_old_row(self, store, session):
        # Simulates the raw-bucketing migration: same note_id but path moves
        # from e.g. "notes/foo.md" → "_processed/foo.md". The old row must be
        # deleted by note_id fallback so the INSERT doesn't hit the unique
        # constraint on note_id.
        _upsert(store, note_id="migrated", path="notes/foo.md", content_hash="h1")
        _upsert(store, note_id="migrated", path="_processed/foo.md", content_hash="h2")
        notes = list(session.scalars(select(Note)))
        assert len(notes) == 1
        assert notes[0].path == "_processed/foo.md"
        assert notes[0].content_hash == "h2"

    def test_edges_emit_typed_link_rows(self, store, session):
        _upsert(
            store,
            metadata=_meta(
                title="A",
                edges={
                    "refines": ["parent"],
                    "related": ["sib1", "sib2"],
                },
            ),
        )
        rows = list(session.scalars(select(NoteLink).order_by(NoteLink.id)))
        kinds = {r.kind for r in rows}
        assert kinds == {"edge"}
        edge_pairs = {(r.edge_type, r.target_id) for r in rows}
        assert edge_pairs == {
            ("refines", "parent"),
            ("related", "sib1"),
            ("related", "sib2"),
        }

    def test_edges_and_body_links_coexist(self, store, session):
        _upsert(
            store,
            metadata=_meta(title="A", edges={"refines": ["p"]}),
            links=[Link(target="B", display="the b")],
        )
        rows = list(session.scalars(select(NoteLink)))
        by_kind: dict[str, list[NoteLink]] = {"edge": [], "link": []}
        for r in rows:
            by_kind[r.kind].append(r)
        assert len(by_kind["edge"]) == 1
        assert by_kind["edge"][0].edge_type == "refines"
        assert len(by_kind["link"]) == 1
        assert by_kind["link"][0].edge_type is None
        assert by_kind["link"][0].target_title == "the b"


class TestUpsertVisibility:
    """``visibility`` from frontmatter must be persisted to the DB column.

    The reconciler treats disk frontmatter as canonical, so every upsert
    must rewrite this column — including back to NULL when the key is
    removed.
    """

    def test_upsert_writes_visibility_public(self, store, session):
        _upsert(
            store,
            note_id="vis-pub",
            path="vp.md",
            metadata=_meta(title="VP", visibility="public"),
        )
        note = session.execute(
            select(Note).where(Note.note_id == "vis-pub")
        ).scalar_one()
        assert note.visibility == "public"

    def test_upsert_writes_visibility_private(self, store, session):
        _upsert(
            store,
            note_id="vis-priv",
            path="vpr.md",
            metadata=_meta(title="VPr", visibility="private"),
        )
        note = session.execute(
            select(Note).where(Note.note_id == "vis-priv")
        ).scalar_one()
        assert note.visibility == "private"

    def test_upsert_null_visibility_writes_null(self, store, session):
        _upsert(
            store,
            note_id="vis-null",
            path="vn.md",
            metadata=_meta(title="VN", visibility=None),
        )
        note = session.execute(
            select(Note).where(Note.note_id == "vis-null")
        ).scalar_one()
        assert note.visibility is None


class TestUpsertAtomicity:
    def test_upsert_replace_is_atomic_on_mid_insert_failure(self, store, session):
        _upsert(
            store,
            path="a.md",
            content_hash="h1",
            title="Original",
            metadata=_meta(title="Original"),
            n_chunks=1,
        )

        boom = {"fired": False}

        def _raise_once(_mapper, _connection, _target):
            if boom["fired"]:
                return
            boom["fired"] = True
            raise RuntimeError("simulated mid-insert failure")

        event.listen(Chunk, "after_insert", _raise_once)
        try:
            with pytest.raises(RuntimeError, match="simulated mid-insert failure"):
                _upsert(
                    store,
                    path="a.md",
                    content_hash="h2",
                    title="Replaced",
                    metadata=_meta(title="Replaced"),
                    n_chunks=2,
                )
            # Rollback any outer-transaction state left behind by the
            # failed upsert so we can query the preserved row.
            session.rollback()
        finally:
            event.remove(Chunk, "after_insert", _raise_once)

        notes = list(session.scalars(select(Note)))
        assert len(notes) == 1
        assert notes[0].title == "Original"
        assert notes[0].content_hash == "h1"


class TestNoteLinkValidation:
    def test_notelink_rejects_link_with_edge_type(self):
        with pytest.raises(ValueError, match="kind='link' requires edge_type=None"):
            NoteLink(
                src_note_fk=1,
                target_id="B",
                kind="link",
                edge_type="refines",
            )

    def test_notelink_rejects_edge_without_edge_type(self):
        with pytest.raises(ValueError, match="kind='edge' requires"):
            NoteLink(
                src_note_fk=1,
                target_id="B",
                kind="edge",
                edge_type=None,
            )


class TestDeleteNote:
    def test_cascade_removes_chunks_and_links(self, store, session):
        _upsert(
            store,
            n_chunks=2,
            links=[Link(target="B", display=None)],
        )
        store.delete_note("a.md")
        assert list(session.scalars(select(Note))) == []
        assert list(session.scalars(select(Chunk))) == []
        assert list(session.scalars(select(NoteLink))) == []


class TestSearchNotes:
    """search_notes requires pgvector cosine_distance (Postgres only)."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_session):
        self.session = pg_session
        self.store = KnowledgeStore(session=pg_session)

    def test_empty_returns_empty_list(self):
        result = self.store.search_notes(query_embedding=[0.0] * 1024)
        assert result == []

    def test_returns_matching_notes_ranked_by_score(self):
        # Insert two notes with distinct embeddings.
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        _upsert(self.store, note_id="n2", path="b.md", title="Beta", n_chunks=1)

        # Query with the same embedding as note n1's chunk (all 0.0s).
        results = self.store.search_notes(query_embedding=[0.0] * 1024)
        assert len(results) == 2
        # Both results should have the required keys.
        assert set(results[0].keys()) == {"note_id", "title", "path", "score"}
        # First result should be n1 (closer to all-zeros query).
        assert results[0]["note_id"] == "n1"

    def test_limit_restricts_result_count(self):
        for i in range(5):
            _upsert(
                self.store,
                note_id=f"n{i}",
                path=f"{i}.md",
                title=f"Note {i}",
                n_chunks=1,
            )
        results = self.store.search_notes(query_embedding=[0.0] * 1024, limit=2)
        assert len(results) == 2

    def test_exclude_ids_filters_notes(self):
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        _upsert(self.store, note_id="n2", path="b.md", title="Beta", n_chunks=1)

        results = self.store.search_notes(
            query_embedding=[0.0] * 1024, exclude_ids=["n1"]
        )
        note_ids = [r["note_id"] for r in results]
        assert "n1" not in note_ids
        assert "n2" in note_ids

    def test_score_is_between_zero_and_one(self):
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=1)
        results = self.store.search_notes(query_embedding=[1.0] * 1024)
        assert len(results) == 1
        assert 0.0 <= results[0]["score"] <= 1.0

    def test_best_chunk_score_used_for_multi_chunk_note(self):
        # A note with 2 chunks — the best chunk's score should be used.
        _upsert(self.store, note_id="n1", path="a.md", title="Alpha", n_chunks=2)
        results = self.store.search_notes(query_embedding=[0.0] * 1024)
        assert len(results) == 1
        # chunk_0 embedding is all 0.0s, chunk_1 is all 1.0s.
        # Query is all 0.0s, so chunk_0 is a perfect match (distance=0, score=1).
        assert results[0]["score"] == pytest.approx(1.0, abs=1e-6)


class TestSearchNotesWithContext:
    """search_notes_with_context requires pgvector cosine_distance (Postgres only)."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_session):
        self.session = pg_session
        self.store = KnowledgeStore(session=pg_session)

    def test_returns_type_tags_snippet_and_section(self):
        # Insert a note with a distinctive chunk under "## Architecture".
        distinctive = "transformer replaces recurrence entirely"
        self.store.upsert_note(
            note_id="n1",
            path="attention.md",
            content_hash="h1",
            title="Attention",
            metadata=_meta(
                title="Attention",
                type="paper",
                tags=["ml", "transformers"],
            ),
            chunks=[
                {
                    "index": 0,
                    "section_header": "## Architecture",
                    "text": distinctive,
                }
            ],
            vectors=[[0.0] * 1024],
            links=[],
        )

        results = self.store.search_notes_with_context(query_embedding=[0.0] * 1024)
        assert len(results) == 1
        row = results[0]
        assert set(row.keys()) == {
            "note_id",
            "title",
            "path",
            "type",
            "tags",
            "score",
            "section",
            "snippet",
            "edges",
            "scope",
            "verification_state",
            "confidence",
            "valid_from",
            "valid_until",
            "observed_at",
            "disputed",
            "provenance",
        }
        assert row["note_id"] == "n1"
        assert row["title"] == "Attention"
        assert row["path"] == "attention.md"
        assert row["type"] == "paper"
        assert row["tags"] == ["ml", "transformers"]
        assert row["section"] == "## Architecture"
        assert distinctive in row["snippet"]

    def test_best_chunk_snippet_used_for_multi_chunk_note(self):
        # One note with two chunks whose embeddings are far apart. The
        # DISTINCT ON query must pick the chunk whose embedding is closest
        # to the query — not the first one, not an arbitrary one.
        zeros_text = "zeros chunk about attention heads"
        ones_text = "ones chunk about positional encoding"
        self.store.upsert_note(
            note_id="n1",
            path="multi.md",
            content_hash="h1",
            title="Multi",
            metadata=_meta(title="Multi", type="paper", tags=["ml"]),
            chunks=[
                {
                    "index": 0,
                    "section_header": "## Zeros",
                    "text": zeros_text,
                },
                {
                    "index": 1,
                    "section_header": "## Ones",
                    "text": ones_text,
                },
            ],
            vectors=[[0.0] * 1024, [1.0] * 1024],
            links=[],
        )

        # Query matches the all-ones chunk exactly.
        results = self.store.search_notes_with_context(query_embedding=[1.0] * 1024)
        assert len(results) == 1
        row = results[0]
        assert row["section"] == "## Ones"
        assert ones_text in row["snippet"]
        assert zeros_text not in row["snippet"]

    def test_filters_by_type(self):
        _upsert(
            self.store,
            note_id="n1",
            path="a.md",
            title="Paper",
            metadata=_meta(title="Paper", type="paper"),
            n_chunks=1,
        )
        _upsert(
            self.store,
            note_id="n2",
            path="b.md",
            title="Journal",
            metadata=_meta(title="Journal", type="journal"),
            n_chunks=1,
        )
        results = self.store.search_notes_with_context(
            query_embedding=[0.0] * 1024, type_filter="paper"
        )
        assert len(results) == 1
        assert results[0]["note_id"] == "n1"
        assert results[0]["type"] == "paper"

    def test_empty_db_returns_empty_list(self):
        results = self.store.search_notes_with_context(query_embedding=[0.0] * 1024)
        assert results == []


class TestGetNoteById:
    def test_returns_note_metadata(self, store):
        _upsert(
            store,
            note_id="n1",
            path="folder/note.md",
            title="My Note",
            metadata=_meta(title="My Note", type="paper", tags=["x"]),
        )
        got = store.get_note_by_id("n1")
        assert got == {
            "note_id": "n1",
            "title": "My Note",
            "path": "folder/note.md",
            "type": "paper",
            "tags": ["x"],
            "content": None,
            "scope": None,
            "verification_state": "legacy",
            "confidence": None,
            "valid_from": None,
            "valid_until": None,
            "observed_at": None,
            "disputed": False,
            "provenance": [],
        }

    def test_returns_content_when_set(self, store):
        """ADR 006: get_note_by_id surfaces the authoritative body."""
        _upsert(
            store,
            note_id="n2",
            path="folder/n2.md",
            title="With Body",
            metadata=_meta(title="With Body"),
            content="# Heading\n\nThe authoritative body.",
        )
        got = store.get_note_by_id("n2")
        assert got is not None
        assert got["content"] == "# Heading\n\nThe authoritative body."

    def test_returns_none_when_missing(self, store):
        assert store.get_note_by_id("nope") is None


class TestGetNoteLinks:
    def test_edge_resolved_when_target_exists(self, store):
        """Typed edges include resolved_note_id when target note exists."""
        _upsert(
            store,
            note_id="src",
            path="src.md",
            title="Source",
            metadata=_meta(title="Source", edges={"refines": ["tgt"]}),
        )
        _upsert(store, note_id="tgt", path="tgt.md", title="Target")

        links = store.get_note_links("src")
        edges = [e for e in links if e["kind"] == "edge"]
        assert len(edges) == 1
        assert edges[0]["target_id"] == "tgt"
        assert edges[0]["resolved_note_id"] == "tgt"

    def test_edge_unresolved_when_target_missing(self, store):
        """Typed edges have resolved_note_id=None when target doesn't exist."""
        _upsert(
            store,
            note_id="src",
            path="src.md",
            title="Source",
            metadata=_meta(title="Source", edges={"related": ["ghost"]}),
        )

        links = store.get_note_links("src")
        edges = [e for e in links if e["kind"] == "edge"]
        assert len(edges) == 1
        assert edges[0]["target_id"] == "ghost"
        assert edges[0]["resolved_note_id"] is None

    def test_link_has_no_resolved_note_id(self, store):
        """Body wikilinks (kind='link') do not include resolved_note_id."""
        _upsert(
            store,
            note_id="src",
            path="src.md",
            title="Source",
            links=[Link(target="Some Title", display=None)],
        )

        links = store.get_note_links("src")
        wikilinks = [e for e in links if e["kind"] == "link"]
        assert len(wikilinks) == 1
        assert "resolved_note_id" not in wikilinks[0]

    def test_missing_note_returns_empty(self, store):
        assert store.get_note_links("nonexistent") == []


def test_get_graph_returns_nodes_and_edges(session):
    store = KnowledgeStore(session)

    # Seed two notes with a body wikilink from id-a to id-b.
    _upsert(
        store,
        note_id="id-a",
        path="a.md",
        content_hash="h-a",
        title="A",
        metadata=_meta(title="A", type="atom"),
        links=[Link(target="id-b", display=None)],
    )
    _upsert(
        store,
        note_id="id-b",
        path="b.md",
        content_hash="h-b",
        title="B",
        metadata=_meta(title="B", type="atom"),
    )

    result = store.get_graph()

    assert {n["id"] for n in result["nodes"]} == {"id-a", "id-b"}
    assert any(e["source"] == "id-a" and e["target"] == "id-b" for e in result["edges"])
    assert result["indexed_at"] is not None

    # Every node ships degree (server-side, was client-side) and x/y layout
    # positions (None until a layout job has populated them).
    for node in result["nodes"]:
        assert set(node.keys()) == {"id", "title", "type", "degree", "x", "y"}
        assert node["x"] is None
        assert node["y"] is None
    by_id = {n["id"]: n for n in result["nodes"]}
    assert by_id["id-a"]["degree"] == 1
    assert by_id["id-b"]["degree"] == 1


def test_get_graph_drops_edges_with_unresolved_targets(session):
    store = KnowledgeStore(session)
    _upsert(
        store,
        note_id="id-a",
        path="a.md",
        content_hash="h-a",
        title="A",
        metadata=_meta(title="A", type="atom"),
        # Edge points at a target that does not exist in the notes table.
        links=[Link(target="nonexistent", display=None)],
    )

    result = store.get_graph()

    # The unresolved edge is dropped, which leaves id-a with no edges.
    # Orphans (no edges) are filtered out of the graph response so the
    # layout step doesn't waste effort on disconnected dots.
    assert result["nodes"] == []
    assert result["edges"] == []


def test_get_graph_resolves_title_form_wikilink_to_slug_note_id(session):
    # Body wikilinks land in NoteLink.target_id as the user-typed text
    # (``"Steve Krug"``), but gap stubs (and most _processed notes)
    # store note_id in slug form (``"steve-krug"``). The graph endpoint
    # must slug-normalize before the membership check, otherwise inbound
    # edges to gap nodes are silently dropped — they render as orphans
    # on the canvas perimeter.
    store = KnowledgeStore(session)
    _upsert(
        store,
        note_id="src",
        path="src.md",
        content_hash="h-src",
        title="Source",
        metadata=_meta(title="Source", type="atom"),
        links=[Link(target="Steve Krug", display=None)],
    )
    _upsert(
        store,
        note_id="steve-krug",
        path="_researching/steve-krug.md",
        content_hash="h-gap",
        title="Steve Krug",
        metadata=_meta(title="Steve Krug", type="gap"),
    )

    result = store.get_graph()

    edges = result["edges"]
    assert len(edges) == 1
    edge = edges[0]
    assert edge["source"] == "src"
    # Edge target is rewritten to the canonical slug so it joins
    # cleanly with the corresponding node id on the frontend.
    assert edge["target"] == "steve-krug"
    by_id = {n["id"]: n for n in result["nodes"]}
    assert by_id["src"]["degree"] == 1
    assert by_id["steve-krug"]["degree"] == 1


def test_get_graph_drops_title_form_wikilink_with_no_matching_slug(session):
    # Negative companion to the title-form resolution test: when the
    # slugified target still doesn't match any visible note, the edge
    # is dropped — slug normalization must not invent phantom edges.
    store = KnowledgeStore(session)
    _upsert(
        store,
        note_id="src",
        path="src.md",
        content_hash="h-src",
        title="Source",
        metadata=_meta(title="Source", type="atom"),
        # No note with note_id="steve-krug" exists.
        links=[Link(target="Steve Krug", display=None)],
    )

    result = store.get_graph()

    # The unresolved edge leaves ``src`` with no edges; the orphan
    # filter then drops the node from the response entirely.
    assert result["nodes"] == []
    assert result["edges"] == []


def test_get_graph_returns_empty_for_empty_session(session):
    store = KnowledgeStore(session)

    result = store.get_graph()

    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["indexed_at"] is None


def test_get_graph_drops_typed_edges_with_unresolved_targets(session):
    store = KnowledgeStore(session)

    # Source note A has a frontmatter typed edge (kind='edge') pointing at a
    # target that does not exist in the notes table. Unlike get_note_links()
    # — which preserves the edge with resolved_note_id=None — get_graph()
    # must drop it, just like body-wikilink edges (kind='link').
    _upsert(
        store,
        note_id="id-a",
        path="a.md",
        content_hash="h-a",
        title="A",
        metadata=_meta(
            title="A",
            type="atom",
            edges={"refines": ["missing-target"]},
        ),
    )

    result = store.get_graph()

    # Edge dropped, id-a becomes an orphan, orphan filter drops the node.
    assert result["nodes"] == []
    assert result["edges"] == []


def test_get_graph_preserves_edge_type_when_target_resolves(session):
    store = KnowledgeStore(session)

    # Source note A has a typed edge (kind='edge') pointing at note B which
    # does exist — the edge should survive with edge_type preserved.
    _upsert(
        store,
        note_id="id-a",
        path="a.md",
        content_hash="h-a",
        title="A",
        metadata=_meta(
            title="A",
            type="atom",
            edges={"refines": ["id-b"]},
        ),
    )
    _upsert(
        store,
        note_id="id-b",
        path="b.md",
        content_hash="h-b",
        title="B",
        metadata=_meta(title="B", type="atom"),
    )

    result = store.get_graph()

    typed_edges = [e for e in result["edges"] if e["kind"] == "edge"]
    assert len(typed_edges) == 1
    assert typed_edges[0]["source"] == "id-a"
    assert typed_edges[0]["target"] == "id-b"
    assert typed_edges[0]["edge_type"] == "refines"


def test_get_graph_response_includes_degree_and_positions(session):
    """Server now computes degree (was client-side) and ships positions on each node."""
    store = KnowledgeStore(session)

    # note-a links to both note-b and note-c. note-a also has persisted
    # layout coordinates; the others do not. (We used to also seed an
    # isolated note-d here, but orphans are now filtered out of the
    # graph response — see test_get_graph_filters_orphan_and_typed_nodes
    # for the dedicated orphan-filter assertions.)
    _upsert(
        store,
        note_id="note-a",
        path="a.md",
        content_hash="h-a",
        title="A",
        metadata=_meta(title="A", type="atom"),
        links=[
            Link(target="note-b", display=None),
            Link(target="note-c", display=None),
        ],
    )
    _upsert(
        store,
        note_id="note-b",
        path="b.md",
        content_hash="h-b",
        title="B",
        metadata=_meta(title="B", type="atom"),
    )
    _upsert(
        store,
        note_id="note-c",
        path="c.md",
        content_hash="h-c",
        title="C",
        metadata=_meta(title="C", type="atom"),
    )

    # Set layout coordinates on note-a directly — the upsert path doesn't
    # touch layout fields (those are written by the layout job, not ingest).
    note_a = session.execute(select(Note).where(Note.note_id == "note-a")).scalar_one()
    note_a.layout_x = 0.3
    note_a.layout_y = -0.4
    session.add(note_a)
    session.commit()

    result = store.get_graph()
    by_id = {n["id"]: n for n in result["nodes"]}

    assert by_id["note-a"]["degree"] == 2
    assert by_id["note-b"]["degree"] == 1
    assert by_id["note-c"]["degree"] == 1

    assert by_id["note-a"]["x"] == 0.3
    assert by_id["note-a"]["y"] == -0.4
    assert by_id["note-b"]["x"] is None
    assert by_id["note-b"]["y"] is None


def test_get_graph_filters_nodes_by_type(session):
    """Notes whose type isn't in GRAPH_NOTE_TYPES are dropped, and
    orphan nodes (no edges) are dropped too.

    Locks in two server-as-source-of-truth contracts:

    1. The type filter used to live client-side in +page.svelte, where
       the server's ``degree`` (computed over all visible edges) was
       incoherent with what the user saw (since the client further
       dropped nodes). Now the server filters first, then computes
       degree, so the two stay aligned.
    2. Orphan filtering (added in feat/kg-layout-v2): orphans carry no
       graph information and waste layout effort, so we drop them on
       the way out.
    """
    store = KnowledgeStore(session)

    # An in-set atom links to a paper (in-set) and a "weird" note (out-
    # of-set). Only the atom→paper edge should survive; the atom's
    # degree should be 1, not 2. Plus an isolated in-set atom that has
    # no edges at all — should be dropped by the orphan filter even
    # though its type passes the type filter.
    _upsert(
        store,
        note_id="atom-1",
        path="atom.md",
        content_hash="h-atom",
        title="Atom",
        metadata=_meta(title="Atom", type="atom"),
        links=[
            Link(target="paper-1", display=None),
            Link(target="weird-1", display=None),
        ],
    )
    _upsert(
        store,
        note_id="paper-1",
        path="paper.md",
        content_hash="h-paper",
        title="Paper",
        metadata=_meta(title="Paper", type="paper"),
    )
    _upsert(
        store,
        note_id="weird-1",
        path="weird.md",
        content_hash="h-weird",
        title="Weird",
        metadata=_meta(title="Weird", type="something-weird"),
    )
    # Isolated atom — passes the type filter, fails the orphan filter.
    _upsert(
        store,
        note_id="lonely-1",
        path="lonely.md",
        content_hash="h-lonely",
        title="Lonely",
        metadata=_meta(title="Lonely", type="atom"),
    )

    result = store.get_graph()

    node_ids = {n["id"] for n in result["nodes"]}
    assert node_ids == {"atom-1", "paper-1"}, (
        "weird-typed node must be dropped by type filter; "
        "lonely-1 must be dropped by orphan filter"
    )

    edge_pairs = {(e["source"], e["target"]) for e in result["edges"]}
    assert edge_pairs == {("atom-1", "paper-1")}, (
        "atom→weird edge must be dropped because weird-1 is filtered out"
    )

    by_id = {n["id"]: n for n in result["nodes"]}
    # Degree counts the visible neighbour only (paper-1), not the
    # filtered-out weird-1.
    assert by_id["atom-1"]["degree"] == 1
    assert by_id["paper-1"]["degree"] == 1


def test_get_graph_drops_notes_with_null_type(session):
    """A note with type=None is dropped from the graph response."""
    store = KnowledgeStore(session)

    # Use a connected pair so the orphan filter doesn't also drop the
    # typed survivor — this test is about the null-type path, not
    # orphan filtering.
    _upsert(
        store,
        note_id="typed",
        path="typed.md",
        content_hash="h-t",
        title="T",
        metadata=_meta(title="T", type="atom"),
        links=[Link(target="typed-2", display=None)],
    )
    _upsert(
        store,
        note_id="typed-2",
        path="typed-2.md",
        content_hash="h-t2",
        title="T2",
        metadata=_meta(title="T2", type="atom"),
    )
    _upsert(
        store,
        note_id="untyped",
        path="untyped.md",
        content_hash="h-u",
        title="U",
        metadata=_meta(title="U", type=None),
    )

    result = store.get_graph()

    node_ids = {n["id"] for n in result["nodes"]}
    assert node_ids == {"typed", "typed-2"}, "untyped note must be dropped"
