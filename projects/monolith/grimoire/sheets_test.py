"""Versioned character-sheet workflow and authorization regressions."""

from __future__ import annotations

import pytest
from auth.api import Authority, Principal, PrincipalKind
from core.db import get_session
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire.access import get_authenticated_identity
from grimoire.models import AppUser, CampaignMember
from grimoire.models import CharacterSheetVersion, Entity, PlayerCharacter
from grimoire.router import router
from grimoire import knowledge_sources
from grimoire.models import KnowledgeContribution, KnowledgeGrant, Relationship

DM = "dm@example.test"
PLAYER = "player@example.test"
OTHER_DM = "other-dm@example.test"
OTHER_PLAYER = "other-player@example.test"


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'grimoire-sheets.db'}",
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


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session

    def identity(request: Request) -> Principal:
        email = request.headers.get("X-Test-Auth-Email")
        if not email:
            raise HTTPException(403, "test identity missing")
        return Principal(
            subject=f"test:{email}",
            actor=(),
            scope=(),
            groups=(),
            kind=PrincipalKind.HUMAN,
            authority=Authority.STANDING,
            email=email,
        )

    app.dependency_overrides[get_authenticated_identity] = identity
    yield TestClient(app)
    app.dependency_overrides.clear()


def _auth(email: str) -> dict[str, str]:
    return {"X-Test-Auth-Email": email}


def _post(client: TestClient, path: str, email: str, json: dict | None = None):
    return client.post(path, headers=_auth(email), json=json)


def _campaign(client: TestClient, email: str, name: str) -> dict:
    response = _post(client, "/api/grimoire/campaigns", email, {"name": name})
    assert response.status_code == 200
    return response.json()


def _character(client: TestClient, email: str, campaign_id: str, name: str) -> dict:
    response = _post(
        client,
        f"/api/grimoire/campaigns/{campaign_id}/characters",
        email,
        {"character_name": name},
    )
    assert response.status_code == 200
    return response.json()


def _member(client, dm, campaign_id, player, character_id):
    session = client.app.dependency_overrides[get_session]()
    user = AppUser(email=player)
    session.add(user)
    session.flush()
    member = CampaignMember(
        campaign_id=campaign_id,
        app_user_id=user.id,
        role="player",
        player_character_id=character_id,
    )
    session.add(member)
    session.commit()
    return {"id": member.id, "player_character_id": character_id}


def _sheet(level: int = 5) -> dict:
    return {
        "schema_version": 1,
        "ancestry": "Human",
        "class_name": "Fighter",
        "level": level,
        "ability_scores": {
            "strength": 16,
            "dexterity": 14,
            "constitution": 14,
            "intelligence": 10,
            "wisdom": 12,
            "charisma": 8,
        },
    }


@pytest.fixture(name="table")
def table_fixture(session: Session, client: TestClient):
    session.add(
        Entity(
            entity_type="race",
            name="Human",
            source_book="system-reference-doc-5-1",
            is_global=True,
            detail={"speed": "30 feet"},
        )
    )
    session.add(
        Entity(
            entity_type="class",
            name="Fighter",
            source_book="system-reference-doc-5-1",
            is_global=True,
            detail={"hit_die": "d10", "saves": "Strength, Constitution"},
        )
    )
    session.commit()
    campaign = _campaign(client, DM, "Table")
    character = _character(client, DM, campaign["id"], "Arden")
    _member(client, DM, campaign["id"], PLAYER, character["id"])
    return campaign, character


def _draft_path(campaign: dict, character: dict) -> str:
    return (
        f"/api/grimoire/campaigns/{campaign['id']}"
        f"/characters/{character['id']}/sheets/drafts"
    )


def _version_path(campaign: dict, character: dict, version_id: str) -> str:
    return (
        f"/api/grimoire/campaigns/{campaign['id']}"
        f"/characters/{character['id']}/sheets/{version_id}"
    )


def _submitted(client, table, body=None):
    campaign, character = table
    created = _post(client, _draft_path(campaign, character), PLAYER, body or _sheet())
    assert created.status_code == 200, created.text
    base = _version_path(campaign, character, created.json()["id"])
    assert _post(client, f"{base}/submit", PLAYER).status_code == 200
    return base, created.json()["id"]


def test_approval_sources_repeat_return_and_manual_fallback(session, client, table):
    campaign, character = table
    ancestry = session.exec(select(Entity).where(Entity.entity_type == "race")).one()
    manual = KnowledgeGrant(
        campaign_id=campaign["id"],
        player_character_id=character["id"],
        entity_id=ancestry.id,
        grant_scope="partial",
        revealed_details={"clue": "Independent manual clue"},
    )
    session.add(manual)
    session.commit()
    base, version_id = _submitted(client, table)
    approved = _post(client, f"{base}/approve", DM, {})
    assert approved.status_code == 200, approved.text
    assert _post(client, f"{base}/approve", DM, {}).status_code == 200
    session.refresh(manual)
    assert manual.grant_scope == "full"
    sources = knowledge_sources.contributions(session, manual)
    assert {source.source_key for source in sources} == {
        "manual",
        f"sheet:{version_id}",
    }
    assert next(
        source for source in sources if source.source_key == "manual"
    ).revealed_details == {"clue": "Independent manual clue"}
    preview = _post(
        client,
        f"/api/grimoire/campaigns/{campaign['id']}/grants/preview",
        DM,
        {
            "grants": [
                {
                    "entity_id": ancestry.id,
                    "player_character_id": character["id"],
                    "grant_scope": "name_only",
                }
            ]
        },
    )
    assert preview.status_code == 200, preview.text
    assert "recognition_only" not in preview.json()[0]["projection"]
    assert (
        knowledge_sources.preview_manual(
            session,
            KnowledgeGrant(
                campaign_id=campaign["id"],
                player_character_id=character["id"],
                entity_id=ancestry.id,
                grant_scope="name_only",
            ),
        ).grant_scope
        == "full"
    )

    # Returning a different draft must preserve the active approved source.
    returned, _ = _submitted(client, table, _sheet(6))
    assert (
        _post(client, f"{returned}/return", DM, {"comment": "Revise"}).status_code
        == 200
    )
    assert {
        source.source_key for source in knowledge_sources.contributions(session, manual)
    } == {"manual", f"sheet:{version_id}"}
    assert (
        _post(client, f"{base}/return", DM, {"comment": "Withdraw"}).status_code == 409
    )

    # A replacement ancestry removes only the obsolete automatic source.
    session.add(
        Entity(entity_type="race", name="Elf", source_book="system-reference-doc-5-1")
    )
    session.commit()
    replacement, replacement_id = _submitted(
        client, table, {**_sheet(), "ancestry": "Elf"}
    )
    assert _post(client, f"{replacement}/approve", DM, {}).status_code == 200
    session.refresh(manual)
    assert manual.grant_scope == "partial"
    assert manual.revealed_details == {"clue": "Independent manual clue"}
    assert {
        source.source_key for source in knowledge_sources.contributions(session, manual)
    } == {"manual"}
    auto = session.exec(
        select(KnowledgeContribution).where(
            KnowledgeContribution.source_sheet_version_id.is_not(None)
        )
    ).all()
    assert {source.source_sheet_version_id for source in auto} == {replacement_id}


def test_creation_links_are_typed_bounded_and_not_adventures(session, client, table):
    parent = session.exec(select(Entity).where(Entity.entity_type == "class")).one()
    safe = Entity(
        entity_type="class_feature",
        name="Second Wind",
        source_book="system-reference-doc-5-1",
        detail={"level": 1},
    )
    late = Entity(
        entity_type="class_feature",
        name="Later feature",
        source_book="system-reference-doc-5-1",
        detail={"level": 20},
    )
    adventure = Entity(
        entity_type="class_feature",
        name="Adventure secret",
        source_book="lost-mine-of-phandelver",
        detail={"level": 1},
    )
    arbitrary = Entity(
        entity_type="npc", name="Secret NPC", source_book="lost-mine-of-phandelver"
    )
    session.add_all([safe, late, adventure, arbitrary])
    session.flush()
    session.add_all(
        [
            Relationship(
                from_entity_id=entity.id, to_entity_id=parent.id, rel_type="FEATURE_OF"
            )
            for entity in (safe, late, adventure, arbitrary)
        ]
    )
    session.commit()
    base, _ = _submitted(client, table)
    assert _post(client, f"{base}/approve", DM, {}).status_code == 200
    granted = {grant.entity_id for grant in session.exec(select(KnowledgeGrant)).all()}
    assert safe.id in granted
    assert not granted.intersection({late.id, adventure.id, arbitrary.id})


def test_approval_revalidates_changed_source_and_rolls_back(session, client, table):
    base, version_id = _submitted(client, table)
    entity = session.exec(select(Entity).where(Entity.entity_type == "race")).one()
    entity.source_book = "lost-mine-of-phandelver"
    session.commit()
    assert _post(client, f"{base}/approve", DM, {}).status_code == 422
    assert session.get(CharacterSheetVersion, version_id).status == "submitted"
    assert session.exec(select(KnowledgeGrant)).all() == []
    assert session.exec(select(KnowledgeContribution)).all() == []


def test_approved_history_is_immutable_and_later_edits_create_new_draft(
    session: Session, client: TestClient, table
):
    campaign, character = table
    rejected = _post(
        client,
        _draft_path(campaign, character),
        PLAYER,
        {**_sheet(), "proficiency_bonus": 99},
    )
    assert rejected.status_code == 422

    created = _post(client, _draft_path(campaign, character), PLAYER, _sheet())
    assert created.status_code == 200
    first = created.json()
    assert first["version"] == 1
    assert first["status"] == "draft"
    assert first["derived"]["proficiency_bonus"] == 3
    assert first["derived"]["ability_modifiers"]["strength"] == 3
    assert first["derived"]["saving_throw_bonuses"]["strength"] == 6
    assert first["derived"]["saving_throw_bonuses"]["wisdom"] == 1
    assert first["derived"]["attack_bonuses"] == {"melee": 6, "ranged": 5}
    assert first["derived"]["unarmored_armor_class"] == 12
    assert first["derived"]["max_hit_points"] == 44

    base = _version_path(campaign, character, first["id"])
    submitted = _post(client, f"{base}/submit", PLAYER)
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"
    approved = _post(client, f"{base}/approve", DM, {"comment": "Ready"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert (
        client.patch(base, headers=_auth(PLAYER), json=_sheet(level=6)).status_code
        == 409
    )

    second = _post(client, _draft_path(campaign, character), PLAYER, _sheet(level=6))
    assert second.status_code == 200
    assert second.json()["version"] == 2
    assert second.json()["status"] == "draft"

    history = client.get(base.rsplit("/", 1)[0], headers=_auth(PLAYER))
    assert history.status_code == 200
    assert history.json()["viewer_role"] == "player"
    version_states = [
        (item["version"], item["status"]) for item in history.json()["versions"]
    ]
    assert version_states == [
        (2, "draft"),
        (1, "approved"),
    ]

    rows = session.exec(
        select(CharacterSheetVersion).order_by(CharacterSheetVersion.version)
    ).all()
    assert rows[0].status == "approved"
    assert rows[0].sheet["level"] == 5
    persisted_character = session.get(PlayerCharacter, character["id"])
    assert persisted_character is not None
    assert persisted_character.level == 5
    assert persisted_character.class_name == "Fighter"


def test_player_cannot_take_dm_decisions_or_write_another_character(
    client: TestClient, table
):
    campaign, character = table
    created = _post(client, _draft_path(campaign, character), PLAYER, _sheet()).json()
    base = _version_path(campaign, character, created["id"])
    assert _post(client, f"{base}/submit", PLAYER).status_code == 200

    assert _post(client, f"{base}/approve", PLAYER, {}).status_code == 403
    assert _post(client, f"{base}/return", PLAYER, {"comment": "No"}).status_code == 403
    dm_draft = _post(client, _draft_path(campaign, character), DM, _sheet())
    assert dm_draft.status_code == 403


def test_return_comment_and_workflow_transitions_are_enforced(
    client: TestClient, table
):
    campaign, character = table
    created = _post(client, _draft_path(campaign, character), PLAYER, _sheet()).json()
    base = _version_path(campaign, character, created["id"])

    assert _post(client, f"{base}/approve", DM, {}).status_code == 409
    assert _post(client, f"{base}/submit", PLAYER).status_code == 200
    assert _post(client, f"{base}/submit", PLAYER).status_code == 409
    assert _post(client, f"{base}/return", DM, {"comment": "  "}).status_code == 422

    returned = _post(
        client,
        f"{base}/return",
        DM,
        {"comment": "Choose an ancestry from the campaign corpus."},
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == "returned"
    assert _post(client, f"{base}/approve", DM, {}).status_code == 409
    assert client.patch(base, headers=_auth(PLAYER), json=_sheet()).status_code == 409

    replacement = _post(client, _draft_path(campaign, character), PLAYER, _sheet(2))
    assert replacement.status_code == 200
    assert replacement.json()["version"] == 2


def test_cross_campaign_sheet_reads_and_writes_are_hidden(client: TestClient, table):
    campaign, character = table
    other_campaign = _campaign(client, OTHER_DM, "Other table")
    other_character = _character(client, OTHER_DM, other_campaign["id"], "Bryn")
    _member(
        client,
        OTHER_DM,
        other_campaign["id"],
        OTHER_PLAYER,
        other_character["id"],
    )
    own = _post(client, _draft_path(campaign, character), PLAYER, _sheet()).json()

    foreign_history = (
        f"/api/grimoire/campaigns/{other_campaign['id']}"
        f"/characters/{other_character['id']}/sheets"
    )
    assert client.get(foreign_history, headers=_auth(PLAYER)).status_code == 404
    assert (
        _post(
            client,
            f"{foreign_history}/drafts",
            PLAYER,
            _sheet(),
        ).status_code
        == 404
    )
    mixed_path = (
        f"/api/grimoire/campaigns/{campaign['id']}"
        f"/characters/{other_character['id']}/sheets"
    )
    assert client.get(mixed_path, headers=_auth(DM)).status_code == 404
    assert (
        _post(
            client,
            f"{mixed_path}/{own['id']}/approve",
            DM,
            {},
        ).status_code
        == 404
    )


def test_sheet_selections_must_exist_in_visible_campaign_corpus(
    client: TestClient, table
):
    campaign, character = table
    missing_race = {**_sheet(), "ancestry": "Astral Elf"}
    response = _post(client, _draft_path(campaign, character), PLAYER, missing_race)
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "ancestry is not available in this campaign corpus"
    )
