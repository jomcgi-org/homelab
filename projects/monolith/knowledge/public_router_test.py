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

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from knowledge.public_models import PublicNote, PublicNoteLink
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
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


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
    )


def _make_link(
    id_: int, source: str, target: str, kind: str = "link"
) -> PublicNoteLink:
    return PublicNoteLink(id=id_, source=source, target=target, kind=kind)


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
        session.add(_make_note("n1", "My Note", content="Body here."))
        session.commit()

        body = client.get("/api/knowledge/public/notes/n1").json()
        assert body["note_id"] == "n1"
        assert body["title"] == "My Note"
        assert body["body"] == "Body here."
        assert "tags" in body
        assert "aliases" in body
        assert "indexed_at" in body

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
