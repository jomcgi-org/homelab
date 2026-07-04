"""Unit tests for grimoire/public.py: the no-campaign, no-grants read paths
backing the public Grimoire tier.

In-memory SQLite + SQLModel.metadata.create_all fixtures, mirroring the
schema-stripping pattern in library_test.py / entities_test.py. No FastAPI app
here (router_public_test.py covers the HTTP layer); these tests call the pure
functions directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from grimoire import public
from grimoire.models import (
    Book,
    ChunkEntityMention,
    Entity,
    EntityCreature,
    EntityNpc,
    EntitySpell,
    KnowledgeChunk,
    Relationship,
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
    """One book, three chunks, a creature + spell + npc, one relationship."""
    session.add(Book(id="mm", display_name="Monster Manual"))
    c0 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r0",
        content="Aboleths are ancient aberrations lurking in flooded caverns.",
        seq=0,
        section_path="Monsters/Aboleth",
    )
    c1 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r1",
        content="Illustration caption: an aboleth lurking.",
        seq=1,
        section_path="Monsters/Aboleth",
        image_ref="s3://grimoire/books/mm/img/aboleth.png",
    )
    c2 = _chunk(
        session,
        book_id="mm",
        chunk_ref="r2",
        content="Fireball is a classic evocation spell.",
        seq=2,
        section_path="Spells/Fireball",
    )

    aboleth = Entity(id="e-aboleth", entity_type="creature", name="Aboleth")
    fireball = Entity(id="e-fireball", entity_type="spell", name="Fireball")
    strahd = Entity(id="e-strahd", entity_type="npc", name="Strahd", is_global=False)
    session.add_all([aboleth, fireball, strahd])
    session.commit()

    session.add(EntityCreature(entity_id="e-aboleth", size="Large", cr=7.0))
    session.add(EntitySpell(entity_id="e-fireball", level=3, school="evocation"))
    session.add(EntityNpc(entity_id="e-strahd", race="human", occupation="vampire"))
    session.add_all(
        [
            ChunkEntityMention(
                chunk_id=c0.id, entity_id="e-aboleth", mention_text="Aboleth"
            ),
            ChunkEntityMention(
                chunk_id=c1.id, entity_id="e-aboleth", mention_text="aboleth"
            ),
            ChunkEntityMention(
                chunk_id=c2.id, entity_id="e-fireball", mention_text="Fireball"
            ),
        ]
    )
    session.add(
        Relationship(
            from_entity_id="e-strahd", to_entity_id="e-aboleth", rel_type="allied_with"
        )
    )
    session.commit()
    return SimpleNamespace(
        c0=c0, c1=c1, c2=c2, aboleth=aboleth, fireball=fireball, strahd=strahd
    )


class TestGetChunkPublic:
    def test_full_shape_no_grants(self, session):
        seed = seed_corpus(session)
        chunk = public.get_chunk_public(session, seed.c0.id)
        assert chunk["id"] == seed.c0.id
        assert chunk["book_id"] == "mm"
        assert chunk["seq"] == 0
        assert chunk["chunk_count"] == 3
        assert chunk["prev_id"] is None
        assert chunk["next_id"] == seed.c1.id
        assert chunk["image_url"] is None
        assert {e["name"] for e in chunk["entities"]} == {"Aboleth"}
        assert chunk["entities"][0]["mention_text"] == "Aboleth"

    def test_image_url_set(self, session):
        seed = seed_corpus(session)
        chunk = public.get_chunk_public(session, seed.c1.id)
        assert chunk["image_url"] == f"/api/grimoire/chunks/{seed.c1.id}/image"

    def test_missing_chunk_returns_none(self, session):
        seed_corpus(session)
        assert public.get_chunk_public(session, "nope") is None

    def test_no_grant_gating_all_entities_visible(self, session):
        """A non-global, ungranted entity (strahd) still appears if it is
        mentioned on a chunk: the public tier has no grant concept."""
        seed = seed_corpus(session)
        session.add(
            ChunkEntityMention(
                chunk_id=seed.c0.id, entity_id="e-strahd", mention_text="Strahd"
            )
        )
        session.commit()
        chunk = public.get_chunk_public(session, seed.c0.id)
        assert {e["name"] for e in chunk["entities"]} == {"Aboleth", "Strahd"}


class TestListEntitiesPublic:
    def test_all_entities_no_type_filter(self, session):
        seed_corpus(session)
        body = public.list_entities_public(session)
        assert body["total"] == 3
        assert {item["name"] for item in body["items"]} == {
            "Aboleth",
            "Fireball",
            "Strahd",
        }
        assert body["next_cursor"] is None

    def test_secondary_fields_by_type(self, session):
        seed_corpus(session)
        body = public.list_entities_public(session, entity_type="creature")
        assert body["total"] == 1
        aboleth = body["items"][0]
        assert aboleth["size"] == "Large"
        assert aboleth["cr"] == 7.0

        body = public.list_entities_public(session, entity_type="spell")
        fireball = body["items"][0]
        assert fireball["level"] == 3
        assert fireball["school"] == "evocation"

    def test_npc_has_no_secondary_fields(self, session):
        seed_corpus(session)
        body = public.list_entities_public(session, entity_type="npc")
        strahd = body["items"][0]
        assert set(strahd.keys()) == {"id", "name", "entity_type"}

    def test_includes_non_global_entities(self, session):
        """No grant filtering: strahd (is_global=False) still shows up."""
        seed_corpus(session)
        body = public.list_entities_public(session, q="strahd")
        assert body["total"] == 1

    def test_pagination(self, session):
        seed_corpus(session)
        first = public.list_entities_public(session, limit=1)
        assert len(first["items"]) == 1
        assert first["next_cursor"] is not None

        second = public.list_entities_public(
            session, limit=1, cursor=first["next_cursor"]
        )
        assert len(second["items"]) == 1
        assert second["items"][0]["name"] != first["items"][0]["name"]


class TestGetEntityPublic:
    def test_full_detail_no_grant(self, session):
        seed_corpus(session)
        entity = public.get_entity_public(session, "e-aboleth")
        assert entity["name"] == "Aboleth"
        assert entity["size"] == "Large"
        assert entity["cr"] == 7.0

    def test_non_global_entity_still_visible(self, session):
        seed_corpus(session)
        entity = public.get_entity_public(session, "e-strahd")
        assert entity is not None
        assert entity["race"] == "human"

    def test_missing_entity_returns_none(self, session):
        seed_corpus(session)
        assert public.get_entity_public(session, "nope") is None


class TestListRelationshipsPublic:
    def test_both_directions_no_dimming(self, session):
        seed_corpus(session)
        rels = public.list_relationships_public(session, "e-aboleth")
        assert len(rels) == 1
        assert rels[0]["direction"] == "in"
        assert rels[0]["rel_type"] == "allied_with"
        assert rels[0]["entity"]["name"] == "Strahd"

        rels = public.list_relationships_public(session, "e-strahd")
        assert rels[0]["direction"] == "out"
        assert rels[0]["entity"]["name"] == "Aboleth"

    def test_no_relationships_returns_empty(self, session):
        seed_corpus(session)
        assert public.list_relationships_public(session, "e-fireball") == []


class TestSearchPublic:
    def test_entity_name_hit(self, session):
        seed_corpus(session)
        body = public.search_public(session, "abol")
        assert {e["name"] for e in body["entities"]} == {"Aboleth"}

    def test_lore_content_hit(self, session):
        seed_corpus(session)
        body = public.search_public(session, "aberrations")
        assert len(body["lore"]) == 1
        assert body["lore"][0]["display_name"] == "Monster Manual"
        assert body["lore"][0]["book_id"] == "mm"

    def test_no_hits_returns_empty_lists(self, session):
        seed_corpus(session)
        body = public.search_public(session, "xyzzy-no-match")
        assert body == {"entities": [], "lore": []}
