"""HTTP enforcement tests for authenticated Grimoire campaign ACLs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from auth.api import (
    Authority,
    Principal,
    PrincipalKind,
    auth_error_handler,
)
from auth.errors import AuthError
from auth.verifier import TokenResolver
from core.db import get_session
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from knowledge.api import get_embedding_client
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire.access import get_authenticated_identity, get_grimoire_operator_email
from grimoire.models import (
    AppUser,
    Book,
    Campaign,
    CampaignMember,
    ChunkEntityMention,
    Entity,
    KnowledgeChunk,
    KnowledgeGrant,
    PlayerCharacter,
    Relationship,
)
from grimoire.router import router

DM_EMAIL = "dm@example.test"
OTHER_DM_EMAIL = "other-dm@example.test"
PLAYER_EMAIL = "player@example.test"
OTHER_PLAYER_EMAIL = "other-player@example.test"
OPERATOR_EMAIL = "operator@example.test"


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'grimoire-acl.db'}",
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


class FakeEmbedClient:
    async def embed(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0, 0.0]


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbedClient()

    def trusted_test_identity(request: Request) -> Principal:
        email = request.headers.get("X-Test-Auth-Email")
        if not email:
            raise HTTPException(403, "test identity missing")
        normalized = email.strip().lower()
        groups = ("operators",) if normalized == OPERATOR_EMAIL else ()
        return _principal(normalized, groups=groups)

    # Only cryptographic verification is replaced. Email projection, operator
    # role, campaign, object, and character authorization execute unchanged.
    app.dependency_overrides[get_authenticated_identity] = trusted_test_identity
    yield TestClient(app)
    app.dependency_overrides.clear()


def _auth(email: str) -> dict[str, str]:
    return {"X-Test-Auth-Email": email}


def _create_campaign(client: TestClient, email: str, name: str) -> dict:
    response = client.post(
        "/api/grimoire/campaigns",
        headers=_auth(email),
        json={"name": name},
    )
    assert response.status_code == 200
    return response.json()


def _create_character(
    client: TestClient, email: str, campaign_id: str, name: str
) -> dict:
    response = client.post(
        f"/api/grimoire/campaigns/{campaign_id}/characters",
        headers=_auth(email),
        json={"character_name": name, "sheet": {"secret": name}},
    )
    assert response.status_code == 200
    return response.json()


def _provision(
    client: TestClient,
    dm_email: str,
    campaign_id: str,
    player_email: str,
    character_id: str | None,
) -> dict:
    client.app.dependency_overrides[get_grimoire_operator_email] = lambda: dm_email
    response = client.post(
        f"/api/grimoire/campaigns/{campaign_id}/members",
        headers=_auth(dm_email),
        json={
            "email": player_email,
            "player_character_id": character_id,
        },
    )
    del client.app.dependency_overrides[get_grimoire_operator_email]
    assert response.status_code == 200
    return response.json()


def _seed_table(session: Session, client: TestClient) -> SimpleNamespace:
    campaign_a = _create_campaign(client, DM_EMAIL, "Campaign A")
    campaign_b = _create_campaign(client, OTHER_DM_EMAIL, "Campaign B")

    game_a = client.post(
        f"/api/grimoire/campaigns/{campaign_a['id']}/sessions",
        headers=_auth(DM_EMAIL),
    ).json()
    game_b = client.post(
        f"/api/grimoire/campaigns/{campaign_b['id']}/sessions",
        headers=_auth(OTHER_DM_EMAIL),
    ).json()

    player_character = _create_character(
        client, DM_EMAIL, campaign_a["id"], "Player Character"
    )
    spare_character = _create_character(
        client, DM_EMAIL, campaign_a["id"], "DM Character"
    )
    foreign_character = _create_character(
        client, OTHER_DM_EMAIL, campaign_b["id"], "Foreign Character"
    )
    player_member = _provision(
        client,
        DM_EMAIL,
        campaign_a["id"],
        PLAYER_EMAIL,
        player_character["id"],
    )
    foreign_member = _provision(
        client,
        OTHER_DM_EMAIL,
        campaign_b["id"],
        OTHER_PLAYER_EMAIL,
        foreign_character["id"],
    )

    global_entity = Entity(entity_type="creature", name="Global", is_global=True)
    private_a = Entity(
        entity_type="npc",
        name="Campaign A Secret",
        is_global=False,
        created_in_session=game_a["id"],
        detail={"dm_only": True},
    )
    ungranted_a = Entity(
        entity_type="faction",
        name="Campaign A Ungranted",
        is_global=False,
        created_in_session=game_a["id"],
    )
    private_b = Entity(
        entity_type="npc",
        name="Campaign B Secret",
        is_global=False,
        created_in_session=game_b["id"],
    )
    session.add(global_entity)
    session.add(private_a)
    session.add(ungranted_a)
    session.add(private_b)
    session.commit()
    for entity in (global_entity, private_a, ungranted_a, private_b):
        session.refresh(entity)

    grant_a_response = client.post(
        f"/api/grimoire/campaigns/{campaign_a['id']}/grants",
        headers=_auth(DM_EMAIL),
        json={
            "entity_id": private_a.id,
            "player_character_id": player_character["id"],
            "grant_scope": "partial",
            "revealed_details": {"known": "safe"},
        },
    )
    assert grant_a_response.status_code == 200
    grant_b_response = client.post(
        f"/api/grimoire/campaigns/{campaign_b['id']}/grants",
        headers=_auth(OTHER_DM_EMAIL),
        json={
            "entity_id": private_b.id,
            "player_character_id": foreign_character["id"],
            "grant_scope": "full",
        },
    )
    assert grant_b_response.status_code == 200

    chunk = KnowledgeChunk(
        book_id="private-book",
        chunk_ref="private-1",
        content="Private campaign source context",
        seq=0,
    )
    session.add(chunk)
    session.commit()
    session.refresh(chunk)
    session.add(ChunkEntityMention(chunk_id=chunk.id, entity_id=private_a.id))
    session.add(
        Relationship(
            from_entity_id=private_a.id,
            to_entity_id=ungranted_a.id,
            rel_type="KNOWS",
        )
    )
    session.add(Book(id="private-book", display_name="Private Book"))
    session.commit()

    return SimpleNamespace(
        campaign_a=campaign_a,
        campaign_b=campaign_b,
        game_a=game_a,
        game_b=game_b,
        player_character=player_character,
        spare_character=spare_character,
        foreign_character=foreign_character,
        player_member=player_member,
        foreign_member=foreign_member,
        global_entity=global_entity,
        private_a=private_a,
        ungranted_a=ungranted_a,
        private_b=private_b,
        grant_a=grant_a_response.json(),
        grant_b=grant_b_response.json(),
        chunk=chunk,
    )


class MappingVerifier:
    def __init__(self, tokens: dict[str, Principal]) -> None:
        self.tokens = tokens

    async def verify(self, token: str) -> Principal | None:
        return self.tokens.get(token)


def _principal(email: str, *, groups: tuple[str, ...] = ()) -> Principal:
    return Principal(
        subject=f"user:{email}",
        actor=(),
        scope=(),
        groups=groups,
        email=email,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )


def test_private_routes_require_verified_identity_and_bind_proxy_projection(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.auth_resolver = TokenResolver(
        [
            MappingVerifier(
                {
                    "browser-token": _principal(DM_EMAIL),
                    "bearer-token": _principal(OTHER_DM_EMAIL),
                }
            )
        ]
    )
    app.add_exception_handler(AuthError, auth_error_handler)
    with TestClient(app) as real_client:
        missing = real_client.post(
            "/api/grimoire/campaigns",
            json={"name": "No identity"},
        )
        assert missing.status_code == 403

        forged_projection = real_client.post(
            "/api/grimoire/campaigns",
            headers={"X-Auth-Email": "forged@example.test"},
            json={"name": "Forged"},
        )
        assert forged_projection.status_code == 403

        invalid_credential = real_client.post(
            "/api/grimoire/campaigns",
            headers={"Authorization": "Bearer invalid-token"},
            json={"name": "Invalid"},
        )
        assert invalid_credential.status_code == 401

        mismatch = real_client.post(
            "/api/grimoire/campaigns",
            headers={
                "Authorization": "Bearer bearer-token",
                "X-Auth-Email": DM_EMAIL,
            },
            json={"name": "Mismatch"},
        )
        assert mismatch.status_code == 403

        browser = real_client.post(
            "/api/grimoire/campaigns",
            headers={
                "Cf-Access-Jwt-Assertion": "browser-token",
                "X-Auth-Email": "  DM@EXAMPLE.TEST  ",
            },
            json={"name": "Browser"},
        )
        assert browser.status_code == 200
        assert (
            session.exec(select(AppUser).where(AppUser.email == DM_EMAIL)).first()
            is not None
        )

        direct_bearer = real_client.post(
            "/api/grimoire/campaigns",
            headers={"Authorization": "Bearer bearer-token"},
            json={"name": "Direct bearer"},
        )
        assert direct_bearer.status_code == 200

        ambiguous = real_client.post(
            "/api/grimoire/campaigns",
            headers=[
                ("Cf-Access-Jwt-Assertion", "browser-token"),
                ("X-Auth-Email", "forged@example.test"),
                ("X-Auth-Email", DM_EMAIL),
            ],
            json={"name": "Ambiguous"},
        )
        assert ambiguous.status_code == 403


def test_initial_dm_provisions_player_character_acl_and_revocation(session, client):
    campaign = _create_campaign(client, DM_EMAIL, "Campaign")
    own_character = _create_character(client, DM_EMAIL, campaign["id"], "Own")
    _create_character(client, DM_EMAIL, campaign["id"], "Hidden")

    members = client.get(
        f"/api/grimoire/campaigns/{campaign['id']}/members",
        headers=_auth(DM_EMAIL),
    ).json()
    assert [(member["email"], member["role"]) for member in members] == [
        (DM_EMAIL, "dm")
    ]

    player_member = _provision(
        client,
        DM_EMAIL,
        campaign["id"],
        "  PLAYER@EXAMPLE.TEST  ",
        own_character["id"],
    )
    assert player_member["email"] == PLAYER_EMAIL
    assert player_member["role"] == "player"

    campaigns = client.get(
        "/api/grimoire/campaigns", headers=_auth(PLAYER_EMAIL)
    ).json()
    assert [row["id"] for row in campaigns] == [campaign["id"]]
    characters = client.get(
        f"/api/grimoire/campaigns/{campaign['id']}/characters",
        headers=_auth(PLAYER_EMAIL),
    ).json()
    assert [row["id"] for row in characters] == [own_character["id"]]
    assert characters[0]["sheet"] == {"secret": "Own"}

    unassigned_email = "unassigned@example.test"
    unassigned = _provision(
        client,
        DM_EMAIL,
        campaign["id"],
        unassigned_email,
        None,
    )
    assert unassigned["player_character_id"] is None
    assert (
        client.get(
            f"/api/grimoire/campaigns/{campaign['id']}/characters",
            headers=_auth(unassigned_email),
        ).json()
        == []
    )

    revoked = client.delete(
        f"/api/grimoire/campaigns/{campaign['id']}/members/{player_member['id']}",
        headers=_auth(DM_EMAIL),
    )
    assert revoked.status_code == 204
    assert (
        client.get(
            f"/api/grimoire/campaigns/{campaign['id']}",
            headers=_auth(PLAYER_EMAIL),
        ).status_code
        == 404
    )
    assert (
        client.get("/api/grimoire/campaigns", headers=_auth(PLAYER_EMAIL)).json() == []
    )
    assert session.exec(select(AppUser).where(AppUser.email == PLAYER_EMAIL)).first()


def test_operator_bootstraps_existing_campaign_without_rewriting_characters(
    session, client
):
    campaign = Campaign(name="Imported campaign", dm_name="Legacy DM")
    session.add(campaign)
    session.flush()
    character = PlayerCharacter(
        campaign_id=campaign.id,
        character_name="Imported character",
        sheet={"legacy": True},
    )
    session.add(character)
    session.commit()
    session.refresh(campaign)
    session.refresh(character)

    hidden = client.get(
        f"/api/grimoire/campaigns/{campaign.id}",
        headers=_auth(DM_EMAIL),
    )
    assert hidden.status_code == 404

    unrelated = client.post(
        f"/api/grimoire/campaigns/{campaign.id}/bootstrap-dm",
        headers=_auth(DM_EMAIL),
        json={"email": DM_EMAIL},
    )
    assert unrelated.status_code == 403
    assert session.exec(select(CampaignMember)).all() == []

    bootstrapped = client.post(
        f"/api/grimoire/campaigns/{campaign.id}/bootstrap-dm",
        headers=_auth(OPERATOR_EMAIL),
        json={"email": "  DM@EXAMPLE.TEST  "},
    )
    assert bootstrapped.status_code == 200
    assert bootstrapped.json()["email"] == DM_EMAIL
    assert bootstrapped.json()["role"] == "dm"

    visible_characters = client.get(
        f"/api/grimoire/campaigns/{campaign.id}/characters",
        headers=_auth(DM_EMAIL),
    )
    assert visible_characters.status_code == 200
    assert visible_characters.json() == [
        {
            "id": character.id,
            "campaign_id": campaign.id,
            "player_name": None,
            "character_name": "Imported character",
            "class_name": None,
            "level": None,
            "sheet": {"legacy": True},
        }
    ]

    repeated = client.post(
        f"/api/grimoire/campaigns/{campaign.id}/bootstrap-dm",
        headers=_auth(OPERATOR_EMAIL),
        json={"email": OTHER_DM_EMAIL},
    )
    assert repeated.status_code == 409


def test_nonmember_and_cross_campaign_reads_reveal_nothing(session, client):
    seed = _seed_table(session, client)
    outsider = _auth("outsider@example.test")
    player = _auth(PLAYER_EMAIL)

    assert (
        client.get(
            f"/api/grimoire/campaigns/{seed.campaign_a['id']}", headers=outsider
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/grimoire/campaigns/{seed.campaign_a['id']}/characters",
            headers=outsider,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/grimoire/campaigns/{seed.campaign_a['id']}/entities",
            headers=outsider,
        ).status_code
        == 404
    )

    for suffix in (
        "",
        "/characters",
        "/entities",
        f"/entities/{seed.private_b.id}",
        f"/entities/{seed.private_b.id}/relationships",
        f"/entities/{seed.private_b.id}/mentions",
    ):
        response = client.get(
            f"/api/grimoire/campaigns/{seed.campaign_b['id']}{suffix}",
            headers=player,
        )
        assert response.status_code == 404
    assert (
        client.get(
            f"/api/grimoire/campaigns/{seed.campaign_b['id']}/search?q=secret",
            headers=player,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/grimoire/chunks/{seed.chunk.id}?campaign={seed.campaign_b['id']}",
            headers=player,
        ).status_code
        == 404
    )

    dm_a_entities = client.get(
        f"/api/grimoire/campaigns/{seed.campaign_a['id']}/entities",
        headers=_auth(DM_EMAIL),
    ).json()["items"]
    assert seed.private_b.id not in {item["id"] for item in dm_a_entities}
    assert (
        client.get(
            f"/api/grimoire/campaigns/{seed.campaign_a['id']}/entities/{seed.private_b.id}",
            headers=_auth(DM_EMAIL),
        ).status_code
        == 404
    )


def test_player_cannot_use_any_existing_dm_only_mutation(session, client):
    seed = _seed_table(session, client)
    player = _auth(PLAYER_EMAIL)
    campaign_id = seed.campaign_a["id"]

    attempts = [
        client.post(
            f"/api/grimoire/campaigns/{campaign_id}/characters",
            headers=player,
            json={"character_name": "Forbidden"},
        ),
        client.post(
            f"/api/grimoire/campaigns/{campaign_id}/members",
            headers=player,
            json={"email": "new@example.test"},
        ),
        client.get(f"/api/grimoire/campaigns/{campaign_id}/members", headers=player),
        client.delete(
            f"/api/grimoire/campaigns/{campaign_id}/members/{seed.player_member['id']}",
            headers=player,
        ),
        client.post(
            f"/api/grimoire/campaigns/{campaign_id}/grants",
            headers=player,
            json={
                "entity_id": seed.global_entity.id,
                "player_character_id": seed.player_character["id"],
                "grant_scope": "full",
            },
        ),
        client.get(f"/api/grimoire/campaigns/{campaign_id}/grants", headers=player),
        client.patch(
            f"/api/grimoire/campaigns/{campaign_id}/grants/{seed.grant_a['id']}",
            headers=player,
            json={"grant_scope": "full"},
        ),
        client.delete(
            f"/api/grimoire/campaigns/{campaign_id}/grants/{seed.grant_a['id']}",
            headers=player,
        ),
        client.post(f"/api/grimoire/campaigns/{campaign_id}/sessions", headers=player),
        client.patch(
            f"/api/grimoire/campaigns/{campaign_id}/sessions/{seed.game_a['id']}",
            headers=player,
            json={"status": "paused"},
        ),
        client.patch(
            "/api/grimoire/books/private-book",
            headers=player,
            json={"display_name": "Forbidden"},
        ),
    ]
    assert [response.status_code for response in attempts] == [403] * len(attempts)

    allowed = client.patch(
        "/api/grimoire/books/private-book",
        headers=_auth(OPERATOR_EMAIL),
        json={"display_name": "Operator Authored"},
    )
    assert allowed.status_code == 200


def test_campaign_creation_never_mints_global_corpus_authority(session, client):
    campaign = _create_campaign(client, DM_EMAIL, "Existing campaign")
    character = _create_character(client, DM_EMAIL, campaign["id"], "Player")
    player_member = _provision(
        client,
        DM_EMAIL,
        campaign["id"],
        PLAYER_EMAIL,
        character["id"],
    )
    session.add(Book(id="global-book", display_name="Original"))
    session.commit()

    denied_player = client.patch(
        "/api/grimoire/books/global-book",
        headers=_auth(PLAYER_EMAIL),
        json={"display_name": "Player edit"},
    )
    assert denied_player.status_code == 403

    player_campaign = _create_campaign(client, PLAYER_EMAIL, "Player-created")
    assert player_campaign["name"] == "Player-created"
    still_denied = client.patch(
        "/api/grimoire/books/global-book",
        headers=_auth(PLAYER_EMAIL),
        json={"display_name": "Minted edit"},
    )
    assert still_denied.status_code == 403

    revoked = client.delete(
        f"/api/grimoire/campaigns/{campaign['id']}/members/{player_member['id']}",
        headers=_auth(DM_EMAIL),
    )
    assert revoked.status_code == 204
    revoked_campaign = _create_campaign(client, PLAYER_EMAIL, "After revocation")
    assert revoked_campaign["name"] == "After revocation"
    revoked_still_denied = client.patch(
        "/api/grimoire/books/global-book",
        headers=_auth(PLAYER_EMAIL),
        json={"display_name": "Revoked edit"},
    )
    assert revoked_still_denied.status_code == 403

    book = session.get(Book, "global-book")
    assert book is not None
    assert book.display_name == "Original"


def test_foreign_object_and_character_ids_cannot_cross_campaigns(session, client):
    seed = _seed_table(session, client)
    dm = _auth(DM_EMAIL)
    campaign_id = seed.campaign_a["id"]

    foreign_entity = client.post(
        f"/api/grimoire/campaigns/{campaign_id}/grants",
        headers=dm,
        json={
            "entity_id": seed.private_b.id,
            "player_character_id": seed.player_character["id"],
            "grant_scope": "full",
        },
    )
    assert foreign_entity.status_code == 404

    foreign_character = client.post(
        f"/api/grimoire/campaigns/{campaign_id}/grants",
        headers=dm,
        json={
            "entity_id": seed.global_entity.id,
            "player_character_id": seed.foreign_character["id"],
            "grant_scope": "full",
        },
    )
    assert foreign_character.status_code == 404

    assert (
        client.patch(
            f"/api/grimoire/campaigns/{campaign_id}/grants/{seed.grant_b['id']}",
            headers=dm,
            json={"grant_scope": "partial"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/grimoire/campaigns/{campaign_id}/grants/{seed.grant_b['id']}",
            headers=dm,
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/grimoire/campaigns/{campaign_id}/sessions/{seed.game_b['id']}",
            headers=dm,
            json={"status": "ended"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/grimoire/campaigns/{campaign_id}/members/{seed.foreign_member['id']}",
            headers=dm,
        ).status_code
        == 404
    )

    client.app.dependency_overrides[get_grimoire_operator_email] = lambda: DM_EMAIL
    provision_foreign_character = client.post(
        f"/api/grimoire/campaigns/{campaign_id}/members",
        headers=dm,
        json={
            "email": "foreign-character@example.test",
            "player_character_id": seed.foreign_character["id"],
        },
    )
    del client.app.dependency_overrides[get_grimoire_operator_email]
    assert provision_foreign_character.status_code == 404


def test_grant_session_provenance_must_belong_to_campaign(session, client):
    seed = _seed_table(session, client)
    own_entity = Entity(entity_type="npc", name="Own session grant", is_global=True)
    foreign_entity = Entity(
        entity_type="npc", name="Foreign session grant", is_global=True
    )
    missing_entity = Entity(
        entity_type="npc", name="Missing session grant", is_global=True
    )
    session.add(own_entity)
    session.add(foreign_entity)
    session.add(missing_entity)
    session.commit()
    for entity in (own_entity, foreign_entity, missing_entity):
        session.refresh(entity)

    base = f"/api/grimoire/campaigns/{seed.campaign_a['id']}/grants"
    common = {
        "player_character_id": seed.player_character["id"],
        "grant_scope": "full",
    }
    foreign = client.post(
        base,
        headers=_auth(DM_EMAIL),
        json={
            **common,
            "entity_id": foreign_entity.id,
            "granted_in_session": seed.game_b["id"],
        },
    )
    assert foreign.status_code == 404

    missing_session_id = "00000000-0000-0000-0000-000000000099"
    missing = client.post(
        base,
        headers=_auth(DM_EMAIL),
        json={
            **common,
            "entity_id": missing_entity.id,
            "granted_in_session": missing_session_id,
        },
    )
    assert missing.status_code == 404

    own = client.post(
        base,
        headers=_auth(DM_EMAIL),
        json={
            **common,
            "entity_id": own_entity.id,
            "granted_in_session": seed.game_a["id"],
        },
    )
    assert own.status_code == 200
    assert own.json()["granted_in_session"] == seed.game_a["id"]

    grants = session.exec(
        select(KnowledgeGrant).where(
            KnowledgeGrant.entity_id.in_(
                [own_entity.id, foreign_entity.id, missing_entity.id]
            )
        )
    ).all()
    assert [(grant.entity_id, grant.granted_in_session) for grant in grants] == [
        (own_entity.id, seed.game_a["id"])
    ]


def test_query_viewpoint_tampering_never_changes_player_authority(session, client):
    seed = _seed_table(session, client)
    player = _auth(PLAYER_EMAIL)
    base = f"/api/grimoire/campaigns/{seed.campaign_a['id']}"

    partial = client.get(
        f"{base}/entities/{seed.private_a.id}?as=dm",
        headers=player,
    )
    assert partial.status_code == 200
    assert partial.json()["revealed_details"] == {"known": "safe"}
    assert "source_type" not in partial.json()

    listed = client.get(
        f"{base}/entities?as={seed.foreign_character['id']}",
        headers=player,
    ).json()["items"]
    visible_ids = {item["id"] for item in listed}
    assert seed.private_a.id in visible_ids
    assert seed.ungranted_a.id not in visible_ids
    assert seed.private_b.id not in visible_ids

    relationships = client.get(
        f"{base}/entities/{seed.private_a.id}/relationships?as=dm",
        headers=player,
    )
    assert relationships.status_code == 200
    assert relationships.json() == []

    chunk = client.get(
        f"/api/grimoire/chunks/{seed.chunk.id}?campaign={seed.campaign_a['id']}&as=dm",
        headers=player,
    ).json()
    chip = next(item for item in chunk["entities"] if item["id"] == seed.private_a.id)
    assert chip["revealed_details"] == {"known": "safe"}
    assert "grants" not in chip


def test_revocation_takes_effect_on_next_request_without_stale_authority(
    session, client
):
    seed = _seed_table(session, client)
    player = _auth(PLAYER_EMAIL)
    entities_url = f"/api/grimoire/campaigns/{seed.campaign_a['id']}/entities"

    assert client.get(entities_url, headers=player).status_code == 200
    revoked = client.delete(
        f"/api/grimoire/campaigns/{seed.campaign_a['id']}"
        f"/members/{seed.player_member['id']}",
        headers=_auth(DM_EMAIL),
    )
    assert revoked.status_code == 204
    assert client.get(entities_url, headers=player).status_code == 404
    assert (
        client.get(
            f"/api/grimoire/chunks/{seed.chunk.id}?campaign={seed.campaign_a['id']}",
            headers=player,
        ).status_code
        == 404
    )
