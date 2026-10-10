"""Unit tests for grimoire.visibility: the grant-overlay predicate query and
the scope-projection function every read path shares."""

import ast
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session, SQLModel, create_engine

from grimoire.models import (
    Campaign,
    Entity,
    EntityCreature,
    EntityLocation,
    EntityNpc,
    KnowledgeGrant,
    PlayerCharacter,
)
from grimoire.visibility import (
    _SPINE_FIELDS,
    _SPINE_IDENTITY_FIELDS,
    project_entity,
    visible_entities_query,
)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'grimoire-visibility.db'}",
        connect_args={"check_same_thread": False},
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


def test_dm_sees_global_and_current_campaign_entities(session: Session):
    seed = seed_campaign(session)

    visible = _visible_ids(session, seed.campaign.id, "dm")

    assert visible == {
        seed.creature.id,
        seed.npc.id,
        seed.location.id,
        seed.spell.id,
    }
    assert seed.faction.id not in visible


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


# Golden projections shared with the frontend renderer test. A server change
# fails here; a renderer change fails the vitest that reads the same file.
_FIXTURE_DIR = "projects/monolith/frontend/src/lib/grimoire/fixtures"


def _load_fixture(name: str) -> dict:
    rel = f"{_FIXTURE_DIR}/{name}"
    roots = [
        Path(__file__).resolve().parents[3],
        Path(os.environ.get("TEST_SRCDIR", "")) / "_main",
    ]
    for root in roots:
        candidate = root / rel
        if candidate.exists():
            return json.loads(candidate.read_text())
    raise FileNotFoundError(
        f"{rel} not found under {roots} "
        f"(TEST_SRCDIR={os.environ.get('TEST_SRCDIR', '')!r})"
    )


_PROJECTION_FIXTURE = _load_fixture("reveal-projections.json")
_DM_ROUTE_FIXTURE = _load_fixture("dm-only-routes.json")


def test_projection_fixture_spine_matches_server_constants():
    assert tuple(_PROJECTION_FIXTURE["spine_fields"]) == _SPINE_FIELDS
    assert tuple(_PROJECTION_FIXTURE["identity_fields"]) == _SPINE_IDENTITY_FIELDS


@pytest.mark.parametrize(
    "case", _PROJECTION_FIXTURE["cases"], ids=lambda case: case["name"]
)
def test_project_entity_matches_golden_fixture(case):
    fixture = _PROJECTION_FIXTURE
    row = {
        **fixture["entities"][case["entity"]],
        "created_at": datetime.fromisoformat(
            fixture["entities"][case["entity"]]["created_at"]
        ),
    }
    entity = Entity(**row)
    detail = EntityNpc(entity_id=entity.id, **fixture["details"][case["entity"]])
    grant = (
        None
        if case["grant"] is None
        else KnowledgeGrant(
            campaign_id="55555555-5555-4555-8555-555555555555",
            entity_id=entity.id,
            player_character_id=fixture["viewer"],
            **case["grant"],
        )
    )

    result = project_entity(
        entity, detail, grant, viewer=fixture["viewer"], context=case["context"]
    )

    assert jsonable_encoder(result) == case["expected"]


def _dm_guarded_routes() -> set[tuple[str, str]]:
    """(method, path) of every router.py route whose handler calls _require_dm."""
    tree = ast.parse((Path(__file__).parent / "router.py").read_text())
    routes = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        guarded = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_require_dm"
            for call in ast.walk(node)
        )
        if not guarded:
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "router"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                routes.add(
                    (
                        decorator.func.attr.upper(),
                        "/api/grimoire" + decorator.args[0].value,
                    )
                )
    return routes


def test_dm_route_fixture_matches_require_dm_guards():
    """The frontend isolation test's DM-only list is the backend's guard list."""
    guarded = _dm_guarded_routes()
    listed = {tuple(route) for route in _DM_ROUTE_FIXTURE["routes"]}
    assert listed <= guarded, f"not _require_dm routes: {sorted(listed - guarded)}"
    # The BFF reaches grants, sessions and inventory, so every guarded route there
    # must be listed or a new DM route could slip past the player test.
    reachable = {
        route
        for route in guarded
        if "/grants" in route[1]
        or "/inventory" in route[1]
        or route[1].endswith(("/sessions", "/{session_id}"))
    }
    assert reachable <= listed, f"unlisted DM routes: {sorted(reachable - listed)}"
