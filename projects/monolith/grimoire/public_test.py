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
    # A global npc with no relationships, for the npc-secondary and
    # no-relationships cases.
    volo = Entity(id="e-volo", entity_type="npc", name="Volo")
    # A campaign-private entity: is_global=False. The public tier must never
    # expose it (list, detail, search, chunk mentions, relationships).
    strahd = Entity(id="e-strahd", entity_type="npc", name="Strahd", is_global=False)
    session.add_all([aboleth, fireball, volo, strahd])
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
    session.add_all(
        [
            # global -> global: the visible edge the direction test asserts.
            Relationship(
                from_entity_id="e-aboleth",
                to_entity_id="e-fireball",
                rel_type="summons",
            ),
            # non-global source -> global: must be filtered out of the public view.
            Relationship(
                from_entity_id="e-strahd",
                to_entity_id="e-aboleth",
                rel_type="allied_with",
            ),
        ]
    )
    session.commit()
    return SimpleNamespace(
        c0=c0,
        c1=c1,
        c2=c2,
        aboleth=aboleth,
        fireball=fireball,
        volo=volo,
        strahd=strahd,
    )


def seed_degree_corpus(session):
    """Three global npcs with distinct relationship degrees (Zeta=3, Mu=1,
    Alpha=0). Names are chosen so degree order (Zeta, Mu, Alpha) is the REVERSE
    of alphabetical order (Alpha, Mu, Zeta), so a degree-ordered list is
    distinguishable from a name-ordered one. Zeta's degree is padded with edges
    to two non-global neighbours, which never appear in the (is_global) list."""
    session.add_all(
        [
            Entity(id="e-zeta", entity_type="npc", name="Zeta"),
            Entity(id="e-mu", entity_type="npc", name="Mu"),
            Entity(id="e-alpha", entity_type="npc", name="Alpha"),
            Entity(id="e-nx", entity_type="npc", name="Nx", is_global=False),
            Entity(id="e-ny", entity_type="npc", name="Ny", is_global=False),
        ]
    )
    session.commit()
    session.add_all(
        [
            Relationship(
                from_entity_id="e-zeta", to_entity_id="e-mu", rel_type="knows"
            ),
            Relationship(
                from_entity_id="e-zeta", to_entity_id="e-nx", rel_type="knows"
            ),
            Relationship(
                from_entity_id="e-zeta", to_entity_id="e-ny", rel_type="knows"
            ),
        ]
    )
    session.commit()


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

    def test_non_global_mention_excluded(self, session):
        """A campaign-private entity (strahd, is_global=False) mentioned on a
        chunk must NOT appear in the public on-page chips."""
        seed = seed_corpus(session)
        session.add(
            ChunkEntityMention(
                chunk_id=seed.c0.id, entity_id="e-strahd", mention_text="Strahd"
            )
        )
        session.commit()
        chunk = public.get_chunk_public(session, seed.c0.id)
        assert {e["name"] for e in chunk["entities"]} == {"Aboleth"}


class TestListEntitiesPublic:
    def test_all_entities_no_type_filter(self, session):
        seed_corpus(session)
        body = public.list_entities_public(session)
        # Three global entities; strahd (is_global=False) is excluded.
        assert body["total"] == 3
        assert {item["name"] for item in body["items"]} == {
            "Aboleth",
            "Fireball",
            "Volo",
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
        # Only the global npc (Volo); the non-global Strahd is excluded.
        assert {item["name"] for item in body["items"]} == {"Volo"}
        assert set(body["items"][0].keys()) == {"id", "name", "entity_type"}

    def test_excludes_non_global_entities(self, session):
        """A campaign-private entity (strahd, is_global=False) is never listed."""
        seed_corpus(session)
        body = public.list_entities_public(session, q="strahd")
        assert body["total"] == 0
        assert body["items"] == []

    def test_default_orders_by_degree_desc(self, session):
        """No q, no type filter -> most-connected first. Zeta (degree 3) leads,
        then Mu (1), then Alpha (0), the reverse of alphabetical order."""
        seed_degree_corpus(session)
        body = public.list_entities_public(session)
        assert body["total"] == 3
        assert [item["name"] for item in body["items"]] == ["Zeta", "Mu", "Alpha"]
        assert body["next_cursor"] is None

    def test_type_filter_reverts_to_name_order(self, session):
        """A type filter switches ordering back to alphabetical by name."""
        seed_degree_corpus(session)
        body = public.list_entities_public(session, entity_type="npc")
        assert [item["name"] for item in body["items"]] == ["Alpha", "Mu", "Zeta"]

    def test_search_reverts_to_name_order(self, session):
        """A search filter switches ordering back to alphabetical by name.
        'a' matches Alpha and Zeta (not Mu); degree order would be [Zeta, Alpha]
        but name order is [Alpha, Zeta]."""
        seed_degree_corpus(session)
        body = public.list_entities_public(session, q="a")
        assert [item["name"] for item in body["items"]] == ["Alpha", "Zeta"]

    def test_degree_mode_offset_pagination(self, session):
        """Degree mode paginates by a stringified integer offset cursor."""
        seed_degree_corpus(session)
        first = public.list_entities_public(session, limit=2)
        assert [item["name"] for item in first["items"]] == ["Zeta", "Mu"]
        assert first["next_cursor"] == "2"
        second = public.list_entities_public(
            session, limit=2, cursor=first["next_cursor"]
        )
        assert [item["name"] for item in second["items"]] == ["Alpha"]
        assert second["next_cursor"] is None

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

    def test_created_in_session_not_in_payload(self, session):
        """The private game-session id must never ship in a public payload."""
        seed_corpus(session)
        entity = public.get_entity_public(session, "e-aboleth")
        assert "created_in_session" not in entity

    def test_non_global_entity_hidden(self, session):
        """A campaign-private entity (is_global=False) 404s: a guessed id cannot
        fetch it."""
        seed_corpus(session)
        assert public.get_entity_public(session, "e-strahd") is None

    def test_missing_entity_returns_none(self, session):
        seed_corpus(session)
        assert public.get_entity_public(session, "nope") is None


class TestListRelationshipsPublic:
    def test_both_directions_no_dimming(self, session):
        seed_corpus(session)
        # Aboleth: outgoing summons -> Fireball is visible; the incoming edge from
        # the non-global Strahd is filtered out.
        rels = public.list_relationships_public(session, "e-aboleth")
        assert len(rels) == 1
        assert rels[0]["direction"] == "out"
        assert rels[0]["rel_type"] == "summons"
        assert rels[0]["entity"]["name"] == "Fireball"

        # Fireball sees the same edge from the other side.
        rels = public.list_relationships_public(session, "e-fireball")
        assert len(rels) == 1
        assert rels[0]["direction"] == "in"
        assert rels[0]["entity"]["name"] == "Aboleth"

    def test_non_global_neighbor_filtered(self, session):
        """A public entity does not surface an edge to a campaign-private
        neighbor (Aboleth's incoming edge from the non-global Strahd)."""
        seed_corpus(session)
        rels = public.list_relationships_public(session, "e-aboleth")
        assert all(r["entity"]["name"] != "Strahd" for r in rels)

    def test_non_global_source_returns_empty(self, session):
        """Relationships for a campaign-private id return nothing, so a guessed
        id cannot reveal its edges."""
        seed_corpus(session)
        assert public.list_relationships_public(session, "e-strahd") == []

    def test_no_relationships_returns_empty(self, session):
        seed_corpus(session)
        # Volo is a global entity with no edges.
        assert public.list_relationships_public(session, "e-volo") == []


class TestSearchPublic:
    def test_entity_name_hit(self, session):
        seed_corpus(session)
        body = public.search_public(session, "abol")
        assert {e["name"] for e in body["entities"]} == {"Aboleth"}

    def test_search_excludes_non_global(self, session):
        """Name search never returns a campaign-private entity."""
        seed_corpus(session)
        body = public.search_public(session, "strahd")
        assert body["entities"] == []

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
