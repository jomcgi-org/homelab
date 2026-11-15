"""Versioned character-sheet workflow and authorization regressions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from auth.api import Authority, Principal, PrincipalKind
from core.db import get_session
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire.access import get_authenticated_identity
from grimoire.models import AppUser, CampaignMember
from grimoire.models import (
    CharacterSheetVersion,
    Entity,
    KnowledgeGrant,
    PlayerCharacter,
)
from grimoire.router import router

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
            is_global=True,
            detail={"speed": "30 feet"},
        )
    )
    session.add(
        Entity(
            entity_type="class",
            name="Fighter",
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


@pytest.fixture(name="request_client")
def request_client_fixture(session: Session, client: TestClient, table):
    """Keep legacy setup helpers, then match production request-session cleanup."""
    session.commit()
    engine = session.get_bind()

    def request_session():
        with Session(engine) as request_session:
            yield request_session

    client.app.dependency_overrides[get_session] = request_session
    try:
        with TestClient(client.app, raise_server_exceptions=False) as request_client:
            yield request_client
    finally:
        client.app.dependency_overrides[get_session] = lambda: session


def _version_snapshot(engine, version_id: str) -> dict:
    with Session(engine) as observer:
        version = observer.get(CharacterSheetVersion, version_id)
        assert version is not None
        snapshot = deepcopy(version.model_dump())
    # SQLite reloads timestamps without timezone information.
    for key, value in snapshot.items():
        if isinstance(value, datetime):
            snapshot[key] = (
                value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            )
    return snapshot


def _version_count(engine) -> int:
    with Session(engine) as observer:
        return len(observer.exec(select(CharacterSheetVersion)).all())


_BAD_CLASS_FACTS = [
    pytest.param(key, facts, detail, id=f"{key}-{case}")
    for key, valid_other, values, detail in (
        (
            "hit_die",
            {"saves": "Strength, Constitution"},
            [
                ("missing", {}),
                ("null", {"hit_die": None}),
                ("numeric", {"hit_die": 8}),
                ("empty", {"hit_die": ""}),
                ("whitespace", {"hit_die": " \t "}),
                ("1d8", {"hit_die": "1d8"}),
                ("d20", {"hit_die": "d20"}),
            ],
            "class corpus entry has no supported hit die",
        ),
        (
            "saves",
            {"hit_die": "d8 per level"},
            [
                ("missing", {}),
                ("null", {"saves": None}),
                ("numeric", {"saves": 12}),
                ("empty", {"saves": ""}),
                ("whitespace", {"saves": " \t "}),
                ("unknown", {"saves": "Fortitude, Reflex"}),
            ],
            "class corpus entry has no supported saving throws",
        ),
    )
    for case, malformed in values
    for facts in [{**valid_other, **malformed}]
]


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("fact,detail,error", _BAD_CLASS_FACTS)
def test_malformed_class_mechanics_refuse_without_persisting(
    session: Session, request_client: TestClient, table, method, fact, detail, error
):
    campaign, character = table
    class_name = f"Malformed {fact} Class"
    session.add(
        Entity(entity_type="class", name=class_name, is_global=True, detail=detail)
    )
    session.commit()
    engine = session.get_bind()
    path = _draft_path(campaign, character)
    before = None
    if method == "PATCH":
        created = _post(request_client, path, PLAYER, _sheet())
        assert created.status_code == 200
        version_id = created.json()["id"]
        path = _version_path(campaign, character, version_id)
        before = _version_snapshot(engine, version_id)
    count = _version_count(engine)
    changed = {**_sheet(level=6), "class_name": class_name}
    changed["ability_scores"]["constitution"] = 18
    response = request_client.request(method, path, headers=_auth(PLAYER), json=changed)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": error}
    assert _version_count(engine) == count
    if before is not None:
        # All columns, including every decision field, are compared after cleanup.
        assert _version_snapshot(engine, version_id) == before

    valid = _sheet(level=6)
    response = request_client.request(method, path, headers=_auth(PLAYER), json=valid)
    assert response.status_code == 200, response.text
    saved = _version_snapshot(engine, response.json()["id"])
    assert saved["sheet"] == valid
    assert saved["derived"] == response.json()["derived"]
    assert saved["status"] == "draft"


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize(
    "hit_die,saves,die,hp,bonuses",
    [
        (
            "d10 per level",
            "Strength, Constitution",
            10,
            44,
            {
                "strength": 6,
                "dexterity": 2,
                "constitution": 5,
                "intelligence": 0,
                "wisdom": 1,
                "charisma": -1,
            },
        ),
        (
            "d8 per level",
            "WIS/CHA",
            8,
            38,
            {
                "strength": 3,
                "dexterity": 2,
                "constitution": 2,
                "intelligence": 0,
                "wisdom": 4,
                "charisma": 2,
            },
        ),
    ],
)
def test_supported_class_mechanics_save(
    session: Session,
    request_client: TestClient,
    table,
    method,
    hit_die,
    saves,
    die,
    hp,
    bonuses,
):
    campaign, character = table
    session.add(
        Entity(
            entity_type="class",
            name="Supported Prose Class",
            is_global=True,
            detail={"hit_die": hit_die, "saves": saves},
        )
    )
    session.commit()
    path = _draft_path(campaign, character)
    if method == "PATCH":
        created = _post(request_client, path, PLAYER, _sheet(level=1))
        assert created.status_code == 200
        path = _version_path(campaign, character, created.json()["id"])
    sheet = {**_sheet(), "class_name": "Supported Prose Class"}
    response = request_client.request(method, path, headers=_auth(PLAYER), json=sheet)
    assert response.status_code == 200, response.text
    data = response.json()
    assert int(data["derived"]["hit_die"].removeprefix("d")) == die
    assert data["derived"]["max_hit_points"] == hp
    assert data["derived"]["saving_throw_bonuses"] == bonuses
    saved = _version_snapshot(session.get_bind(), data["id"])
    assert saved["sheet"] == sheet
    assert saved["derived"] == data["derived"]
    assert saved["status"] == "draft"


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


def _options_path(campaign: dict, character: dict) -> str:
    return _draft_path(campaign, character).replace("/drafts", "/options")


def test_sheet_options_offer_what_a_draft_accepts_with_class_facts(
    session: Session, client: TestClient, table
):
    campaign, character = table
    scopes = {"Full": "full", "Partial": "partial", "Named": "name_only"}
    for label, scope in scopes.items():
        entity = Entity(
            entity_type="class",
            name=f"{label} Homebrew",
            is_global=False,
            source_type="homebrew",
            detail={"hit_die": "d8 per level", "saves": "WIS and CHA"},
        )
        session.add(entity)
        session.flush()
        session.add(
            KnowledgeGrant(
                campaign_id=campaign["id"],
                entity_id=entity.id,
                player_character_id=character["id"],
                grant_scope=scope,
            )
        )
    session.add(Entity(entity_type="class", name="Hidden", is_global=False, detail={}))
    session.add(Entity(entity_type="class", name="fighter", is_global=True))
    session.add(Entity(entity_type="class", name="Odd  Spacing", is_global=True))
    session.add(Entity(entity_type="class", name="Bare", is_global=True, detail={}))
    session.add(Entity(entity_type="spell", name="Fireball", is_global=True))
    session.commit()

    response = client.get(_options_path(campaign, character), headers=_auth(PLAYER))
    assert response.status_code == 200
    options = response.json()
    assert [
        (item["name"], item["size"], item["speed"]) for item in options["ancestries"]
    ] == [("Human", None, "30 feet")]
    classes = {item["name"]: item for item in options["classes"]}
    assert sorted(classes) == ["Bare", "Fighter", "Full Homebrew"]
    assert classes["Fighter"]["hit_die"] == "d10"
    assert classes["Fighter"]["saves"] == "Strength, Constitution"
    assert classes["Full Homebrew"]["hit_die"] == "d8 per level"
    assert classes["Bare"] == {
        "id": classes["Bare"]["id"],
        "name": "Bare",
        "hit_die": None,
        "primary_ability": None,
        "saves": None,
    }

    # Every offered pair is one a draft accepts or refuses for its class facts.
    for name, expected in (("Partial Homebrew", 422), ("Full Homebrew", 200)):
        draft = _post(
            client,
            _draft_path(campaign, character),
            PLAYER,
            {**_sheet(), "class_name": name},
        )
        assert draft.status_code == expected, name
        if expected == 200:
            assert (
                draft.json()["derived"]["corpus"]["class_entity_id"]
                == (classes[name]["id"])
            )
            assert draft.json()["derived"]["hit_die"] == "d8"

    dm_view = client.get(_options_path(campaign, character), headers=_auth(DM))
    assert dm_view.status_code == 200
    assert dm_view.json() == options


@pytest.mark.parametrize("scope", ["partial", "name_only"])
def test_a_narrowed_global_entry_is_neither_offered_nor_accepted(
    session: Session, client: TestClient, table, scope
):
    """A grant below full on a global class hides its mechanics from the character."""
    campaign, character = table
    entity = Entity(
        entity_type="class",
        name="Warlock",
        is_global=True,
        detail={"hit_die": "d8", "saves": "Wisdom, Charisma"},
    )
    session.add(entity)
    session.flush()
    session.add(
        KnowledgeGrant(
            campaign_id=campaign["id"],
            entity_id=entity.id,
            player_character_id=character["id"],
            grant_scope=scope,
        )
    )
    session.commit()

    options = client.get(_options_path(campaign, character), headers=_auth(PLAYER))
    assert options.status_code == 200
    assert "Warlock" not in [item["name"] for item in options.json()["classes"]]
    assert "Wisdom, Charisma" not in options.text
    draft = _post(
        client,
        _draft_path(campaign, character),
        PLAYER,
        {**_sheet(), "class_name": "Warlock"},
    )
    assert draft.status_code == 422


def test_sheet_options_are_hidden_from_other_players_and_campaigns(
    client: TestClient, table
):
    campaign, character = table
    other = _character(client, DM, campaign["id"], "Bryn")
    _member(client, DM, campaign["id"], OTHER_PLAYER, other["id"])
    path = _options_path(campaign, character)
    assert client.get(path, headers=_auth(OTHER_PLAYER)).status_code == 404
    assert client.get(path, headers=_auth(OTHER_DM)).status_code == 404
