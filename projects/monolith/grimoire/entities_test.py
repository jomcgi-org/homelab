"""Unit tests for the grant-filtered entity read endpoints in
grimoire/router.py: list, single detail, and relationships.

In-memory SQLite + a minimal FastAPI app mounting only the grimoire router,
mirroring the pattern in router_test.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from grimoire.models import (
    Campaign,
    Entity,
    EntityCreature,
    EntityNpc,
    KnowledgeGrant,
    PlayerCharacter,
    Relationship,
)
from grimoire.router import router


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


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


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
    bob = PlayerCharacter(campaign_id=campaign.id, character_name="Bob")
    session.add(alice)
    session.add(bob)
    session.commit()
    session.refresh(alice)
    session.refresh(bob)
    seed.alice = alice
    seed.bob = bob

    creature = Entity(entity_type="creature", name="Umbrasyl", is_global=True)
    session.add(creature)
    session.commit()
    session.refresh(creature)
    session.add(EntityCreature(entity_id=creature.id, size="Gargantuan", ac=19))
    seed.creature = creature

    npc = Entity(entity_type="npc", name="Strahd", is_global=False)
    session.add(npc)
    session.commit()
    session.refresh(npc)
    # Detail row with all-default (None) fields: the single-entity endpoint
    # hydrates it, so the projection carries the typed columns as None.
    session.add(EntityNpc(entity_id=npc.id))
    seed.npc = npc

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
            entity_id=npc.id,
            player_character_id=alice.id,
            grant_scope="full",
        )
    )
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

    session.add(
        Relationship(
            from_entity_id=creature.id, to_entity_id=location.id, rel_type="LAIRS_IN"
        )
    )
    session.add(
        Relationship(from_entity_id=npc.id, to_entity_id=spell.id, rel_type="KNOWS")
    )
    session.add(
        Relationship(
            from_entity_id=location.id,
            to_entity_id=faction.id,
            rel_type="CONTROLLED_BY",
        )
    )
    session.commit()

    return seed


class TestListEntities:
    def test_alice_sees_global_full_and_partial_not_name_only_or_ungranted(
        self, session, client
    ):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities?as={seed.alice.id}"
        )
        assert r.status_code == 200
        names = {item["name"] for item in r.json()["items"]}
        assert names == {"Umbrasyl", "Strahd", "Castle Ravenloft"}

    def test_bob_sees_global_only(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities?as={seed.bob.id}"
        )
        assert r.status_code == 200
        names = {item["name"] for item in r.json()["items"]}
        assert names == {"Umbrasyl"}

    def test_dm_sees_all_with_grant_annotations_aggregated(self, session, client):
        seed = seed_scenario(session)
        r = client.get(f"/api/grimoire/campaigns/{seed.campaign.id}/entities?as=dm")
        assert r.status_code == 200
        body = r.json()
        by_name = {item["name"]: item for item in body["items"]}
        assert set(by_name) == {
            "Umbrasyl",
            "Strahd",
            "Castle Ravenloft",
            "Vampiric Touch",
            "The Keepers of the Feather",
        }
        assert by_name["Umbrasyl"]["grants"] == []
        assert by_name["Strahd"]["grants"] == [
            {
                "player_character_id": seed.alice.id,
                "grant_scope": "full",
                "revealed_details": None,
            }
        ]
        # No duplicate rows for entities with exactly one grant.
        assert len(body["items"]) == 5
        assert body["total"] == 5
        assert body["next_cursor"] is None

    def test_type_filter(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities?as=dm&type=creature"
        )
        assert r.status_code == 200
        names = {item["name"] for item in r.json()["items"]}
        assert names == {"Umbrasyl"}

    def test_q_filter(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities?as=dm&q=cast"
        )
        assert r.status_code == 200
        names = {item["name"] for item in r.json()["items"]}
        assert names == {"Castle Ravenloft"}

    def test_pagination_limit_and_cursor(self, session, client):
        seed = seed_scenario(session)
        base = f"/api/grimoire/campaigns/{seed.campaign.id}/entities?as=dm"

        first = client.get(f"{base}&limit=2")
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["total"] == 5
        assert len(first_body["items"]) == 2
        assert first_body["next_cursor"] is not None

        # Walk every page via the cursor and assert we see all 5 entities once,
        # in the endpoint's name order, with no overlap between pages.
        seen = list(first_body["items"])
        cursor = first_body["next_cursor"]
        while cursor is not None:
            page = client.get(f"{base}&limit=2&cursor={cursor}").json()
            seen.extend(page["items"])
            cursor = page["next_cursor"]
        names = [item["name"] for item in seen]
        assert names == sorted(names)
        assert len(names) == 5
        assert len(set(names)) == 5

    def test_unknown_viewer_pc_404(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities?as=does-not-exist"
        )
        assert r.status_code == 404


class TestGetEntity:
    def test_alice_full_npc_includes_detail(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.npc.id}"
            f"?as={seed.alice.id}"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Strahd"
        assert body["occupation"] is None
        assert body["race"] is None

    def test_alice_partial_location_reveals_details_no_columns(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.location.id}"
            f"?as={seed.alice.id}"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["revealed_details"] == {"note": "a brooding castle on a cliff"}
        assert "location_type" not in body
        assert "source_type" not in body

    def test_alice_name_only_spell_404s(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.spell.id}"
            f"?as={seed.alice.id}"
        )
        assert r.status_code == 404

    def test_alice_ungranted_faction_404s(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.faction.id}"
            f"?as={seed.alice.id}"
        )
        assert r.status_code == 404

    def test_dm_sees_faction_and_spell(self, session, client):
        seed = seed_scenario(session)
        faction_response = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.faction.id}"
            f"?as=dm"
        )
        spell_response = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.spell.id}?as=dm"
        )
        assert faction_response.status_code == 200
        assert spell_response.status_code == 200
        spell_body = spell_response.json()
        assert spell_body["grants"] == [
            {
                "player_character_id": seed.alice.id,
                "grant_scope": "name_only",
                "revealed_details": None,
            }
        ]

    def test_dm_creature_includes_detail_and_empty_grants(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.creature.id}"
            f"?as=dm"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ac"] == 19
        assert body["grants"] == []


class TestEntityRelationships:
    def test_alice_sees_location_edge_from_global_creature(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.creature.id}"
            f"/relationships?as={seed.alice.id}"
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        edge = body[0]
        assert edge["rel_type"] == "LAIRS_IN"
        assert edge["direction"] == "out"
        assert edge["entity"]["name"] == "Castle Ravenloft"

    def test_alice_from_location_omits_faction_and_keeps_recognition_stub(
        self, session, client
    ):
        seed = seed_scenario(session)
        # Give the location a second, incoming edge from the spell so both
        # an omitted (faction) and a recognition-stub (spell) neighbor are
        # exercised from the same center entity. The seeded creature->location
        # LAIRS_IN edge (incoming, creature is global/full-visible) stays
        # visible too, so this center entity has three total edges and only
        # one (the outgoing CONTROLLED_BY to the ungranted faction) drops.
        session.add(
            Relationship(
                from_entity_id=seed.spell.id,
                to_entity_id=seed.location.id,
                rel_type="CAST_AT",
            )
        )
        session.commit()

        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.location.id}"
            f"/relationships?as={seed.alice.id}"
        )
        assert r.status_code == 200
        body = r.json()
        rel_types = {edge["rel_type"] for edge in body}
        assert rel_types == {"LAIRS_IN", "CAST_AT"}

        by_rel_type = {edge["rel_type"]: edge for edge in body}
        lairs_in_edge = by_rel_type["LAIRS_IN"]
        assert lairs_in_edge["direction"] == "in"
        assert lairs_in_edge["entity"]["name"] == "Umbrasyl"

        stub_edge = by_rel_type["CAST_AT"]
        assert stub_edge["direction"] == "in"
        assert stub_edge["entity"]["recognition_only"] is True
        assert stub_edge["entity"]["name"] == "Vampiric Touch"

    def test_bad_viewer_pc_id_404s(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.creature.id}"
            f"/relationships?as=does-not-exist"
        )
        assert r.status_code == 404

    def test_center_not_visible_404s(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.faction.id}"
            f"/relationships?as={seed.alice.id}"
        )
        assert r.status_code == 404

    def test_dm_sees_faction_edge(self, session, client):
        seed = seed_scenario(session)
        r = client.get(
            f"/api/grimoire/campaigns/{seed.campaign.id}/entities/{seed.location.id}"
            f"/relationships?as=dm"
        )
        assert r.status_code == 200
        rel_types = {edge["rel_type"] for edge in r.json()}
        # DM sees every edge from the center: LAIRS_IN (incoming, from the
        # global creature) and CONTROLLED_BY (outgoing, to the ungranted
        # faction that only the DM view can see).
        assert rel_types == {"LAIRS_IN", "CONTROLLED_BY"}
