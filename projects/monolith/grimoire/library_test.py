"""Unit tests for grimoire/library.py and the Library/reader endpoints.

In-memory SQLite + a minimal FastAPI app mounting only the grimoire router,
mirroring the schema-stripping + ``app.dependency_overrides[get_session]``
pattern in router_test.py / entities_test.py. Aggregations are asserted both
directly (calling library.*) and through the HTTP endpoints.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from grimoire import library
from grimoire.extract import current_extraction_key
from grimoire.models import (
    Book,
    ChunkEntityMention,
    ChunkExtraction,
    Entity,
    KnowledgeChunk,
)
from grimoire.router import router


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
    session,
    *,
    book_id,
    chunk_ref,
    content,
    seq,
    section_path=None,
    section_hierarchy=None,
    image_ref=None,
):
    row = KnowledgeChunk(
        book_id=book_id,
        chunk_ref=chunk_ref,
        content=content,
        section_path=section_path,
        section_hierarchy=section_hierarchy,
        image_ref=image_ref,
        seq=seq,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def seed_book(session):
    """One book, four chunks across two sections (one image chunk), two entities
    mentioned, partial extraction coverage under the live model+prompt."""
    session.add(Book(id="mm", display_name="Monster Manual"))
    c0 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r0",
        content="Aboleths are ancient aberrations. " * 10,
        seq=0,
        section_path="Monsters/Aboleth",
    )
    c1 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r1",
        content="The aboleth's tentacle attack.",
        seq=1,
        section_path="Monsters/Aboleth",
    )
    c2 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r2",
        content="Illustration caption: an aboleth lurking.",
        seq=2,
        section_path="Monsters/Aboleth",
        image_ref="s3://grimoire/books/mm/img/aboleth.png",
    )
    c3 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r3",
        content="Beholders float and glare.",
        seq=3,
        section_path="Monsters/Beholder",
    )

    aboleth = Entity(id="e-aboleth", entity_type="creature", name="Aboleth")
    beholder = Entity(id="e-beholder", entity_type="creature", name="Beholder")
    session.add_all([aboleth, beholder])
    session.add_all(
        [
            ChunkEntityMention(
                chunk_id=c0.id, entity_id="e-aboleth", mention_text="Aboleth"
            ),
            ChunkEntityMention(
                chunk_id=c1.id, entity_id="e-aboleth", mention_text="aboleth"
            ),
            ChunkEntityMention(
                chunk_id=c3.id, entity_id="e-beholder", mention_text="Beholder"
            ),
        ]
    )

    # Extraction coverage: mark c0 and c1 done under the live (model, version).
    model, prompt_version = current_extraction_key()
    session.add_all(
        [
            ChunkExtraction(
                chunk_id=c0.id, model=model, prompt_version=prompt_version, status="ok"
            ),
            ChunkExtraction(
                chunk_id=c1.id,
                model=model,
                prompt_version=prompt_version,
                status="empty",
            ),
            # A stale-key marker must NOT count toward coverage.
            ChunkExtraction(
                chunk_id=c3.id, model="old/model", prompt_version="v0", status="ok"
            ),
        ]
    )
    session.commit()
    return SimpleNamespace(
        c0=c0, c1=c1, c2=c2, c3=c3, aboleth=aboleth, beholder=beholder
    )


class TestListBooks:
    def test_coverage_counts(self, session, client):
        seed = seed_book(session)
        body = client.get("/api/grimoire/books").json()
        assert len(body) == 1
        book = body[0]
        assert book["book_id"] == "mm"
        assert book["display_name"] == "Monster Manual"
        # book_kind is derived from the slug; the "mm" test slug is unmapped.
        assert book["book_kind"] is None
        assert book["chunk_count"] == 4
        assert book["image_count"] == 1
        # c0 (ok) + c1 (empty) under the live key; the stale-key c3 excluded.
        assert book["extracted_count"] == 2
        assert book["entity_count"] == 2
        assert book["latest_chunk_at"] is not None
        assert book["last_loaded_at"] is not None
        # Only image chunk is c2 (seq 2, below _COVER_MIN_SEQ): falls back to it.
        assert book["cover_chunk_id"] == seed.c2.id


class TestCoverChunk:
    def test_no_images_yields_none_cover(self, session, client):
        session.add(Book(id="empty-book", display_name="No Pictures"))
        session.add(
            KnowledgeChunk(
                book_id="empty-book", chunk_ref="r0", content="Just words.", seq=0
            )
        )
        session.commit()
        body = client.get("/api/grimoire/books").json()
        book = next(b for b in body if b["book_id"] == "empty-book")
        assert book["cover_chunk_id"] is None

    def test_prefers_seq_at_or_past_min_over_earlier_image(self, session, client):
        """An image at seq 1 (front matter) loses to a later image at seq 5,
        even though seq 1 comes first in reading order."""
        session.add(Book(id="illustrated", display_name="Illustrated Tome"))
        front_matter = _chunk(
            session,
            book_id="illustrated",
            chunk_ref="r1",
            content="Title page scan.",
            seq=1,
            image_ref="s3://grimoire/books/illustrated/img/title.png",
        )
        art = _chunk(
            session,
            book_id="illustrated",
            chunk_ref="r5",
            content="A proper illustration.",
            seq=5,
            image_ref="s3://grimoire/books/illustrated/img/art.png",
        )
        body = client.get("/api/grimoire/books").json()
        book = next(b for b in body if b["book_id"] == "illustrated")
        assert book["cover_chunk_id"] == art.id
        assert book["cover_chunk_id"] != front_matter.id

    def test_falls_back_to_earliest_image_when_none_past_min_seq(self, session, client):
        session.add(Book(id="only-front-matter", display_name="Sparse Tome"))
        only_image = _chunk(
            session,
            book_id="only-front-matter",
            chunk_ref="r1",
            content="The only art in this book, alas.",
            seq=1,
            image_ref="s3://grimoire/books/only-front-matter/img/cover.png",
        )
        body = client.get("/api/grimoire/books").json()
        book = next(b for b in body if b["book_id"] == "only-front-matter")
        assert book["cover_chunk_id"] == only_image.id


class TestSections:
    def test_ordered_by_reading_order(self, session, client):
        """seed_book sets only section_path (no section_hierarchy), so every
        chunk falls back to splitting it on "/", collapsing "Monsters/Aboleth"
        and "Monsters/Beholder" under a shared "Monsters" chapter node."""
        seed = seed_book(session)
        body = client.get("/api/grimoire/books/mm/sections").json()
        assert [s["section_path"] for s in body] == [
            "Monsters",
            "Monsters > Aboleth",
            "Monsters > Beholder",
        ]
        chapter, aboleth, beholder = body
        assert chapter["depth"] == 0
        assert chapter["parent_path"] is None
        # The chapter node has no chunks tagged with its OWN full breadcrumb
        # (nothing is section_path="Monsters" alone), but still gets a
        # first_chunk_id (the earliest descendant chunk) so it's clickable.
        assert chapter["chunk_count"] == 0
        assert chapter["first_chunk_id"] == seed.c0.id

        assert aboleth["title"] == "Aboleth"
        assert aboleth["depth"] == 1
        assert aboleth["parent_path"] == "Monsters"
        assert aboleth["chunk_count"] == 3
        assert aboleth["image_count"] == 1
        assert aboleth["first_chunk_id"] == seed.c0.id
        assert aboleth["raw_section_paths"] == ["Monsters/Aboleth"]

        assert beholder["title"] == "Beholder"
        assert beholder["first_chunk_id"] == seed.c3.id

    def test_nests_from_section_hierarchy_when_present(self, session, client):
        """A chunk with section_hierarchy set is grouped by its full breadcrumb
        (split on " > "), not by section_path, and can nest deeper than two
        levels."""
        session.add(Book(id="deep", display_name="Deep Book"))
        c0 = _chunk(
            session,
            book_id="deep",
            chunk_ref="d0",
            content="Vulnerability details.",
            seq=0,
            section_path="Chapter 3/Armor",
            section_hierarchy="Chapter 3: Magic Items > Armor > Armor of Vulnerability",
        )
        body = client.get("/api/grimoire/books/deep/sections").json()
        assert [s["section_path"] for s in body] == [
            "Chapter 3: Magic Items",
            "Chapter 3: Magic Items > Armor",
            "Chapter 3: Magic Items > Armor > Armor of Vulnerability",
        ]
        leaf = body[2]
        assert leaf["title"] == "Armor of Vulnerability"
        assert leaf["depth"] == 2
        assert leaf["parent_path"] == "Chapter 3: Magic Items > Armor"
        assert leaf["first_chunk_id"] == c0.id
        # raw_section_paths tracks the chunk-level column for the reader's
        # scroll-highlight match, independent of the section_hierarchy nesting.
        assert leaf["raw_section_paths"] == ["Chapter 3/Armor"]

    def test_falls_back_per_chunk_when_hierarchy_missing(self, session, client):
        """Within one book, chunks with section_hierarchy nest by breadcrumb
        while chunks missing it (pre-backfill) fall back to section_path,
        matching the mixed-coverage case (e.g. monster-manual, 1222/2028)."""
        session.add(Book(id="mixed", display_name="Mixed Coverage"))
        with_hierarchy = _chunk(
            session,
            book_id="mixed",
            chunk_ref="m0",
            content="Has hierarchy.",
            seq=0,
            section_path="Ch1/Intro",
            section_hierarchy="Chapter 1 > Introduction",
        )
        without_hierarchy = _chunk(
            session,
            book_id="mixed",
            chunk_ref="m1",
            content="Missing hierarchy.",
            seq=1,
            section_path="Ch2/Setup",
            section_hierarchy=None,
        )
        body = client.get("/api/grimoire/books/mixed/sections").json()
        paths = [s["section_path"] for s in body]
        assert "Chapter 1 > Introduction" in paths
        assert "Ch2 > Setup" in paths
        by_path = {s["section_path"]: s for s in body}
        assert (
            by_path["Chapter 1 > Introduction"]["first_chunk_id"] == with_hierarchy.id
        )
        assert by_path["Ch2 > Setup"]["first_chunk_id"] == without_hierarchy.id


class TestListChunks:
    def test_pagination_and_section_filter(self, session, client):
        seed = seed_book(session)
        first = client.get("/api/grimoire/books/mm/chunks?limit=2").json()
        assert [c["seq"] for c in first["items"]] == [0, 1]
        assert first["next_cursor"] == "1"
        assert first["items"][0]["kind"] == "text"

        second = client.get(
            f"/api/grimoire/books/mm/chunks?limit=2&cursor={first['next_cursor']}"
        ).json()
        assert [c["seq"] for c in second["items"]] == [2, 3]
        assert second["next_cursor"] is None
        # The image chunk reports kind=image.
        assert second["items"][0]["kind"] == "image"

        section = client.get(
            "/api/grimoire/books/mm/chunks?section=Monsters/Beholder"
        ).json()
        assert [c["id"] for c in section["items"]] == [seed.c3.id]


class TestReadBook:
    def test_full_content_and_image_key(self, session, client):
        seed = seed_book(session)
        page = client.get("/api/grimoire/books/mm/read").json()
        assert [c["seq"] for c in page["items"]] == [0, 1, 2, 3]
        assert page["next_cursor"] is None
        first = page["items"][0]
        # Full content, not the 200-char list preview.
        assert first["content"] == seed.c0.content
        assert len(first["content"]) > 200
        assert first["image_key"] is None
        img = page["items"][2]
        assert img["kind"] == "image"
        # Bucket-relative object key, ready for imgproxy signing server-side.
        assert img["image_key"] == "books/mm/img/aboleth.png"

    def test_keyset_pagination(self, session, client):
        seed_book(session)
        first = client.get("/api/grimoire/books/mm/read?limit=3").json()
        assert [c["seq"] for c in first["items"]] == [0, 1, 2]
        assert first["next_cursor"] == "2"
        second = client.get(
            f"/api/grimoire/books/mm/read?limit=3&cursor={first['next_cursor']}"
        ).json()
        assert [c["seq"] for c in second["items"]] == [3]
        assert second["next_cursor"] is None

    def test_malformed_image_ref_degrades_to_no_key(self):
        assert library._image_object_key(None) is None
        assert library._image_object_key("https://x/y.png") is None
        assert library._image_object_key("s3://bucket-only") is None


class TestGetChunk:
    def test_content_neighbours_and_image_url(self, session, client):
        seed = seed_book(session)
        body = client.get(f"/api/grimoire/chunks/{seed.c2.id}?campaign=none&as=dm")
        # campaign "none" does not exist -> 404 before viewpoint resolution.
        assert body.status_code == 404

        campaign = client.post("/api/grimoire/campaigns", json={"name": "C"}).json()
        chunk = client.get(
            f"/api/grimoire/chunks/{seed.c2.id}?campaign={campaign['id']}&as=dm"
        ).json()
        assert chunk["seq"] == 2
        assert chunk["prev_id"] == seed.c1.id
        assert chunk["next_id"] == seed.c3.id
        assert chunk["image_url"] == f"/api/grimoire/chunks/{seed.c2.id}/image"
        assert chunk["content"].startswith("Illustration caption")
        # seed_book seeds 4 chunks (c0-c3) for book "mm".
        assert chunk["chunk_count"] == 4

        first = client.get(
            f"/api/grimoire/chunks/{seed.c0.id}?campaign={campaign['id']}&as=dm"
        ).json()
        assert first["prev_id"] is None
        assert first["image_url"] is None
        assert {e["name"] for e in first["entities"]} == {"Aboleth"}

    def test_missing_chunk_404(self, session, client):
        campaign = client.post("/api/grimoire/campaigns", json={"name": "C"}).json()
        r = client.get(f"/api/grimoire/chunks/nope?campaign={campaign['id']}&as=dm")
        assert r.status_code == 404

    def test_on_page_entities_respect_viewpoint(self, session, client):
        seed = seed_book(session)
        # Non-global entity, ungranted to the player: dropped from the chips.
        seed.aboleth.is_global = False
        session.add(seed.aboleth)
        session.commit()

        campaign = client.post("/api/grimoire/campaigns", json={"name": "C"}).json()
        pc = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/characters",
            json={"character_name": "Rogue"},
        ).json()

        chunk = client.get(
            f"/api/grimoire/chunks/{seed.c0.id}?campaign={campaign['id']}&as={pc['id']}"
        ).json()
        assert chunk["entities"] == []

        # The DM still sees it.
        dm = client.get(
            f"/api/grimoire/chunks/{seed.c0.id}?campaign={campaign['id']}&as=dm"
        ).json()
        assert {e["name"] for e in dm["entities"]} == {"Aboleth"}


class TestMentions:
    def test_dm_sees_sources(self, session, client):
        seed = seed_book(session)
        campaign = client.post("/api/grimoire/campaigns", json={"name": "C"}).json()
        body = client.get(
            f"/api/grimoire/campaigns/{campaign['id']}/entities/e-aboleth/mentions?as=dm"
        ).json()
        chunk_ids = {m["chunk_id"] for m in body}
        assert chunk_ids == {seed.c0.id, seed.c1.id}
        assert all(m["book_id"] == "mm" for m in body)

    def test_player_without_grant_404(self, session, client):
        seed = seed_book(session)
        seed.aboleth.is_global = False
        session.add(seed.aboleth)
        session.commit()
        campaign = client.post("/api/grimoire/campaigns", json={"name": "C"}).json()
        pc = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/characters",
            json={"character_name": "Rogue"},
        ).json()
        r = client.get(
            f"/api/grimoire/campaigns/{campaign['id']}/entities/e-aboleth/mentions"
            f"?as={pc['id']}"
        )
        assert r.status_code == 404


class TestRenameBook:
    def test_rename_and_404(self, session, client):
        seed_book(session)
        r = client.patch(
            "/api/grimoire/books/mm", json={"display_name": "The Monster Manual"}
        )
        assert r.status_code == 200
        assert r.json().get("display_name") == "The Monster Manual"
        assert session.get(Book, "mm").display_name == "The Monster Manual"

        missing = client.patch("/api/grimoire/books/nope", json={"display_name": "X"})
        assert missing.status_code == 404

        empty = client.patch("/api/grimoire/books/mm", json={"display_name": "  "})
        assert empty.status_code == 422


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False

    def iter_chunks(self, chunk_size=65536):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]

    def close(self):
        self.closed = True


class TestChunkImage:
    def test_streams_image(self, session, client, monkeypatch):
        seed = seed_book(session)
        captured = {}

        class _FakeClient:
            def get_object(self, Bucket, Key):
                captured["bucket"] = Bucket
                captured["key"] = Key
                return {"Body": _FakeBody(b"PNGDATA")}

        monkeypatch.setattr("grimoire.ingest.build_s3_client", lambda: _FakeClient())
        r = client.get(f"/api/grimoire/chunks/{seed.c2.id}/image")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content == b"PNGDATA"
        assert captured["bucket"] == "grimoire"
        assert captured["key"] == "books/mm/img/aboleth.png"

    def test_text_chunk_404(self, session, client):
        seed = seed_book(session)
        r = client.get(f"/api/grimoire/chunks/{seed.c0.id}/image")
        assert r.status_code == 404
