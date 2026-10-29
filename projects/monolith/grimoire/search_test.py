"""Shared corpus projections through the retained knowledge search implementation.

File-backed SQLite and fixed vector candidates exercise visibility, scoring,
and trimming. HTTP authorization and bounds remain in knowledge_search_test.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import grimoire.search as search_module
from grimoire.models import (
    AppUser,
    Campaign,
    CampaignMember,
    Embedding,
    Entity,
    GameSession,
    KnowledgeChunk,
    KnowledgeGrant,
    PlayerCharacter,
)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'grimoire-search.db'}",
        connect_args={"check_same_thread": False},
    )
    # SQLite can't span schemas, so strip the Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default schema.
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


class FakeEmbedClient:
    async def embed(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0, 0.0]


def _embedding(kind: str, embeddable_id: str) -> Embedding:
    # Not persisted: knn_embeddings is monkeypatched, so this only needs to
    # satisfy Embedding's own required-field validation.
    return Embedding(
        embeddable_kind=kind,
        embeddable_id=embeddable_id,
        model="stub-model",
        dim=4,
        vector=[0.0, 0.0, 0.0, 0.0],
    )


class Seed:
    """Bag of ids/rows created by seed_scenario(), for readable assertions."""


def seed_scenario(session: Session) -> Seed:
    seed = Seed()

    campaign = Campaign(name="Curse of Strahd", dm_name="Joe")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    seed.campaign = campaign

    alice = PlayerCharacter(campaign_id=campaign.id, character_name="Alice")
    session.add(alice)
    session.commit()
    session.refresh(alice)
    seed.alice = alice

    dm_user = AppUser(email="dm@example.test")
    alice_user = AppUser(email="alice@example.test")
    session.add(dm_user)
    session.add(alice_user)
    session.flush()
    session.add(
        CampaignMember(
            campaign_id=campaign.id,
            app_user_id=dm_user.id,
            role="dm",
        )
    )
    session.add(
        CampaignMember(
            campaign_id=campaign.id,
            app_user_id=alice_user.id,
            role="player",
            player_character_id=alice.id,
        )
    )
    game_session = GameSession(campaign_id=campaign.id, status="ended")
    session.add(game_session)
    session.commit()
    session.refresh(game_session)
    seed.alice_email = alice_user.email

    creature = Entity(entity_type="creature", name="Umbrasyl", is_global=True)
    session.add(creature)
    session.commit()
    session.refresh(creature)
    seed.creature = creature

    location = Entity(
        entity_type="location",
        name="Castle Ravenloft",
        is_global=False,
        created_in_session=game_session.id,
    )
    session.add(location)
    session.commit()
    session.refresh(location)
    seed.location = location

    spell = Entity(
        entity_type="spell",
        name="Vampiric Touch",
        is_global=False,
        created_in_session=game_session.id,
    )
    session.add(spell)
    session.commit()
    session.refresh(spell)
    seed.spell = spell

    faction = Entity(
        entity_type="faction",
        name="The Keepers of the Feather",
        is_global=False,
        created_in_session=game_session.id,
    )
    session.add(faction)
    session.commit()
    session.refresh(faction)
    seed.faction = faction

    session.add(
        KnowledgeGrant(
            campaign_id=campaign.id,
            entity_id=location.id,
            player_character_id=alice.id,
            grant_scope="partial",
            revealed_details={"note": "a brooding castle on a cliff"},
        )
    )
    session.add(
        KnowledgeGrant(
            campaign_id=campaign.id,
            entity_id=spell.id,
            player_character_id=alice.id,
            grant_scope="name_only",
        )
    )
    session.commit()

    chunk1 = KnowledgeChunk(
        book_id="phb",
        chunk_ref="phb-c1",
        content="A" * 250,
        section_path="Chapter 1 > Intro",
    )
    chunk2 = KnowledgeChunk(
        book_id="phb",
        chunk_ref="phb-c2",
        content="a much shorter chunk",
        section_path=None,
    )
    session.add(chunk1)
    session.add(chunk2)
    session.commit()
    session.refresh(chunk1)
    session.refresh(chunk2)
    seed.chunk1 = chunk1
    seed.chunk2 = chunk2

    return seed


def _candidates(seed: Seed) -> list[tuple[Embedding, float]]:
    """Fixed candidates ordered by ascending distance (closest first).

    Scores (1 - distance), descending: chunk1 .95, creature .9, location .85
    (partial grant), spell .8 (name_only, dropped for players), faction .75
    (ungranted, dropped for players, visible to the DM), chunk2 .7.
    """
    return [
        (_embedding("chunk", seed.chunk1.id), 0.05),
        (_embedding("entity", seed.creature.id), 0.10),
        (_embedding("entity", seed.location.id), 0.15),
        (_embedding("entity", seed.spell.id), 0.20),
        (_embedding("entity", seed.faction.id), 0.25),
        (_embedding("chunk", seed.chunk2.id), 0.30),
    ]


def _patch_knn(monkeypatch, candidates: list[tuple[Embedding, float]]) -> dict:
    """Monkeypatches knn_embeddings to return ``candidates`` regardless of
    the real query vector, capturing the call kwargs for assertions."""
    captured: dict = {}

    def fake_knn_embeddings(session, query_vector, kinds, limit, **kwargs):
        captured["kinds"] = kinds
        captured["limit"] = limit
        return candidates

    monkeypatch.setattr(search_module, "knn_embeddings", fake_knn_embeddings)
    return captured


def campaign_member(session, seed, viewer):
    predicate = (
        CampaignMember.role == "dm"
        if viewer == "dm"
        else CampaignMember.player_character_id == viewer
    )
    return session.exec(
        select(CampaignMember).where(
            CampaignMember.campaign_id == seed.campaign.id, predicate
        )
    ).one()


class TestSearchKnowledgeCorpus:
    def test_mixed_hits_trimmed_to_k_and_visibility_filtered(
        self, session, monkeypatch
    ):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = asyncio.run(
            search_module.search_knowledge(
                session,
                FakeEmbedClient(),
                seed.campaign.id,
                seed.alice.id,
                campaign_member(session, seed, seed.alice.id),
                "strahd",
                3,
            )
        )
        body = r
        assert len(body) == 3

        kinds = [item["kind"] for item in body]
        assert kinds == ["chunk", "entity", "entity"]

        names_or_ids = [item.get("name") or item["id"] for item in body]
        assert names_or_ids == [seed.chunk1.id, "Umbrasyl", "Castle Ravenloft"]

        scores = [item["score"] for item in body]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == pytest.approx(0.95)
        assert scores[1] == pytest.approx(0.9)
        assert scores[2] == pytest.approx(0.85)

    def test_chunk_hit_shape_and_preview_truncation(self, session, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = asyncio.run(
            search_module.search_knowledge(
                session,
                FakeEmbedClient(),
                seed.campaign.id,
                seed.alice.id,
                campaign_member(session, seed, seed.alice.id),
                "strahd",
                1,
            )
        )
        body = r
        hit = body[0]
        assert hit["kind"] == "chunk"
        assert hit["id"] == seed.chunk1.id
        assert hit["book_id"] == "phb"
        # No grimoire.book row for "phb" in this fixture, so display_name falls
        # back to the book_id (the loader would have upserted a real name).
        assert hit["display_name"] == "phb"
        assert hit["section_path"] == "Chapter 1 > Intro"
        assert len(hit["preview"]) == 200

    def test_name_only_entity_dropped_for_player(self, session, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.spell.id), 0.1)])
        r = asyncio.run(
            search_module.search_knowledge(
                session,
                FakeEmbedClient(),
                seed.campaign.id,
                seed.alice.id,
                campaign_member(session, seed, seed.alice.id),
                "touch",
                10,
            )
        )
        assert r == []

    def test_ungranted_entity_dropped_for_player(self, session, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.faction.id), 0.1)])
        r = asyncio.run(
            search_module.search_knowledge(
                session,
                FakeEmbedClient(),
                seed.campaign.id,
                seed.alice.id,
                campaign_member(session, seed, seed.alice.id),
                "keepers",
                10,
            )
        )
        assert r == []

    def test_partial_grant_returns_revealed_details_only(self, session, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.location.id), 0.1)])
        r = asyncio.run(
            search_module.search_knowledge(
                session,
                FakeEmbedClient(),
                seed.campaign.id,
                seed.alice.id,
                campaign_member(session, seed, seed.alice.id),
                "castle",
                10,
            )
        )
        body = r
        assert len(body) == 1
        hit = body[0]
        assert hit["revealed_details"] == {"note": "a brooding castle on a cliff"}
        assert "location_type" not in hit

    def test_global_entity_returns_full_fields(self, session, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.creature.id), 0.1)])
        r = asyncio.run(
            search_module.search_knowledge(
                session,
                FakeEmbedClient(),
                seed.campaign.id,
                seed.alice.id,
                campaign_member(session, seed, seed.alice.id),
                "dragon",
                10,
            )
        )
        body = r
        assert len(body) == 1
        assert body[0]["name"] == "Umbrasyl"
        assert body[0]["entity_type"] == "creature"

    def test_dm_sees_ungranted_and_name_only_entities(self, session, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = asyncio.run(
            search_module.search_knowledge(
                session,
                FakeEmbedClient(),
                seed.campaign.id,
                "dm",
                campaign_member(session, seed, "dm"),
                "strahd",
                10,
            )
        )
        body = r
        names_or_ids = {item.get("name") or item["id"] for item in body}
        assert names_or_ids == {
            seed.chunk1.id,
            seed.chunk2.id,
            "Umbrasyl",
            "Castle Ravenloft",
            "Vampiric Touch",
            "The Keepers of the Feather",
        }
        faction_hit = next(
            item for item in body if item.get("name") == "The Keepers of the Feather"
        )
        assert faction_hit["kind"] == "entity"
        assert faction_hit["grants"] == []
