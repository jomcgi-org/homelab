"""Unit tests for knowledge.public_router -- /api/knowledge/public/* endpoints.

Uses a minimal FastAPI app that mounts only the public router with
``get_session`` overridden onto a SQLite-backed session. The schema-strip
pattern (same as router_test.py) lets SQLModel.metadata.create_all() build
PublicNote / PublicNoteLink as plain SQLite tables for tests.

Coverage:
  GET /api/knowledge/public/graph:
    - empty graph (no notes, no edges)
    - graph with nodes and edges, degree computation
    - nodes outside GRAPH_NOTE_TYPES filtered out
    - private-target edges dropped (target not in public set)
    - ETag 304 short-circuit when If-None-Match matches
    - Cache-Control / ETag / Last-Modified headers set

  GET /api/knowledge/public/notes/{note_id}:
    - found note with body returns 200
    - private/missing note returns identical 404
    - note with no body returns 404
    - wikilinks to private notes stripped to plain text
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from knowledge.public_models import (
    PublicEntity,
    PublicNote,
    PublicNoteEntity,
    PublicNoteLink,
)
from knowledge.public_limits import reset_semantic_search_limits
from knowledge.public_router import router

_UTC = timezone.utc
_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with schema stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
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


@pytest.fixture(name="client")
def client_fixture(session):
    reset_semantic_search_limits()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()
    reset_semantic_search_limits()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_note(
    note_id: str,
    title: str,
    type_: str = "atom",
    content: str | None = "Body text.",
    indexed_at: datetime = _NOW,
    x: float | None = None,
    y: float | None = None,
    verification_state: str = "verified",
    disputed: bool = False,
) -> PublicNote:
    return PublicNote(
        note_id=note_id,
        title=title,
        type=type_,
        content=content,
        indexed_at=indexed_at,
        path=f"atoms/{note_id}.md",
        layout_x=x,
        layout_y=y,
        verification_state=verification_state,
        disputed=disputed,
    )


def _make_link(
    id_: int, source: str, target: str, kind: str = "link"
) -> PublicNoteLink:
    return PublicNoteLink(id=id_, source=source, target=target, kind=kind)


def _make_entity(
    id_: int = 1,
    *,
    kind: str = "project",
    slug: str = "embervm",
    title: str = "EmberVM",
) -> PublicEntity:
    return PublicEntity(
        id=id_,
        kind=kind,
        slug=slug,
        title=title,
        aliases=[],
        source="knowledge/entities.yaml",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _link_entity(
    id_: int,
    note_id: str,
    *,
    entity_id: int = 1,
    role: str = "subject",
    state: str = "verified",
) -> PublicNoteEntity:
    return PublicNoteEntity(
        id=id_,
        note_id=note_id,
        entity_id=entity_id,
        role=role,
        source="extractor",
        created_at=_NOW,
        verification_state=state,
        note_indexed_at=_NOW,
    )


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/graph -- empty
# ---------------------------------------------------------------------------


class TestPublicGraphEmpty:
    def test_empty_graph_returns_200(self, client):
        resp = client.get("/api/knowledge/public/graph")
        assert resp.status_code == 200

    def test_empty_graph_has_no_nodes_or_edges(self, client):
        body = client.get("/api/knowledge/public/graph").json()
        assert body["nodes"] == []
        assert body["edges"] == []

    def test_empty_graph_indexed_at_is_none(self, client):
        body = client.get("/api/knowledge/public/graph").json()
        assert body["indexed_at"] is None

    def test_empty_graph_has_cache_control_header(self, client):
        resp = client.get("/api/knowledge/public/graph")
        assert "Cache-Control" in resp.headers
        assert "public" in resp.headers["Cache-Control"]

    def test_empty_graph_has_etag_header(self, client):
        resp = client.get("/api/knowledge/public/graph")
        assert "ETag" in resp.headers


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/graph -- nodes and edges
# ---------------------------------------------------------------------------


class TestPublicGraphWithData:
    def test_nodes_returned_for_graph_types(self, client, session):
        session.add(_make_note("n1", "Atom One", type_="atom"))
        session.add(_make_note("n2", "Fact One", type_="fact"))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        ids = {n["id"] for n in body["nodes"]}
        assert "n1" in ids
        assert "n2" in ids

    def test_non_graph_type_excluded(self, client, session):
        session.add(_make_note("n1", "Atom", type_="atom"))
        session.add(_make_note("n2", "Journal", type_="journal"))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        ids = {n["id"] for n in body["nodes"]}
        assert "n1" in ids
        assert "n2" not in ids

    def test_edge_between_two_public_nodes_included(self, client, session):
        session.add(_make_note("n1", "Source"))
        session.add(_make_note("n2", "Target"))
        session.add(_make_link(1, "n1", "n2"))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        assert len(body["edges"]) == 1
        assert body["edges"][0]["source"] == "n1"
        assert body["edges"][0]["target"] == "n2"

    def test_edge_to_private_target_dropped(self, client, session):
        """A link whose target is not in the public set must be excluded."""
        session.add(_make_note("n1", "Public Source"))
        # n-private is NOT in PublicNote -- simulates a private note
        session.add(_make_link(1, "n1", "n-private"))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        assert body["edges"] == []

    def test_degree_computed_from_edges(self, client, session):
        session.add(_make_note("hub", "Hub"))
        session.add(_make_note("spoke", "Spoke"))
        session.add(_make_link(1, "hub", "spoke"))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        by_id = {n["id"]: n for n in body["nodes"]}
        assert by_id["hub"]["degree"] == 1
        assert by_id["spoke"]["degree"] == 1

    def test_node_with_no_edges_has_degree_zero(self, client, session):
        session.add(_make_note("isolated", "Isolated"))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        assert body["nodes"][0]["degree"] == 0

    def test_indexed_at_returns_iso_string(self, client, session):
        session.add(_make_note("n1", "Note", indexed_at=_NOW))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        assert body["indexed_at"] is not None
        # Should be an ISO-format string
        parsed = datetime.fromisoformat(body["indexed_at"])
        assert isinstance(parsed, datetime)

    def test_layout_coordinates_included_in_node(self, client, session):
        session.add(_make_note("n1", "Placed", x=3.14, y=2.71))
        session.commit()

        body = client.get("/api/knowledge/public/graph").json()
        node = body["nodes"][0]
        assert node["x"] == pytest.approx(3.14)
        assert node["y"] == pytest.approx(2.71)

    def test_verification_state_included_in_node(self, client, session):
        session.add(_make_note("n1", "Unverified", verification_state="unverified"))
        session.commit()

        node = client.get("/api/knowledge/public/graph").json()["nodes"][0]
        assert node["verification_state"] == "unverified"


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/graph -- caching / ETag
# ---------------------------------------------------------------------------


class TestPublicGraphCaching:
    def test_304_when_if_none_match_matches_etag(self, client, session):
        resp1 = client.get("/api/knowledge/public/graph")
        etag = resp1.headers["ETag"]

        resp2 = client.get(
            "/api/knowledge/public/graph",
            headers={"If-None-Match": etag},
        )
        assert resp2.status_code == 304

    def test_200_when_if_none_match_differs(self, client, session):
        resp = client.get(
            "/api/knowledge/public/graph",
            headers={"If-None-Match": '"stale-etag"'},
        )
        assert resp.status_code == 200

    def test_last_modified_header_set_when_notes_present(self, client, session):
        session.add(_make_note("n1", "Note"))
        session.commit()

        resp = client.get("/api/knowledge/public/graph")
        assert "Last-Modified" in resp.headers

    def test_last_modified_header_absent_when_no_notes(self, client):
        resp = client.get("/api/knowledge/public/graph")
        assert "Last-Modified" not in resp.headers


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/notes/{note_id}
# ---------------------------------------------------------------------------


class TestPublicNote_:
    def test_returns_200_for_public_note(self, client, session):
        session.add(_make_note("n1", "Public Atom", content="# Hello\n\nWorld."))
        session.commit()

        resp = client.get("/api/knowledge/public/notes/n1")
        assert resp.status_code == 200

    def test_returned_note_fields(self, client, session):
        note = _make_note(
            "n1",
            "My Note",
            content="Body here.",
            verification_state="unverified",
            disputed=True,
        )
        note.confidence = 0.7
        note.scope = "org:jomcgi-org"
        note.observed_at = _NOW
        note.valid_from = _NOW
        note.published_at = _NOW
        session.add(note)
        session.commit()

        body = client.get("/api/knowledge/public/notes/n1").json()
        assert body["note_id"] == "n1"
        assert body["title"] == "My Note"
        assert body["body"] == "Body here."
        assert "tags" in body
        assert "aliases" in body
        assert "indexed_at" in body
        assert body["verification_state"] == "unverified"
        assert body["confidence"] == pytest.approx(0.7)
        assert body["scope"] == "org:jomcgi-org"
        assert body["observed_at"] is not None
        assert body["valid_from"] is not None
        assert body["valid_until"] is None
        assert body["published_at"] is not None
        assert body["disputed"] is True

    def test_404_for_missing_note(self, client):
        resp = client.get("/api/knowledge/public/notes/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Not Found"

    def test_404_when_note_has_no_body(self, client, session):
        """None content produces the same 404 as a missing note."""
        session.add(_make_note("n1", "No body", content=None))
        session.commit()

        resp = client.get("/api/knowledge/public/notes/n1")
        assert resp.status_code == 404

    def test_wikilinks_to_private_notes_stripped(self, client, session):
        """Wikilinks targeting non-public notes are replaced with plain text."""
        session.add(
            _make_note(
                "n1",
                "Public Note",
                content="See [[private thing]] here.",
            )
        )
        # "private thing" is NOT in the public note set
        session.commit()

        body = client.get("/api/knowledge/public/notes/n1").json()
        assert "[[private thing]]" not in body["body"]
        assert "private thing" in body["body"]

    def test_wikilinks_to_public_notes_kept(self, client, session):
        """Wikilinks targeting public notes should be kept intact."""
        session.add(_make_note("n1", "Source Note", content="See [[n2]] here."))
        session.add(_make_note("n2", "Target Note"))
        session.commit()

        body = client.get("/api/knowledge/public/notes/n1").json()
        # The wikilink to n2 (a public note) must remain in the body intact.
        assert "[[n2]]" in body["body"]

    def test_indexed_at_is_iso_string(self, client, session):
        session.add(_make_note("n1", "Note", indexed_at=_NOW))
        session.commit()

        body = client.get("/api/knowledge/public/notes/n1").json()
        assert body["indexed_at"] is not None
        parsed = datetime.fromisoformat(body["indexed_at"])
        assert isinstance(parsed, datetime)


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/entities/{kind}/{slug}/notes
# ---------------------------------------------------------------------------


class TestPublicEntityNotes:
    def test_missing_entity_is_404(self, client):
        response = client.get("/api/knowledge/public/entities/project/missing/notes")
        assert response.status_code == 404

    def test_returns_subjects_before_mentions_then_newest(self, client, session):
        session.add(_make_entity())
        older_subject = _make_note("subject-old", "Older subject")
        older_subject.observed_at = datetime(2024, 5, 1, tzinfo=_UTC)
        newer_subject = _make_note("subject-new", "Newer subject")
        newer_subject.observed_at = datetime(2024, 5, 2, tzinfo=_UTC)
        newest_mention = _make_note("mention", "Newest mention")
        newest_mention.observed_at = datetime(2024, 5, 3, tzinfo=_UTC)
        session.add(older_subject)
        session.add(newer_subject)
        session.add(newest_mention)
        session.add(_link_entity(1, "subject-old"))
        session.add(_link_entity(2, "subject-new"))
        session.add(_link_entity(3, "mention", role="mentions"))
        session.commit()

        body = client.get("/api/knowledge/public/entities/project/embervm/notes").json()

        assert body["entity"] == {
            "kind": "project",
            "slug": "embervm",
            "title": "EmberVM",
        }
        assert [row["note_id"] for row in body["notes"]] == [
            "subject-new",
            "subject-old",
            "mention",
        ]

    def test_filters_states_and_shapes_snippet(self, client, session):
        session.add(_make_entity())
        verified = _make_note("verified", "Verified", content="one\n\n two")
        unverified = _make_note(
            "unverified", "Unverified", verification_state="unverified"
        )
        session.add(verified)
        session.add(unverified)
        session.add(_link_entity(1, "verified"))
        session.add(_link_entity(2, "unverified", state="unverified"))
        session.commit()

        response = client.get(
            "/api/knowledge/public/entities/project/embervm/notes?state=verified"
        )

        assert response.status_code == 200
        assert [row["note_id"] for row in response.json()["notes"]] == ["verified"]
        assert response.json()["notes"][0]["snippet"] == "one two"

    def test_rejects_unknown_state_and_large_limit(self, client, session):
        session.add(_make_entity())
        session.commit()

        bad_state = client.get(
            "/api/knowledge/public/entities/project/embervm/notes?state=legacy"
        )
        bad_limit = client.get(
            "/api/knowledge/public/entities/project/embervm/notes?limit=61"
        )

        assert bad_state.status_code == 422
        assert bad_limit.status_code == 422

    def test_returns_contradictions_inside_entity_set(self, client, session):
        session.add(_make_entity())
        session.add(_make_note("fact-a", "Fact A"))
        session.add(
            _make_note(
                "fact-b", "Fact B", verification_state="unverified", disputed=True
            )
        )
        session.add(_make_note("outside", "Outside"))
        session.add(_link_entity(1, "fact-a"))
        session.add(_link_entity(2, "fact-b", state="unverified"))
        session.add(
            PublicNoteLink(
                id=1,
                source="fact-a",
                target="fact-b",
                kind="edge",
                edge_type="contradicts",
            )
        )
        session.add(
            PublicNoteLink(
                id=2,
                source="fact-a",
                target="outside",
                kind="edge",
                edge_type="contradicts",
            )
        )
        session.commit()

        body = client.get("/api/knowledge/public/entities/project/embervm/notes").json()

        assert len(body["contradictions"]) == 1
        pair = body["contradictions"][0]
        assert {pair["a"]["note_id"], pair["b"]["note_id"]} == {
            "fact-a",
            "fact-b",
        }

    def test_contradictions_keep_disputed_and_invalidated_marks(self, client, session):
        session.add(_make_entity())
        session.add(_make_note("current", "Current"))
        session.add(_make_note("disputed", "Disputed", verification_state="disputed"))
        session.add(
            _make_note("invalidated", "Invalidated", verification_state="invalidated")
        )
        session.add(_link_entity(1, "current"))
        session.add(_link_entity(2, "disputed", state="disputed"))
        session.add(_link_entity(3, "invalidated", state="invalidated"))
        session.add(
            PublicNoteLink(
                id=1,
                source="current",
                target="disputed",
                kind="edge",
                edge_type="contradicts",
            )
        )
        session.add(
            PublicNoteLink(
                id=2,
                source="current",
                target="invalidated",
                kind="edge",
                edge_type="contradicts",
            )
        )
        session.commit()

        body = client.get("/api/knowledge/public/entities/project/embervm/notes").json()

        assert [note["note_id"] for note in body["notes"]] == ["current"]
        states = {
            side["verification_state"]
            for pair in body["contradictions"]
            for side in (pair["a"], pair["b"])
        }
        assert {"disputed", "invalidated"} <= states

    def test_sets_cache_headers_and_supports_304(self, client, session):
        session.add(_make_entity())
        session.add(_make_note("fact", "Fact"))
        session.add(_link_entity(1, "fact"))
        session.commit()

        first = client.get("/api/knowledge/public/entities/project/embervm/notes")
        second = client.get(
            "/api/knowledge/public/entities/project/embervm/notes",
            headers={"If-None-Match": first.headers["etag"]},
        )

        assert "public" in first.headers["cache-control"]
        assert "last-modified" in first.headers
        assert second.status_code == 304


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/search-index
# ---------------------------------------------------------------------------


class TestPublicSearchIndex:
    def test_returns_compact_parallel_array_shape(self, client, session):
        session.add(_make_entity())
        session.add(_make_note("fact", "Ember fact"))
        session.add(_link_entity(1, "fact"))
        session.commit()

        body = client.get("/api/knowledge/public/search-index").json()

        assert body == {
            "generated_at": "2024-06-01T12:00:00Z",
            "states": ["verified", "unverified", "disputed", "invalidated"],
            "entities": ["embervm"],
            "notes": [["fact", "Ember fact", 0, 0]],
        }

    def test_includes_only_record_states(self, client, session):
        session.add(_make_note("verified", "Verified"))
        session.add(
            _make_note("unverified", "Unverified", verification_state="unverified")
        )
        session.add(_make_note("legacy", "Legacy", verification_state="legacy"))
        session.add(_make_note("disputed", "Disputed", verification_state="disputed"))
        session.add(
            _make_note("invalidated", "Invalidated", verification_state="invalidated")
        )
        session.commit()

        body = client.get("/api/knowledge/public/search-index").json()

        assert {note[0] for note in body["notes"]} == {"verified", "unverified"}
        assert {note[2] for note in body["notes"]} == {0, 1}

    def test_orders_newest_observation_first_with_nulls_last(self, client, session):
        oldest = _make_note(
            "oldest", "Oldest", indexed_at=datetime(2024, 6, 3, tzinfo=_UTC)
        )
        oldest.observed_at = datetime(2024, 5, 1, tzinfo=_UTC)
        newest = _make_note(
            "newest", "Newest", indexed_at=datetime(2024, 6, 1, tzinfo=_UTC)
        )
        newest.observed_at = datetime(2024, 5, 2, tzinfo=_UTC)
        no_observation = _make_note(
            "unobserved",
            "Unobserved",
            indexed_at=datetime(2024, 6, 4, tzinfo=_UTC),
        )
        session.add_all([oldest, newest, no_observation])
        session.commit()

        body = client.get("/api/knowledge/public/search-index").json()

        assert [note[0] for note in body["notes"]] == [
            "newest",
            "oldest",
            "unobserved",
        ]

    def test_supports_conditional_get(self, client, session):
        session.add(_make_note("fact", "Fact"))
        session.commit()

        first = client.get("/api/knowledge/public/search-index")
        second = client.get(
            "/api/knowledge/public/search-index",
            headers={"If-None-Match": first.headers["etag"]},
        )

        assert first.headers["cache-control"] == (
            "public, max-age=300, s-maxage=300, stale-while-revalidate=86400"
        )
        assert second.status_code == 304

    def test_truncates_at_hard_ceiling(self, client, session, monkeypatch, caplog):
        monkeypatch.setattr("knowledge.public_router._SEARCH_INDEX_NOTE_LIMIT", 2)
        for index in range(3):
            note = _make_note(f"fact-{index}", f"Fact {index}")
            note.observed_at = _NOW + timedelta(minutes=index)
            session.add(note)
        session.commit()

        with caplog.at_level("WARNING"):
            body = client.get("/api/knowledge/public/search-index").json()

        assert [note[0] for note in body["notes"]] == ["fact-2", "fact-1"]
        assert "public.search_index.truncated limit=2" in caplog.text


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/search
# ---------------------------------------------------------------------------


class TestPublicRecordSearch:
    def test_grep_matches_title_and_content_case_insensitively(self, client, session):
        session.add(_make_note("title", "Ember quarantine"))
        session.add(_make_note("content", "Another fact", content="EMBER drain"))
        session.add(_make_note("miss", "Other fact", content="nothing"))
        session.commit()

        body = client.get("/api/knowledge/public/search?q=ember").json()

        assert {row["note_id"] for row in body} == {"title", "content"}

    def test_grep_excludes_legacy_and_includes_entities(self, client, session):
        session.add(_make_entity())
        session.add(_make_note("current", "Needle current"))
        session.add(_make_note("old", "Needle legacy", verification_state="legacy"))
        session.add(_link_entity(1, "current"))
        session.commit()

        body = client.get("/api/knowledge/public/search?q=needle").json()

        assert [row["note_id"] for row in body] == ["current"]
        assert body[0]["entities"] == [
            {"kind": "project", "slug": "embervm", "title": "EmberVM"}
        ]

    def test_grep_excludes_disputed_and_invalidated_states(self, client, session):
        session.add(_make_note("current", "Needle current"))
        session.add(
            _make_note("disputed", "Needle disputed", verification_state="disputed")
        )
        session.add(
            _make_note(
                "invalidated", "Needle invalidated", verification_state="invalidated"
            )
        )
        session.commit()

        body = client.get("/api/knowledge/public/search?q=needle").json()

        assert [row["note_id"] for row in body] == ["current"]

    @pytest.mark.parametrize(
        ("query", "matching"),
        [
            ("%", "literal-percent"),
            ("_", "literal-underscore"),
            ("\\", "literal-slash"),
        ],
    )
    def test_grep_treats_like_metacharacters_literally(
        self, client, session, query, matching
    ):
        session.add(_make_note("literal-percent", "Contains 100% certainty"))
        session.add(_make_note("literal-underscore", "Contains under_score"))
        session.add(_make_note("literal-slash", r"Contains a back\\slash"))
        session.add(_make_note("ordinary", "Ordinary title"))
        session.commit()

        body = client.get("/api/knowledge/public/search", params={"q": query}).json()

        assert [row["note_id"] for row in body] == [matching]

    def test_blank_query_returns_no_rows(self, client, session):
        session.add(_make_note("fact", "Fact"))
        session.commit()

        assert client.get("/api/knowledge/public/search?q=").json() == []

    def test_validates_mode_query_length_and_limit(self, client):
        assert (
            client.get("/api/knowledge/public/search?mode=other&q=x").status_code == 422
        )
        assert (
            client.get("/api/knowledge/public/search?q=" + ("x" * 201)).status_code
            == 422
        )
        assert (
            client.get("/api/knowledge/public/search?q=x&limit=51").status_code == 422
        )

    def test_semantic_search_uses_public_chunks(self, client, session, monkeypatch):
        class FakeEmbeddingClient:
            base_url = "http://embedding.test"

            async def embed(self, query):
                assert query == "quarantine"
                return [0.1, 0.2]

        session.add(_make_entity())
        session.add(_make_note("semantic", "Semantic match"))
        session.add(_link_entity(1, "semantic"))
        session.commit()
        monkeypatch.setattr(
            "knowledge.public_router.EmbeddingClient", FakeEmbeddingClient
        )
        monkeypatch.setattr(
            "knowledge.public_router.search_public_chunks",
            lambda _session, vector, limit: [
                {
                    "note_id": "semantic",
                    "title": "Semantic match",
                    "verification_state": "verified",
                    "disputed": False,
                    "score": 0.9,
                    "chunk_text": "match",
                }
            ],
        )

        response = client.get(
            "/api/knowledge/public/search?q=quarantine&mode=semantic&limit=5"
        )

        assert response.status_code == 200
        assert response.json()[0]["note_id"] == "semantic"
        assert response.json()[0]["entities"][0]["slug"] == "embervm"

    def test_semantic_search_drops_repo_docs_and_non_record_states(
        self, client, session, monkeypatch
    ):
        class FakeEmbeddingClient:
            base_url = "http://embedding.test"

            async def embed(self, _query):
                return [0.1, 0.2]

        session.add(_make_note("kept", "Kept"))
        session.add(_make_note("disputed", "Disputed", verification_state="disputed"))
        session.commit()
        monkeypatch.setattr(
            "knowledge.public_router.EmbeddingClient", FakeEmbeddingClient
        )
        monkeypatch.setattr(
            "knowledge.public_router.search_public_chunks",
            lambda _session, vector, limit: [
                {"note_id": "repo:README.md"},
                {"note_id": "disputed"},
                {"note_id": "kept"},
            ],
        )

        body = client.get("/api/knowledge/public/search?q=record&mode=semantic").json()

        assert [row["note_id"] for row in body] == ["kept"]

    def test_semantic_search_is_limited_to_ten_per_client(
        self, client, session, monkeypatch
    ):
        class FakeEmbeddingClient:
            base_url = "http://embedding.test"

            async def embed(self, _query):
                return [0.1, 0.2]

        monkeypatch.setattr(
            "knowledge.public_router.EmbeddingClient", FakeEmbeddingClient
        )
        monkeypatch.setattr(
            "knowledge.public_router.search_public_chunks",
            lambda _session, vector, limit: [],
        )

        for _ in range(10):
            assert (
                client.get("/api/knowledge/public/search?q=x&mode=semantic").status_code
                == 200
            )
        limited = client.get("/api/knowledge/public/search?q=x&mode=semantic")

        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"

    def test_search_supports_conditional_get(self, client, session):
        session.add(_make_note("fact", "Needle"))
        session.commit()

        first = client.get("/api/knowledge/public/search?q=needle")
        second = client.get(
            "/api/knowledge/public/search?q=needle",
            headers={"If-None-Match": first.headers["etag"]},
        )

        assert second.status_code == 304


# ---------------------------------------------------------------------------
# GET /api/knowledge/public/facts/daily
# ---------------------------------------------------------------------------


class TestPublicFactsDaily:
    def test_returns_daily_state_totals_and_contradictions(self, client, session):
        now = datetime.now(_UTC)
        verified = _make_note("verified", "Verified")
        verified.observed_at = now - timedelta(days=1)
        unverified = _make_note(
            "unverified", "Unverified", verification_state="unverified"
        )
        unverified.observed_at = now
        disputed = _make_note(
            "disputed", "Disputed", verification_state="disputed", disputed=True
        )
        disputed.observed_at = now
        old = _make_note("old", "Old")
        old.observed_at = now - timedelta(days=31)
        session.add(verified)
        session.add(unverified)
        session.add(disputed)
        session.add(old)
        session.add(
            PublicNoteLink(
                id=1,
                source="verified",
                target="unverified",
                kind="edge",
                edge_type="contradicts",
            )
        )
        session.commit()

        response = client.get("/api/knowledge/public/facts/daily")
        body = response.json()

        assert response.status_code == 200
        assert body["totals"] == {"verified": 2, "unverified": 1, "disputed": 1}
        assert body["contradictions"] == 1
        assert sum(row["verified"] for row in body["daily"]) == 1
        assert sum(row["unverified"] for row in body["daily"]) == 1

    def test_supports_conditional_get(self, client, session):
        first = client.get("/api/knowledge/public/facts/daily")
        second = client.get(
            "/api/knowledge/public/facts/daily",
            headers={"If-None-Match": first.headers["etag"]},
        )

        assert second.status_code == 304
