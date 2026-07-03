"""Unit tests for grimoire.visibility: the grant-overlay predicate query and
the scope-projection function every read path shares."""

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from grimoire.models import (
    Campaign,
    Entity,
    EntityCreature,
    EntityLocation,
    EntityNpc,
    KnowledgeGrant,
    PlayerCharacter,
)
from grimoire.visibility import project_entity, visible_entities_query


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


class Seed:
    """Bag of ids/rows created by seed_campaign(), for readable assertions."""


def seed_campaign(session: Session) -> Seed:
    seed = Seed()

    campaign = Campaign(name="The Mighty Nein", dm_name="Matt")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    seed.campaign = campaign

    alice = PlayerCharacter(
        campaign_id=campaign.id, player_name="Joe", character_name="Beau", level=5
    )
    bob = PlayerCharacter(
        campaign_id=campaign.id, player_name="Sam", character_name="Fjord", level=5
    )
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
    creature_detail = EntityCreature(entity_id=creature.id, size="Gargantuan", ac=19)
    session.add(creature_detail)
    seed.creature = creature
    seed.creature_detail = creature_detail

    npc = Entity(entity_type="npc", name="Yasha", is_global=False)
    session.add(npc)
    session.commit()
    session.refresh(npc)
    npc_detail = EntityNpc(entity_id=npc.id, race="Human", disposition="loyal")
    session.add(npc_detail)
    seed.npc = npc
    seed.npc_detail = npc_detail

    location = Entity(entity_type="location", name="Zadash Sewers", is_global=False)
    session.add(location)
    session.commit()
    session.refresh(location)
    location_detail = EntityLocation(entity_id=location.id, location_type="ruin")
    session.add(location_detail)
    seed.location = location
    seed.location_detail = location_detail

    spell = Entity(entity_type="spell", name="Forbiddance", is_global=False)
    session.add(spell)
    session.commit()
    session.refresh(spell)
    seed.spell = spell

    faction = Entity(entity_type="faction", name="The Myriad", is_global=False)
    session.add(faction)
    session.commit()
    session.refresh(faction)
    seed.faction = faction

    session.commit()

    full_grant = KnowledgeGrant(
        campaign_id=campaign.id,
        entity_id=npc.id,
        player_character_id=alice.id,
        grant_scope="full",
    )
    partial_grant = KnowledgeGrant(
        campaign_id=campaign.id,
        entity_id=location.id,
        player_character_id=alice.id,
        grant_scope="partial",
        revealed_details={"note": "seen at night"},
    )
    name_only_grant = KnowledgeGrant(
        campaign_id=campaign.id,
        entity_id=spell.id,
        player_character_id=alice.id,
        grant_scope="name_only",
    )
    session.add(full_grant)
    session.add(partial_grant)
    session.add(name_only_grant)
    session.commit()
    session.refresh(full_grant)
    session.refresh(partial_grant)
    session.refresh(name_only_grant)
    seed.full_grant = full_grant
    seed.partial_grant = partial_grant
    seed.name_only_grant = name_only_grant

    return seed


def _visible_ids(session: Session, campaign_id: str, viewer: str) -> set[str]:
    rows = session.exec(visible_entities_query(campaign_id, viewer)).all()
    return {entity.id for entity, _grant in rows}


def test_alice_sees_global_plus_her_grants(session: Session):
    seed = seed_campaign(session)

    visible = _visible_ids(session, seed.campaign.id, seed.alice.id)

    assert visible == {seed.creature.id, seed.npc.id, seed.location.id, seed.spell.id}
    assert seed.faction.id not in visible


def test_bob_sees_only_global(session: Session):
    seed = seed_campaign(session)

    visible = _visible_ids(session, seed.campaign.id, seed.bob.id)

    assert visible == {seed.creature.id}


def test_dm_sees_everything(session: Session):
    seed = seed_campaign(session)

    visible = _visible_ids(session, seed.campaign.id, "dm")

    assert visible == {
        seed.creature.id,
        seed.npc.id,
        seed.location.id,
        seed.spell.id,
        seed.faction.id,
    }


def test_global_entity_projects_with_detail_for_any_player(session: Session):
    seed = seed_campaign(session)

    for viewer in (seed.alice.id, seed.bob.id):
        result = project_entity(
            seed.creature, seed.creature_detail, grant=None, viewer=viewer
        )
        assert result["name"] == "Umbrasyl"
        assert result["ac"] == 19
        assert result["size"] == "Gargantuan"


def test_full_grant_includes_detail_columns(session: Session):
    seed = seed_campaign(session)

    result = project_entity(
        seed.npc, seed.npc_detail, seed.full_grant, viewer=seed.alice.id
    )

    assert result["name"] == "Yasha"
    assert result["race"] == "Human"
    assert result["disposition"] == "loyal"


def test_partial_grant_excludes_detail_columns(session: Session):
    seed = seed_campaign(session)

    result = project_entity(
        seed.location, seed.location_detail, seed.partial_grant, viewer=seed.alice.id
    )

    assert result["id"] == seed.location.id
    assert result["name"] == "Zadash Sewers"
    assert result["entity_type"] == "location"
    assert result["revealed_details"] == {"note": "seen at night"}
    assert "location_type" not in result


def test_name_only_grant_suppressed_in_lookup_context(session: Session):
    seed = seed_campaign(session)

    result = project_entity(
        seed.spell, None, seed.name_only_grant, viewer=seed.alice.id, context="lookup"
    )

    assert result is None


def test_name_only_grant_stub_in_relationship_context(session: Session):
    seed = seed_campaign(session)

    result = project_entity(
        seed.spell,
        None,
        seed.name_only_grant,
        viewer=seed.alice.id,
        context="relationship",
    )

    assert result == {
        "id": seed.spell.id,
        "name": "Forbiddance",
        "entity_type": "spell",
        "recognition_only": True,
    }


def test_dm_projection_includes_everything_and_grant_annotation(session: Session):
    seed = seed_campaign(session)

    result = project_entity(
        seed.location, seed.location_detail, seed.partial_grant, viewer="dm"
    )

    assert result["name"] == "Zadash Sewers"
    assert result["location_type"] == "ruin"
    assert result["grant"] == {
        "player_character_id": seed.alice.id,
        "grant_scope": "partial",
        "revealed_details": {"note": "seen at night"},
    }

    ungranted = project_entity(seed.faction, None, grant=None, viewer="dm")
    assert ungranted["name"] == "The Myriad"
    assert ungranted["grant"] is None
