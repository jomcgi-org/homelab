"""Unit tests for grimoire/search.py's grant-filtered vector search endpoint.

In-memory SQLite + a minimal FastAPI app mounting only the grimoire router,
mirroring the pattern in entities_test.py/router_test.py. knn_embeddings is
monkeypatched (SQLite has no cosine_distance operator) to return a fixed,
ordered list of (Embedding, distance) candidates so the visibility, scoring,
and trimming behavior can be exercised deterministically; the embedding
client is stubbed via app.dependency_overrides, the same DI seam
knowledge's own tests use.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import grimoire.search as search_module
from core.db import get_session
from grimoire.models import (
    Campaign,
    Embedding,
    Entity,
    KnowledgeChunk,
    KnowledgeGrant,
    PlayerCharacter,
)
from grimoire.router import router
from knowledge.api import get_embedding_client


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbedClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


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

    creature = Entity(entity_type="creature", name="Umbrasyl", is_global=True)
    session.add(creature)
    session.commit()
    session.refresh(creature)
    seed.creature = creature

    location = Entity(entity_type="location", name="Castle Ravenloft", is_global=False)
    session.add(location)
    session.commit()
    session.refresh(location)
    seed.location = location

    spell = Entity(entity_type="spell", name="Vampiric Touch", is_global=False)
    session.add(spell)
    session.commit()
    session.refresh(spell)
    seed.spell = spell

    faction = Entity(
        entity_type="faction", name="The Keepers of the Feather", is_global=False
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

    def fake_knn_embeddings(session, query_vector, kinds, limit):
        captured["kinds"] = kinds
        captured["limit"] = limit
        return candidates

    monkeypatch.setattr(search_module, "knn_embeddings", fake_knn_embeddings)
    return captured


class TestSearchCampaign:
    def test_mixed_hits_trimmed_to_k_and_visibility_filtered(
        self, session, client, monkeypatch
    ):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=strahd&k=3"
        )
        assert r.status_code == 200
        body = r.json()
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

    def test_chunk_hit_shape_and_preview_truncation(self, session, client, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=strahd&k=1"
        )
        assert r.status_code == 200
        body = r.json()
        hit = body[0]
        assert hit["kind"] == "chunk"
        assert hit["id"] == seed.chunk1.id
        assert hit["book_id"] == "phb"
        # No grimoire.book row for "phb" in this fixture, so display_name falls
        # back to the book_id (the loader would have upserted a real name).
        assert hit["display_name"] == "phb"
        assert hit["section_path"] == "Chapter 1 > Intro"
        assert len(hit["preview"]) == 200

    def test_name_only_entity_dropped_for_player(self, session, client, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.spell.id), 0.1)])
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=touch&k=10"
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_ungranted_entity_dropped_for_player(self, session, client, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.faction.id), 0.1)])
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=keepers&k=10"
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_partial_grant_returns_revealed_details_only(
        self, session, client, monkeypatch
    ):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.location.id), 0.1)])
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=castle&k=10"
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        hit = body[0]
        assert hit["revealed_details"] == {"note": "a brooding castle on a cliff"}
        assert "location_type" not in hit

    def test_global_entity_returns_full_fields(self, session, client, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, [(_embedding("entity", seed.creature.id), 0.1)])
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=dragon&k=10"
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "Umbrasyl"
        assert body[0]["entity_type"] == "creature"

    def test_dm_sees_ungranted_and_name_only_entities(
        self, session, client, monkeypatch
    ):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search?as=dm&q=strahd&k=10"
        )
        assert r.status_code == 200
        body = r.json()
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

    def test_k_cap_enforced(self, session, client, monkeypatch):
        seed = seed_scenario(session)
        captured = _patch_knn(monkeypatch, _candidates(seed))

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=strahd&k=100"
        )
        assert r.status_code == 422

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as={seed.alice.id}&q=strahd&k=5"
        )
        assert r.status_code == 200
        assert captured["limit"] == 20
        assert captured["kinds"] == ("entity", "chunk")

    def test_q_required_non_empty(self, session, client, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search?as={seed.alice.id}"
        )
        assert r.status_code == 422

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search?as={seed.alice.id}&q="
        )
        assert r.status_code == 422

    def test_unknown_viewer_404s(self, session, client, monkeypatch):
        seed = seed_scenario(session)
        _patch_knn(monkeypatch, _candidates(seed))

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/search"
            f"?as=does-not-exist&q=strahd"
        )
        assert r.status_code == 404
