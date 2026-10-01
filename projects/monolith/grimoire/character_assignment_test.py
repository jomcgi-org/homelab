"""Character seating uses campaign roles and leaves PC-owned data intact."""

from __future__ import annotations

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlmodel import select

from grimoire.models import (
    CampaignMember,
    CharacterSheetVersion,
    KnowledgeGrant,
    PlayerCharacter,
)
from grimoire.testing.leak_harness import sqlite_harness


@pytest.fixture
def harness(tmp_path):
    with (
        sqlite_harness(tmp_path / "assignment.db") as h,
        TestClient(h.app()) as client,
    ):
        yield h, client


def assign(h, client, body, *, caller="dm", member="member_no_character"):
    return client.put(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
        f"/members/{h.rows[member].id}/character",
        headers=h.headers(caller),
        json=body,
    )


def create_self(h, client, *, caller="no_character", body=None):
    return client.post(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/characters/self",
        headers=h.headers(caller),
        json=body if body is not None else {"name": "  Elowen  "},
    )


def unassigned_character(h):
    character = PlayerCharacter(
        campaign_id=h.rows["campaign"].id, character_name="Unassigned PC"
    )
    h.session.add(character)
    h.session.commit()
    return character


def test_dm_assigns_existing_character_and_same_assignment_is_noop(harness):
    h, client = harness
    character = unassigned_character(h)
    response = assign(h, client, {"player_character_id": character.id})
    assert response.status_code == 200, response.text
    assert response.json()["player_character_id"] == character.id
    assert response.json()["character_name"] == "Unassigned PC"
    h.session.expire_all()
    assert h.rows["member_no_character"].player_character_id == character.id
    before = h.snapshot()
    repeated = assign(h, client, {"player_character_id": character.id})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == response.json()
    assert h.snapshot() == before


@pytest.mark.parametrize("body_kind", ("existing", "new", "clear"))
@pytest.mark.parametrize(
    "caller", ("player_a", "no_character", "outsider", "other_campaign")
)
def test_assignment_requires_dm_of_this_campaign(harness, caller, body_kind):
    h, client = harness
    character = unassigned_character(h)
    body = {
        "existing": {"player_character_id": character.id},
        "new": {"new": {"name": "Forbidden PC"}},
        "clear": {"player_character_id": None},
    }[body_kind]
    # This user becomes a DM through the real create-campaign endpoint, yet
    # still cannot mutate the table they joined as a player.
    if caller == "player_a":
        created = client.post(
            "/api/grimoire/campaigns",
            headers=h.headers(caller),
            json={"name": "Player's own campaign"},
        )
        assert created.status_code == 200, created.text
    before = h.snapshot()
    response = assign(h, client, body, caller=caller, member="member")
    assert response.status_code == (
        404 if caller in ("outsider", "other_campaign") else 403
    ), response.text
    h.assert_no_leak(response, caller)
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "member_key", ("member_dm", "member_other_player", "member_other_campaign")
)
def test_assignment_target_must_be_player_in_this_campaign(harness, member_key):
    h, client = harness
    character = unassigned_character(h)
    before = h.snapshot()
    response = assign(
        h, client, {"player_character_id": character.id}, member=member_key
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "player membership not found"
    assert h.snapshot() == before


def test_assignment_missing_member(harness):
    h, client = harness
    before = h.snapshot()
    response = client.put(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/members/missing/character",
        headers=h.headers("dm"),
        json={"new": {"name": "Must not be created"}},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "player membership not found"
    assert h.snapshot() == before


@pytest.mark.parametrize("character_key", ("other_character", "missing"))
def test_assignment_rejects_foreign_or_missing_character_without_writes(
    harness, character_key
):
    h, client = harness
    character_id = h.rows[character_key].id if character_key != "missing" else "missing"
    before = h.snapshot()
    response = assign(h, client, {"player_character_id": character_id})
    assert response.status_code == 404, response.text
    assert h.snapshot() == before


def test_assignment_known_conflict_rejected_before_commit(harness, monkeypatch):
    h, client = harness
    before = h.snapshot()

    def must_not_commit():
        pytest.fail("known character conflict must be checked before commit")

    monkeypatch.setattr(h.session, "commit", must_not_commit)
    response = assign(
        h, client, {"player_character_id": h.rows["character_a"].id}, member="member"
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "character already assigned"
    assert h.snapshot() == before
    assert h.rows["member_player_a"].player_character_id == h.rows["character_a"].id
    assert h.rows["member"].player_character_id == h.rows["character"].id


def test_assignment_commit_integrity_race_rolls_back(harness, monkeypatch):
    h, client = harness
    before = h.snapshot()
    original_commit = h.session.commit

    def concurrent_conflict():
        # Insert the competing link after the route's precheck. SQLite enforces
        # the real unique constraint at flush even though it has no row locks.
        h.rows["member"].player_character_id = h.rows[
            "member_no_character"
        ].player_character_id
        original_commit()

    monkeypatch.setattr(h.session, "commit", concurrent_conflict)
    response = assign(h, client, {"new": {"name": "Rolled back PC"}})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "character already assigned"
    assert h.snapshot() == before


@pytest.mark.parametrize("route", ("assign", "self"))
def test_character_creation_locks_and_refreshes_membership(harness, monkeypatch, route):
    h, client = harness
    original_exec = h.session.exec
    locked = []

    def record_locks(statement, *args, **kwargs):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "FOR NO KEY UPDATE" in sql or "FOR UPDATE" in sql:
            locked.append((sql, statement.get_execution_options()))
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(h.session, "exec", record_locks)
    response = (
        assign(h, client, {"new": {"name": "Locked PC"}})
        if route == "assign"
        else create_self(h, client)
    )
    assert response.status_code == 200, response.text
    if route == "assign":
        assert len(locked) == 2
        assert "FOR NO KEY UPDATE" in locked[0][0]
        assert "FROM campaign \n" in locked[0][0]
        assert "campaign.id =" in locked[0][0]
    else:
        assert len(locked) == 1
    member_sql, options = locked[-1]
    assert "FROM campaign_member \n" in member_sql
    assert "campaign_member.id =" in member_sql
    assert options["populate_existing"] is True


@pytest.mark.parametrize(
    "body",
    (
        {},
        {"player_character_id": None, "new": {"name": "Both"}},
        {"player_character_id": None, "new": None},
        {"new": None},
        {"player_character_id": None, "extra": "forbidden"},
        {"new": {"name": "Name", "extra": "forbidden"}},
        {"new": {"name": "   "}},
        {"new": {"name": "x" * 121}},
        {"new": {}},
    ),
)
def test_assignment_rejects_invalid_body_without_writes(harness, body):
    h, client = harness
    before = h.snapshot()
    response = assign(h, client, body)
    assert response.status_code == 422, response.text
    assert h.snapshot() == before


def test_replacement_leaves_old_character_unassigned(harness):
    h, client = harness
    old_id = h.rows["character"].id
    response = assign(h, client, {"new": {"name": "Replacement"}}, member="member")
    assert response.status_code == 200, response.text
    assert response.json()["player_character_id"] != old_id
    assert response.json()["character_name"] == "Replacement"
    assert h.session.get(PlayerCharacter, old_id) is not None
    assert (
        h.session.exec(
            select(CampaignMember).where(CampaignMember.player_character_id == old_id)
        ).all()
        == []
    )
    transferred = assign(h, client, {"player_character_id": old_id})
    assert transferred.status_code == 200, transferred.text
    h.session.expire_all()
    assert h.rows["member_no_character"].player_character_id == old_id


def test_clear_keeps_character_sheets_and_grants(harness):
    h, client = harness
    character_id = h.rows["character"].id
    before = h.snapshot()
    sheets = h.session.exec(
        select(CharacterSheetVersion).where(
            CharacterSheetVersion.player_character_id == character_id
        )
    ).all()
    grants = h.session.exec(
        select(KnowledgeGrant).where(KnowledgeGrant.player_character_id == character_id)
    ).all()
    assert sheets and grants
    sheet_ids = [row.id for row in sheets]
    grant_ids = [row.id for row in grants]
    response = assign(h, client, {"player_character_id": None}, member="member")
    assert response.status_code == 200, response.text
    assert response.json()["player_character_id"] is None
    assert response.json()["character_name"] is None
    h.session.expire_all()
    assert h.rows["member"].player_character_id is None
    assert h.session.get(PlayerCharacter, character_id) is not None
    for sheet_id in sheet_ids:
        assert (
            h.session.get(CharacterSheetVersion, sheet_id).player_character_id
            == character_id
        )
    for grant_id in grant_ids:
        assert (
            h.session.get(KnowledgeGrant, grant_id).player_character_id == character_id
        )
    after = h.snapshot()
    for table in before:
        if table.rsplit(".", 1)[-1] != CampaignMember.__tablename__:
            assert after[table] == before[table], f"clear changed {table}"


def test_grants_follow_character_through_real_entity_endpoints(harness):
    h, client = harness
    character = unassigned_character(h)
    entity_id = h.rows["private"].id
    response = client.post(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/grants",
        headers=h.headers("dm"),
        json={
            "player_character_id": character.id,
            "entity_id": entity_id,
            "grant_scope": "full",
        },
    )
    assert response.status_code == 200, response.text
    grant_id = response.json()["id"]
    assert (
        assign(
            h, client, {"player_character_id": character.id}, member="member_player_a"
        ).status_code
        == 200
    )
    entity_path = (
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/entities/{entity_id}"
    )
    visible = client.get(entity_path, headers=h.headers("player_a"))
    assert visible.status_code == 200, visible.text
    assert visible.json()["name"] == h.rows["private"].name
    assert client.get(entity_path, headers=h.headers("player_b")).status_code == 404
    assert (
        assign(
            h, client, {"player_character_id": None}, member="member_player_a"
        ).status_code
        == 200
    )
    cleared = client.get(entity_path, headers=h.headers("player_a"))
    assert cleared.status_code == 404, cleared.text
    assert h.rows["private"].name not in cleared.text
    assert (
        assign(
            h, client, {"player_character_id": character.id}, member="member"
        ).status_code
        == 200
    )
    denied = client.get(entity_path, headers=h.headers("player_a"))
    assert denied.status_code == 404, denied.text
    assert h.rows["private"].name not in denied.text
    visible = client.get(entity_path, headers=h.headers("player_b"))
    assert visible.status_code == 200, visible.text
    assert visible.json()["name"] == h.rows["private"].name
    assert h.session.get(KnowledgeGrant, grant_id).player_character_id == character.id


def test_dm_creates_and_assigns_character(harness):
    h, client = harness
    response = assign(h, client, {"new": {"name": "  Elowen  "}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["character_name"] == "Elowen"
    character = h.session.get(PlayerCharacter, result["player_character_id"])
    assert character is not None
    assert character.campaign_id == h.rows["campaign"].id
    assert character.character_name == "Elowen"
    h.session.expire_all()
    assert (
        h.session.get(CampaignMember, result["id"]).player_character_id == character.id
    )


def test_self_creates_once(harness):
    h, client = harness
    response = create_self(h, client)
    assert response.status_code == 200, response.text
    character = h.session.get(PlayerCharacter, response.json()["id"])
    assert character.campaign_id == h.rows["campaign"].id
    assert character.character_name == "Elowen"
    h.session.expire_all()
    assert h.rows["member_no_character"].player_character_id == character.id
    before = h.snapshot()
    again = create_self(h, client)
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "player already has a character"
    assert h.snapshot() == before
    assert len(h.session.exec(select(PlayerCharacter)).all()) == 4
    assert (
        h.session.exec(
            select(CharacterSheetVersion).where(
                CharacterSheetVersion.player_character_id == character.id
            )
        ).all()
        == []
    )


@pytest.mark.parametrize(
    "caller,expected",
    (
        ("player_a", 409),
        ("player_b", 409),
        ("dm", 403),
        ("outsider", 404),
        ("other_campaign", 404),
    ),
)
def test_self_denials_do_not_create_character(harness, caller, expected):
    h, client = harness
    before = h.snapshot()
    response = create_self(h, client, caller=caller)
    assert response.status_code == expected, response.text
    h.assert_no_leak(response, caller)
    assert h.snapshot() == before


def test_self_rejects_character_just_assigned_by_dm(harness):
    h, client = harness
    assert assign(h, client, {"new": {"name": "DM's choice"}}).status_code == 200
    before = h.snapshot()
    response = create_self(h, client)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "player already has a character"
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "body",
    (
        {},
        {"name": "  "},
        {"name": "x" * 121},
        {"name": "Valid", "extra": True},
        {"name": None},
    ),
)
def test_self_name_validation(harness, body):
    h, client = harness
    before = h.snapshot()
    response = create_self(h, client, body=body)
    assert response.status_code == 422, response.text
    assert h.snapshot() == before


@pytest.mark.parametrize("name", ("a", "x" * 120))
@pytest.mark.parametrize("route", ("assign", "self"))
def test_name_length_boundaries(harness, name, route):
    h, client = harness
    response = (
        assign(h, client, {"new": {"name": f"  {name}  "}})
        if route == "assign"
        else create_self(h, client, body={"name": f"  {name}  "})
    )
    assert response.status_code == 200, response.text
    assert response.json()["character_name"] == name


def test_self_literal_route_has_no_same_method_dynamic_collision(harness):
    h, _ = harness
    routes = [
        context
        for context in iter_route_contexts(h.app().routes)
        if "POST" in context.methods
    ]
    path = "/api/grimoire/campaigns/{campaign_id}/characters/self"
    assert sum(context.path == path for context in routes) == 1
    assert not any(
        context.path.startswith("/api/grimoire/campaigns/{campaign_id}/characters/{")
        and len(context.path.split("/")) == len(path.split("/"))
        for context in routes
    )


def test_member_and_lobby_projections_only_return_own_character(harness):
    h, client = harness
    # Lobby registration is real; only the verified-identity dependency is a seam.
    for role in ("dm", "player_a", "player_b", "no_character"):
        h.rows[f"user_{role}"].issuer = "test-issuer"
        h.rows[f"user_{role}"].subject = f"test-{role}"
    h.session.commit()
    response = client.get(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/members",
        headers=h.headers("dm"),
    )
    assert response.status_code == 200, response.text
    h.assert_no_leak(response, "dm")
    members = {row["id"]: row for row in response.json()}
    assert members[h.rows["member_no_character"].id]["character_name"] is None
    assert (
        members[h.rows["member_player_a"].id]["character_name"]
        == h.rows["character_a"].character_name
    )
    assert (
        members[h.rows["member"].id]["character_name"]
        == h.rows["character"].character_name
    )
    for role, character_key in (
        ("dm", None),
        ("player_a", "character_a"),
        ("player_b", "character"),
        ("no_character", None),
    ):
        lobby = client.get("/api/grimoire/lobby", headers=h.headers(role))
        assert lobby.status_code == 200, lobby.text
        h.assert_no_leak(lobby, role)
        campaigns = lobby.json()["campaigns"]
        assert len(campaigns) == 1
        own = campaigns[0]
        character = h.rows[character_key] if character_key else None
        assert own["player_character_id"] == (character.id if character else None)
        assert own["character_name"] == (
            character.character_name if character else None
        )
    created = create_self(h, client)
    assert created.status_code == 200, created.text
    lobby = client.get("/api/grimoire/lobby", headers=h.headers("no_character"))
    assert lobby.status_code == 200, lobby.text
    h.assert_no_leak(lobby, "no_character")
    assert lobby.json()["campaigns"][0]["player_character_id"] == created.json()["id"]
    assert lobby.json()["campaigns"][0]["character_name"] == "Elowen"
