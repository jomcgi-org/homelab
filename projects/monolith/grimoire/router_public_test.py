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

from core.db import get_session
from grimoire.models import (
    Adventure,
    Book,
    ChunkEntityMention,
    Entity,
    EntityCreature,
    EntityLocation,
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
    # The reader-path tests (read/chunk/image) need an open-licensed book: the
    # public Reader endpoints 403 copyrighted books. Mark this fixture open so
    # those tests exercise the happy path; the gate itself is covered by
    # TestCopyrightGate below with a separate copyrighted book.
    session.add(Book(id="mm", display_name="Monster Manual", copyrighted_content=False))
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


def seed_adventures(session):
    """One book, six chunks (seq 0..5), two adventures: adv1 spans seq 0-2,
    adv2 spans seq 3 to end of book (end_seq NULL). An entity mentioned in two
    chunks of adv1 (dedup check); another entity only in adv2."""
    session.add(Book(id="cm", display_name="Candlekeep Mysteries"))
    chunks = [
        _chunk(
            session,
            book_id="cm",
            chunk_ref=f"r{i}",
            content=f"chunk {i}",
            seq=i,
        )
        for i in range(6)
    ]

    fistandia = Entity(id="e-fistandia", entity_type="npc", name="Fistandia")
    raven = Entity(id="e-raven", entity_type="npc", name="Book of the Raven")
    session.add_all([fistandia, raven])
    session.commit()
    session.add_all(
        [
            ChunkEntityMention(chunk_id=chunks[0].id, entity_id="e-fistandia"),
            # Same entity mentioned again in a second chunk of the same
            # adventure: the roster must dedup to one row.
            ChunkEntityMention(chunk_id=chunks[1].id, entity_id="e-fistandia"),
            ChunkEntityMention(chunk_id=chunks[4].id, entity_id="e-raven"),
        ]
    )
    session.commit()

    adv1 = Adventure(
        book_id="cm",
        name="The Joy of Extradimensional Spaces",
        seq=1,
        summary="A wizard's tower folds in on itself.",
        level_range="1",
        start_seq=0,
        end_seq=2,
    )
    adv2 = Adventure(
        book_id="cm",
        name="Book of the Raven",
        seq=2,
        start_seq=3,
        end_seq=None,
    )
    session.add_all([adv1, adv2])
    session.commit()
    session.refresh(adv1)
    session.refresh(adv2)
    return SimpleNamespace(chunks=chunks, adv1=adv1, adv2=adv2)


def _seed_adventure(
    session,
    *,
    book_id: str,
    name: str,
    seq: int,
    start_seq: int,
    end_seq: int | None,
    entity_name: str,
    level_range: str | None = None,
):
    """One book with a single adventure spanning [start_seq, end_seq], a chunk
    inside that range, and one is_global entity mentioned in it (so
    entity_count == 1). Returns the new adventure's id. Used by the
    cross-book EXPLORE-gallery tests (list_all_adventures)."""
    session.add(Book(id=book_id, display_name=f"{book_id.upper()} Sourcebook"))
    chunk = _chunk(
        session,
        book_id=book_id,
        chunk_ref="r0",
        content=f"{entity_name} appears here.",
        seq=start_seq,
    )
    entity = Entity(entity_type="npc", name=entity_name, is_global=True)
    session.add(entity)
    session.commit()
    session.refresh(entity)
    session.add(
        ChunkEntityMention(
            chunk_id=chunk.id, entity_id=entity.id, mention_text=entity_name
        )
    )
    adventure = Adventure(
        book_id=book_id,
        name=name,
        seq=seq,
        level_range=level_range,
        start_seq=start_seq,
        end_seq=end_seq,
    )
    session.add(adventure)
    session.commit()
    session.refresh(adventure)
    return adventure.id


def test_list_all_adventures_across_books(client, session):
    # seed two books, each with one adventure and one in-range chunk+entity
    _seed_adventure(
        session,
        book_id="cos",
        name="Curse of Strahd",
        seq=0,
        start_seq=0,
        end_seq=100,
        entity_name="Strahd",
        level_range="1-10",
    )
    _seed_adventure(
        session,
        book_id="lmop",
        name="Lost Mine of Phandelver",
        seq=0,
        start_seq=0,
        end_seq=50,
        entity_name="Gundren",
        level_range="1-5",
    )
    body = client.get("/api/grimoire/adventures").json()
    names = {a["name"] for a in body}
    assert names == {"Curse of Strahd", "Lost Mine of Phandelver"}
    cos = next(a for a in body if a["name"] == "Curse of Strahd")
    assert cos["book_id"] == "cos"
    assert cos["book_display_name"]  # joined from book table
    assert cos["level_range"] == "1-10"
    assert cos["entity_count"] == 1


class TestAdventures:
    def test_list_ordered_with_entity_counts(self, session, client):
        seed = seed_adventures(session)
        body = client.get("/api/grimoire/books/cm/adventures").json()
        assert [a["name"] for a in body] == [
            "The Joy of Extradimensional Spaces",
            "Book of the Raven",
        ]
        # Fistandia mentioned twice in adv1's chunks, but counted once.
        assert body[0]["entity_count"] == 1
        assert body[0]["start_seq"] == 0
        assert body[0]["end_seq"] == 2
        # adv2 has end_seq NULL and one entity mention within its range.
        assert body[1]["entity_count"] == 1
        assert body[1]["end_seq"] is None

    def test_list_empty_for_book_without_adventures(self, session, client):
        seed_corpus(session)
        body = client.get("/api/grimoire/books/mm/adventures").json()
        assert body == []

    def test_detail_roster_respects_seq_boundary_and_dedup(self, session, client):
        seed = seed_adventures(session)
        r = client.get(f"/api/grimoire/adventures/{seed.adv1.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "The Joy of Extradimensional Spaces"
        assert body["book_id"] == "cm"
        assert body["book_display_name"] == "Candlekeep Mysteries"
        # Fistandia appears once despite two mentions within the range.
        assert [e["name"] for e in body["entities"]] == ["Fistandia"]

    def test_detail_end_seq_null_extends_to_end_of_book(self, session, client):
        seed = seed_adventures(session)
        r = client.get(f"/api/grimoire/adventures/{seed.adv2.id}")
        assert r.status_code == 200
        body = r.json()
        # seq=4's mention (Book of the Raven) is captured even though end_seq
        # is NULL, i.e. no upper bound.
        assert [e["name"] for e in body["entities"]] == ["Book of the Raven"]

    def test_detail_404_missing_adventure(self, session, client):
        seed_adventures(session)
        r = client.get("/api/grimoire/adventures/nope")
        assert r.status_code == 404


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
        # section_hierarchy is unset on this fixture, so list_sections falls
        # back to splitting the raw section_path ("Monsters/Aboleth") into a
        # two-level breadcrumb: a "Monsters" chapter node, then its "Aboleth"
        # child, joined with " > " (the section_hierarchy separator).
        assert [s["section_path"] for s in sections] == [
            "Monsters",
            "Monsters > Aboleth",
        ]
        assert sections[1]["title"] == "Aboleth"
        assert sections[1]["depth"] == 1
        assert sections[1]["parent_path"] == "Monsters"
        assert sections[1]["raw_section_paths"] == ["Monsters/Aboleth"]

    def test_read_page_full_content(self, session, client):
        seed = seed_corpus(session)
        r = client.get("/api/grimoire/books/mm/read")
        assert r.status_code == 200
        page = r.json()
        assert page["items"][0]["id"] == seed.c0.id
        # Full content (the reader reconstructs the book), not a preview.
        assert page["items"][0]["content"] == seed.c0.content
        assert page["next_cursor"] is None

    def test_read_page_includes_global_entity_mentions(self, session, client):
        """Each item carries its is_global entity mentions (name, entity_type,
        mention_text), batched across the page rather than per chunk."""
        seed = seed_corpus(session)
        r = client.get("/api/grimoire/books/mm/read")
        assert r.status_code == 200
        page = r.json()
        c0_item = next(item for item in page["items"] if item["id"] == seed.c0.id)
        assert {e["name"] for e in c0_item["entities"]} == {"Aboleth"}
        assert c0_item["entities"][0]["entity_type"] == "creature"
        assert c0_item["entities"][0]["mention_text"] == "Aboleth"
        # A chunk with no mentions still gets an (empty) entities list.
        c1_item = next(item for item in page["items"] if item["id"] == seed.c1.id)
        assert c1_item["entities"] == []

    def test_read_page_excludes_non_global_entity_mentions(self, session, client):
        """A campaign-private entity (is_global=False) mentioned on a chunk
        must NOT appear in the public reader's per-chunk entities."""
        seed = seed_corpus(session)
        session.add(
            Entity(id="e-strahd", entity_type="npc", name="Strahd", is_global=False)
        )
        session.commit()
        session.add(
            ChunkEntityMention(
                chunk_id=seed.c0.id, entity_id="e-strahd", mention_text="Strahd"
            )
        )
        session.commit()
        r = client.get("/api/grimoire/books/mm/read")
        assert r.status_code == 200
        page = r.json()
        c0_item = next(item for item in page["items"] if item["id"] == seed.c0.id)
        assert {e["name"] for e in c0_item["entities"]} == {"Aboleth"}


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


class TestCopyrightGate:
    """The public Reader (text + images) must refuse copyrighted books; only
    open-licensed books are readable in full. Entities/Chat/Explore stay
    corpus-wide and are unaffected (covered by the other classes)."""

    def _seed_copyrighted(self, session):
        # Default copyrighted_content=True: an unclassified book is copyrighted.
        session.add(Book(id="cos", display_name="Curse of Strahd"))
        text = _chunk(
            session, book_id="cos", chunk_ref="r0", content="Strahd broods.", seq=0
        )
        image = _chunk(
            session,
            book_id="cos",
            chunk_ref="r1",
            content="Castle Ravenloft, a map.",
            seq=1,
            image_ref="s3://grimoire/books/cos/img/castle.png",
        )
        return SimpleNamespace(text=text, image=image)

    def test_read_page_403(self, session, client):
        self._seed_copyrighted(session)
        r = client.get("/api/grimoire/books/cos/read")
        assert r.status_code == 403

    def test_get_chunk_403(self, session, client):
        seed = self._seed_copyrighted(session)
        r = client.get(f"/api/grimoire/chunks/{seed.text.id}")
        assert r.status_code == 403

    def test_chunk_image_403(self, session, client):
        seed = self._seed_copyrighted(session)
        r = client.get(f"/api/grimoire/chunks/{seed.image.id}/image")
        assert r.status_code == 403

    def test_missing_book_row_fails_closed(self, session, client):
        # A chunk whose book has no grimoire.book row (unclassified upload) must
        # be treated as copyrighted, never leaked.
        orphan = _chunk(
            session, book_id="ghost", chunk_ref="r0", content="unclassified", seq=0
        )
        assert client.get("/api/grimoire/books/ghost/read").status_code == 403
        assert client.get(f"/api/grimoire/chunks/{orphan.id}").status_code == 403

    def test_list_books_exposes_flag(self, session, client):
        # Open fixture (mm, copyrighted_content=False) + copyrighted (cos).
        seed_corpus(session)
        self._seed_copyrighted(session)
        rows = {b["book_id"]: b for b in client.get("/api/grimoire/books").json()}
        assert rows["mm"]["copyrighted_content"] is False
        assert rows["cos"]["copyrighted_content"] is True


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


class TestExploreGraph:
    def test_adventure_scope_world_lens_excludes_mechanics_and_private(
        self, session, client
    ):
        session.add(Book(id="cos", display_name="Curse of Strahd"))
        chunks = [
            _chunk(
                session, book_id="cos", chunk_ref=f"r{i}", content=f"chunk {i}", seq=i
            )
            for i in range(3)
        ]
        strahd = Entity(entity_type="npc", name="Strahd", is_global=True)
        barovia = Entity(entity_type="location", name="Barovia", is_global=True)
        wizard = Entity(entity_type="class", name="Wizard", is_global=True)
        secret_npc = Entity(entity_type="npc", name="Secret NPC", is_global=False)
        session.add_all([strahd, barovia, wizard, secret_npc])
        session.commit()
        for e in (strahd, barovia, wizard, secret_npc):
            session.refresh(e)
        session.add(EntityLocation(entity_id=barovia.id, region="Barovia Valley"))
        session.add_all(
            [
                ChunkEntityMention(chunk_id=chunks[0].id, entity_id=strahd.id),
                ChunkEntityMention(chunk_id=chunks[1].id, entity_id=barovia.id),
                ChunkEntityMention(chunk_id=chunks[2].id, entity_id=wizard.id),
                # in-range but campaign-private: must never appear publicly.
                ChunkEntityMention(chunk_id=chunks[0].id, entity_id=secret_npc.id),
            ]
        )
        session.add(
            Relationship(
                from_entity_id=strahd.id, to_entity_id=barovia.id, rel_type="LOCATED_IN"
            )
        )
        session.commit()
        adventure = Adventure(
            book_id="cos", name="Into the Mists", seq=0, start_seq=0, end_seq=100
        )
        session.add(adventure)
        session.commit()
        session.refresh(adventure)

        r = client.get(
            f"/api/grimoire/explore/graph?scope=adventure:{adventure.id}&lens=world"
        )
        assert r.status_code == 200
        body = r.json()
        # Wizard is in the adventure's chunk range but excluded by the world
        # lens (mechanics category); the private NPC is in-range too but
        # excluded because it is not is_global.
        assert {n["name"] for n in body["nodes"]} == {"Strahd", "Barovia"}
        assert body["edges"] == [
            {"from": strahd.id, "to": barovia.id, "rel_type": "LOCATED_IN"}
        ]
        barovia_node = next(n for n in body["nodes"] if n["name"] == "Barovia")
        assert barovia_node["entity_type"] == "location"
        assert barovia_node["category"] == "lore"
        assert barovia_node["region"] == "Barovia Valley"
        # this adventure roster has lore (Strahd, Barovia) and mechanics
        # (Wizard) entities but no events/quests, so world/rules are
        # non-empty while story/quests are empty: the lens buttons that
        # should grey out on the public Explore page.
        assert body["lens_counts"] == {
            "world": 2,
            "story": 0,
            "quests": 0,
            "rules": 1,
        }

    def test_everything_scope_has_no_roster_restriction_but_still_applies_lens(
        self, session, client
    ):
        # seed_corpus: Aboleth (creature -> category lore) and Fireball (spell
        # -> category lore, but the "rules" lens unions spells in by
        # entity_type). "everything" scope applies no roster restriction, so
        # the rules lens is what does the filtering: the spell is kept, the
        # creature is dropped.
        seed_corpus(session)
        r = client.get("/api/grimoire/explore/graph?scope=everything&lens=rules")
        assert r.status_code == 200
        body = r.json()
        assert {n["name"] for n in body["nodes"]} == {"Fireball"}

    def test_unknown_adventure_scope_returns_empty_graph(self, session, client):
        r = client.get("/api/grimoire/explore/graph?scope=adventure:nope&lens=world")
        assert r.status_code == 200
        assert r.json() == {
            "nodes": [],
            "edges": [],
            "lens_counts": {"world": 0, "story": 0, "quests": 0, "rules": 0},
        }


class TestExploreEgo:
    def test_drops_private_neighbor_and_its_edge(self, session, client):
        seed_corpus(session)  # e-aboleth --knows--> e-fireball
        secret_goblin = Entity(entity_type="npc", name="Secret Goblin", is_global=False)
        session.add(secret_goblin)
        session.commit()
        session.refresh(secret_goblin)
        session.add(
            Relationship(
                from_entity_id="e-aboleth",
                to_entity_id=secret_goblin.id,
                rel_type="knows",
            )
        )
        session.commit()

        r = client.get("/api/grimoire/explore/ego?id=e-aboleth")
        assert r.status_code == 200
        body = r.json()
        assert {n["name"] for n in body["nodes"]} == {"Aboleth", "Fireball"}
        assert {(e["from"], e["to"]) for e in body["edges"]} == {
            ("e-aboleth", "e-fireball")
        }
        # Aboleth (creature -> lore) and Fireball (spell) are both in the
        # neighborhood; "rules" unions spells in by entity_type, so the
        # rules count is 1 (Fireball) and world's is 1 (Aboleth, lore).
        assert body["lens_counts"] == {
            "world": 1,
            "story": 0,
            "quests": 0,
            "rules": 1,
        }

    def test_missing_or_non_public_focus_returns_empty_graph(self, session, client):
        r = client.get("/api/grimoire/explore/ego?id=nope")
        assert r.status_code == 200
        assert r.json() == {
            "nodes": [],
            "edges": [],
            "lens_counts": {"world": 0, "story": 0, "quests": 0, "rules": 0},
        }

    def test_adventure_scope_drops_neighbor_outside_adventure_keeps_focus(
        self, session, client
    ):
        # Aboleth is the focus and is never filtered out even though it has
        # no adventure roster membership seeded here; Fireball (its only
        # neighbor) is outside the adventure's roster, so an adventure scope
        # should drop it while keeping Aboleth.
        seed_corpus(session)  # e-aboleth --knows--> e-fireball
        book = Book(id="cos", display_name="Curse of Strahd", copyrighted_content=False)
        session.add(book)
        adventure = Adventure(
            id="cos-death-house",
            book_id="cos",
            name="Death House",
            seq=0,
            start_seq=0,
        )
        session.add(adventure)
        session.commit()

        r = client.get(
            "/api/grimoire/explore/ego?id=e-aboleth&scope=adventure:cos-death-house"
        )
        assert r.status_code == 200
        body = r.json()
        assert {n["name"] for n in body["nodes"]} == {"Aboleth"}
        assert body["edges"] == []

    def test_lens_filters_ego_neighbors(self, session, client):
        # Aboleth is a creature (category "lore", not a spell), so the
        # "rules" lens (mechanics category, or entity_type spell) drops it
        # as a neighbor even though the focus itself (Fireball, a spell) is
        # always kept.
        seed_corpus(session)  # e-aboleth --knows--> e-fireball
        r = client.get("/api/grimoire/explore/ego?id=e-fireball&lens=rules")
        assert r.status_code == 200
        body = r.json()
        assert {n["name"] for n in body["nodes"]} == {"Fireball"}
        assert body["edges"] == []


class TestExplorePath:
    def test_two_hop_path_found(self, session, client):
        seed_corpus(session)  # e-aboleth --knows--> e-fireball
        elminster = Entity(entity_type="npc", name="Elminster", is_global=True)
        session.add(elminster)
        session.commit()
        session.refresh(elminster)
        session.add(
            Relationship(
                from_entity_id="e-fireball",
                to_entity_id=elminster.id,
                rel_type="taught_by",
            )
        )
        session.commit()

        r = client.get(f"/api/grimoire/explore/path?from=e-aboleth&to={elminster.id}")
        assert r.status_code == 200
        body = r.json()
        assert [hop["entity"]["name"] for hop in body["path"]] == [
            "Aboleth",
            "Fireball",
            "Elminster",
        ]
        assert [hop["via"] for hop in body["path"]] == [None, "knows", "taught_by"]

    def test_no_path_between_disconnected_entities(self, session, client):
        seed_corpus(session)
        loner = Entity(entity_type="npc", name="Loner", is_global=True)
        session.add(loner)
        session.commit()
        session.refresh(loner)

        r = client.get(f"/api/grimoire/explore/path?from=e-aboleth&to={loner.id}")
        assert r.status_code == 200
        assert r.json() == {"path": []}


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
