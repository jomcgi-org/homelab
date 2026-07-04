"""HTTP-level tests for grimoire/router_public.py: the public, no-campaign,
no-grants Grimoire API.

In-memory SQLite + a minimal FastAPI app mounting only the public router,
mirroring the pattern in library_test.py / entities_test.py, but there is no
``campaign``/``as`` query param anywhere here: every request is a bare path
(plus optional q/type/limit/cursor), matching the public contract.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session
from grimoire.models import (
    Book,
    ChunkEntityMention,
    Entity,
    EntityCreature,
    EntitySpell,
    KnowledgeChunk,
    Relationship,
)
from grimoire.router_public import router


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


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _chunk(
    session, *, book_id, chunk_ref, content, seq, section_path=None, image_ref=None
):
    row = KnowledgeChunk(
        book_id=book_id,
        chunk_ref=chunk_ref,
        content=content,
        section_path=section_path,
        image_ref=image_ref,
        seq=seq,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def seed_corpus(session):
    session.add(Book(id="mm", display_name="Monster Manual"))
    c0 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r0",
        content="Aboleths are ancient aberrations.",
        seq=0,
        section_path="Monsters/Aboleth",
    )
    c1 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r1",
        content="Illustration caption: an aboleth.",
        seq=1,
        section_path="Monsters/Aboleth",
        image_ref="s3://grimoire/books/mm/img/aboleth.png",
    )

    aboleth = Entity(id="e-aboleth", entity_type="creature", name="Aboleth")
    fireball = Entity(id="e-fireball", entity_type="spell", name="Fireball")
    session.add_all([aboleth, fireball])
    session.commit()
    session.add(EntityCreature(entity_id="e-aboleth", size="Large", cr=7.0))
    session.add(EntitySpell(entity_id="e-fireball", level=3, school="evocation"))
    session.add(
        ChunkEntityMention(
            chunk_id=c0.id, entity_id="e-aboleth", mention_text="Aboleth"
        )
    )
    session.add(
        Relationship(
            from_entity_id="e-aboleth", to_entity_id="e-fireball", rel_type="knows"
        )
    )
    session.commit()
    return SimpleNamespace(c0=c0, c1=c1, aboleth=aboleth, fireball=fireball)


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False

    def iter_chunks(self, chunk_size=65536):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]

    def close(self):
        self.closed = True


class TestBooksAndSections:
    def test_no_campaign_param_needed(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/books")
        assert r.status_code == 200
        body = r.json()
        assert body[0]["book_id"] == "mm"
        assert body[0]["chunk_count"] == 2

    def test_sections(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/books/mm/sections")
        assert r.status_code == 200
        sections = r.json()
        assert sections[0]["section_path"] == "Monsters/Aboleth"

    def test_read_page_full_content(self, session, client):
        seed = seed_corpus(session)
        r = client.get("/api/grimoire/books/mm/read")
        assert r.status_code == 200
        page = r.json()
        assert page["items"][0]["id"] == seed.c0.id
        # Full content (the reader reconstructs the book), not a preview.
        assert page["items"][0]["content"] == seed.c0.content
        assert page["next_cursor"] is None


class TestGetChunk:
    def test_no_campaign_or_as_required(self, session, client):
        seed = seed_corpus(session)
        r = client.get(f"/api/grimoire/chunks/{seed.c0.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["book_id"] == "mm"
        assert body["seq"] == 0
        assert body["next_id"] == seed.c1.id
        assert {e["name"] for e in body["entities"]} == {"Aboleth"}
        # SQLite round-trips datetimes as naive; assert type, not tzinfo.
        assert isinstance(datetime.fromisoformat(body["created_at"]), datetime)

    def test_missing_chunk_404(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/chunks/nope")
        assert r.status_code == 404


class TestChunkImage:
    def test_streams_image(self, session, client, monkeypatch):
        seed = seed_corpus(session)

        class _FakeClient:
            def get_object(self, Bucket, Key):
                return {"Body": _FakeBody(b"PNGDATA")}

        monkeypatch.setattr("grimoire.ingest.build_s3_client", lambda: _FakeClient())
        r = client.get(f"/api/grimoire/chunks/{seed.c1.id}/image")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content == b"PNGDATA"

    def test_text_chunk_404(self, session, client):
        seed = seed_corpus(session)
        r = client.get(f"/api/grimoire/chunks/{seed.c0.id}/image")
        assert r.status_code == 404


class TestEntities:
    def test_list_no_as_param_required(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/entities")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2

    def test_list_secondary_fields(self, session, client):
        seed_corpus(session)
        body = client.get("/api/grimoire/entities?type=creature").json()
        assert body["items"][0]["size"] == "Large"
        assert body["items"][0]["cr"] == 7.0

        body = client.get("/api/grimoire/entities?type=spell").json()
        assert body["items"][0]["level"] == 3
        assert body["items"][0]["school"] == "evocation"

    def test_get_entity_detail(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/entities/e-aboleth")
        assert r.status_code == 200
        assert r.json().get("size") == "Large"

    def test_get_entity_404(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/entities/nope")
        assert r.status_code == 404

    def test_mentions(self, session, client):
        seed = seed_corpus(session)
        r = client.get("/api/grimoire/entities/e-aboleth/mentions")
        assert r.status_code == 200
        mentions = r.json()
        assert mentions[0]["chunk_id"] == seed.c0.id

    def test_mentions_404_missing_entity(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/entities/nope/mentions")
        assert r.status_code == 404

    def test_relationships(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/entities/e-aboleth/relationships")
        assert r.status_code == 200
        body = r.json()
        assert body[0]["direction"] == "out"
        assert body[0]["entity"]["name"] == "Fireball"

    def test_relationships_404_missing_entity(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/entities/nope/relationships")
        assert r.status_code == 404


class TestSearch:
    def test_search_entities_and_lore(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/search?q=abol")
        assert r.status_code == 200
        body = r.json()
        assert {e["name"] for e in body["entities"]} == {"Aboleth"}

    def test_search_requires_q(self, session, client):
        seed_corpus(session)
        r = client.get("/api/grimoire/search")
        assert r.status_code == 422
