"""Tests for KnowledgeStore."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import ANY

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.entities import Entity, NoteEntity
from knowledge.freshness import utc
from knowledge.frontmatter import ParsedFrontmatter
from knowledge.links import Link
from knowledge.models import Chunk, Note, NoteLink
from knowledge.store import KnowledgeStore, _rank_search_chunks


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.db'}")
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
    metadata = metadata or _meta(title=title, verification_state="verified")
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

    def test_upsert_carries_revision_forward_so_it_never_resets(self, store, session):
        _upsert(store, note_id="rev", path="rev.md", content_hash="h1")
        note = session.exec(select(Note).where(Note.note_id == "rev")).one()
        assert note.revision == 0
        # A reindex of identical content and a move into _processed/ both
        # replace the row; the counter still advances.
        _upsert(store, note_id="rev", path="rev.md", content_hash="h1")
        _upsert(store, note_id="rev", path="_processed/rev.md", content_hash="h1")
        _upsert(store, note_id="rev", path="_processed/rev.md", content_hash="h2")
        note = session.exec(select(Note).where(Note.note_id == "rev")).one()
        assert note.revision == 3

    def test_upsert_refreshes_a_stale_revision_before_replacing_a_note(
        self, store, session
    ):
        _upsert(store, note_id="rev", path="rev.md", content_hash="h1")
        stale = session.exec(select(Note).where(Note.note_id == "rev")).one()
        with Session(session.get_bind()) as other:
            row = other.exec(select(Note).where(Note.note_id == "rev")).one()
            row.confidence = 0.8
            other.commit()
        assert stale.revision == 0
        _upsert(store, note_id="rev", path="rev.md", content_hash="h1")
        current = session.exec(select(Note).where(Note.note_id == "rev")).one()
        assert current.revision == 2

    def test_a_review_captured_before_a_reindex_cannot_renew(self, store, session):
        from knowledge.freshness import commit_successful_review

        now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
        _upsert(store, note_id="rev", path="rev.md", content_hash="h1")
        captured = session.exec(select(Note).where(Note.note_id == "rev")).one()
        revision, content_hash = captured.revision, captured.content_hash
        # Retelling then same-content reindex: the old reset-to-zero let (0, h1)
        # pass both checks.
        _upsert(store, note_id="rev", path="rev.md", content_hash="h1")
        _upsert(store, note_id="rev", path="_processed/rev.md", content_hash="h1")
        result = commit_successful_review(
            session,
            note_id="rev",
            expected_revision=revision,
            expected_content_hash=content_hash,
            evidence=["PR #1 is open"],
            evidence_observed_at=now,
            now=now,
        )
        assert not result.renewed and result.reason == "revision_changed"

    def test_upsert_preserves_published_at(self, store, session):
        _upsert(store, note_id="published", path="published.md", content_hash="h1")
        note = session.exec(select(Note).where(Note.note_id == "published")).one()
        published_at = datetime.now(timezone.utc)
        note.published_at = published_at
        session.add(note)
        session.commit()

        _upsert(store, note_id="published", path="published.md", content_hash="h2")

        replacement = session.exec(
            select(Note).where(Note.note_id == "published")
        ).one()
        assert replacement.published_at.replace(tzinfo=timezone.utc) == published_at

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


@pytest.mark.parametrize("scope_filter", [None, "repo:jomcgi-org/homelab"])
@pytest.mark.parametrize("exclude_invalidated", [False, True])
@pytest.mark.parametrize("include_legacy", [False, True])
@pytest.mark.parametrize("include_deployment_observations", [False, True])
def test_context_search_forwards_all_ranking_filters(
    store,
    session,
    monkeypatch,
    scope_filter,
    exclude_invalidated,
    include_legacy,
    include_deployment_observations,
):
    calls = []

    def rank(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr("knowledge.store._rank_search_chunks", rank)

    assert (
        store.search_notes_with_context(
            [0.0] * 1024,
            scope_filter=scope_filter,
            exclude_invalidated=exclude_invalidated,
            include_legacy=include_legacy,
            include_deployment_observations=include_deployment_observations,
        )
        == []
    )
    # A single scope_filter is normalised into the scope_filters tuple that
    # _rank_search_chunks accepts; include_unscoped stays at its default.
    assert calls == [
        (
            (session, [0.0] * 1024, 20, None),
            {
                "scope_filters": (scope_filter,) if scope_filter is not None else None,
                "query_text": None,
                "include_unscoped": False,
                "exclude_invalidated": exclude_invalidated,
                "include_legacy": include_legacy,
                "include_deployment_observations": include_deployment_observations,
                "include_history": False,
                "now": ANY,
            },
        )
    ]


def test_default_legacy_predicate_preserves_null_state_semantics():
    class EmptyResult:
        def all(self):
            return []

    class RecordingSession:
        def __init__(self):
            self.statement = None

        def execute(self, statement):
            self.statement = statement
            return EmptyResult()

    session = RecordingSession()
    assert _rank_search_chunks(session, [0.0] * 1024, 20, None) == []

    sql = str(session.statement)
    assert "verification_state IS NULL" in sql
    assert "verification_state !=" in sql


class TestGetNoteById:
    def test_returns_note_metadata(self, store):
        _upsert(
            store,
            note_id="n1",
            path="folder/note.md",
            title="My Note",
            metadata=_meta(
                title="My Note",
                type="paper",
                tags=["x"],
                verification_state="legacy",
            ),
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
            "review_after": None,
            "review_policy": "standard-90d/v1",
            "last_reviewed_at": None,
            "freshness": "unknown",
            "requires_authoritative_observation": False,
            "last_review_outcome": None,
            "disputed": False,
            "provenance": [],
            "entities": [],
        }

    def test_surfaces_why_a_due_fact_is_still_due(self, store):
        from knowledge.freshness import record_outcome

        _upsert(store, note_id="n1", path="n1.md", title="My Note", metadata=_meta())
        now = datetime(2026, 10, 4, tzinfo=timezone.utc)
        record_outcome(
            store.session,
            note_id="n1",
            revision=0,
            status="unsupported",
            reason="state 'ready' is not verifiable from GitHub",
            now=now,
        )
        store.session.commit()
        got = store.get_note_by_id("n1")["last_review_outcome"]
        assert (got["status"], got["reason"], got["next_attempt_at"]) == (
            "unsupported",
            "state 'ready' is not verifiable from GitHub",
            None,
        )
        assert utc(got["attempted_at"]) == now

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


class TestEntityLinks:
    def test_note_payload_includes_entities(self, store, session):
        _upsert(
            store,
            note_id="entity-note",
            path="entity-note.md",
            title="Entity note",
            metadata=_meta(
                title="Entity note",
                type="fact",
                verification_state="verified",
            ),
        )
        entity = Entity(
            kind="project",
            slug="monolith",
            title="Monolith",
            aliases=["kg"],
            source="manifest",
        )
        session.add(entity)
        session.flush()
        session.add(
            NoteEntity(
                note_id="entity-note",
                entity_id=entity.id,
                role="subject",
                source="extraction",
            )
        )
        session.commit()

        expected = [
            {
                "kind": "project",
                "slug": "monolith",
                "title": "Monolith",
                "role": "subject",
            }
        ]
        assert store.get_note_by_id("entity-note")["entities"] == expected


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


class TestPrivateReviewHistory:
    def test_empty_is_opt_in_and_missing_or_deleted_stays_missing(self, store, session):
        _upsert(store, content="A fact")
        plain = store.get_note_by_id("a-id")
        assert "review_history" not in plain
        expanded = store.get_note_by_id("a-id", include_review_history=True)
        history = expanded.pop("review_history")
        assert expanded == plain
        assert history == {
            "current_revision": 0,
            "limit": 10,
            "has_more": False,
            "entries": [],
        }
        assert store.get_note_by_id("missing", include_review_history=True) is None
        note = session.exec(select(Note).where(Note.note_id == "a-id")).one()
        note.deleted_at = datetime.now(UTC)
        session.commit()
        assert store.get_note_by_id("a-id", include_review_history=True) is None

    def test_bounded_projection_orders_dates_then_ids_without_changing_freshness(
        self, store, session
    ):
        from knowledge.models import ReviewOutcome

        _upsert(store, content="A fact")
        note = session.exec(select(Note).where(Note.note_id == "a-id")).one()
        note.observed_at = datetime(2026, 10, 19, tzinfo=UTC)
        note.review_after = datetime(2026, 10, 20, tzinfo=UTC)
        note.verification_state = "disputed"
        stamp = datetime(2026, 10, 19, 12, tzinfo=UTC)
        statuses = ["success", "failed", "unsupported", "unavailable"]
        rows = [
            ReviewOutcome(
                note_id="a-id",
                status=statuses[i % 4],
                reason="r" * 1200,
                note_revision=0 if i % 2 else 1,
                attempted_at=stamp + timedelta(minutes=i // 2),
                evidence_observed_at=stamp,
                next_attempt_at=stamp + timedelta(days=1),
                evidence=["private source bundle"],
            )
            for i in range(30)
        ]
        # Insertion order intentionally differs from attempted_at ordering.
        rows.reverse()
        session.add_all(
            rows
            + [
                ReviewOutcome(
                    note_id="another-note",
                    status="success",
                    reason="other note",
                    note_revision=1,
                    attempted_at=stamp + timedelta(days=1),
                )
            ]
        )
        session.commit()
        expected = sorted(
            rows, key=lambda row: (row.attempted_at, row.id), reverse=True
        )
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append((statement, parameters))

        event.listen(session.get_bind(), "before_cursor_execute", capture)
        try:
            plain = store.get_note_by_id("a-id")
            plain_count = len(statements)
            statements.clear()
            expanded = store.get_note_by_id(
                "a-id", include_review_history=True, review_history_limit=25
            )
            # Existing latest-outcome/current-state reads occur once, plus one
            # bounded projection query. No query per history row or other note.
            assert len(statements) == plain_count + 1
            projected = [
                (sql, params) for sql, params in statements if "substr(" in sql
            ]
            assert len(projected) == 1
            sql, params = projected[0]
            assert "review_outcomes.evidence," not in sql
            assert "LIMIT" in sql and 26 in params
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", capture)
        history = expanded.pop("review_history")
        assert expanded == plain
        assert history["has_more"] is True
        assert history["current_revision"] == 1
        assert [entry["id"] for entry in history["entries"]] == [
            row.id for row in expected[:25]
        ]
        assert len(history["entries"]) == 25
        for entry, row in zip(history["entries"], expected):
            assert entry["status"] == row.status
            assert entry["is_current_revision"] == (row.note_revision == 1)
            assert entry["reason"] == "r" * 1000
            assert entry["reason_truncated"] is True
            assert entry["reviewer_lane"] is None
            assert entry["evidence_observed_at"] == stamp.isoformat()
            assert entry["next_attempt_at"] == (stamp + timedelta(days=1)).isoformat()
            assert entry["attempted_at"].endswith("+00:00")
            assert "evidence" not in entry
        default = store.get_note_by_id("a-id", include_review_history=True)[
            "review_history"
        ]
        assert len(default["entries"]) == 10 and default["has_more"]
        assert not session.dirty and not session.new

    @pytest.mark.parametrize("limit", [0, -1, 26, 1000])
    def test_rejects_unbounded_read(self, store, limit):
        with pytest.raises(ValueError, match="between 1 and 25"):
            store.get_note_by_id(
                "missing", include_review_history=True, review_history_limit=limit
            )
