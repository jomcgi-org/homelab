"""Registered identities, campaign ownership, and invitation authorization."""

import pytest
from auth.api import Authority, Principal, PrincipalKind
from core.db import get_session
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire.access import get_authenticated_identity
from grimoire.models import AppUser, CampaignMember
from grimoire.router import router

ISSUER = "https://auth.example/application/o/grimoire/"


@pytest.fixture
def table(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'onboarding.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {table: table.schema for table in SQLModel.metadata.tables.values()}
    try:
        for table in schemas:
            table.schema = None
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table, schema in schemas.items():
            table.schema = schema
        engine.dispose()


@pytest.fixture
def client(table):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: table

    def identity(request: Request):
        subject = request.headers.get("test-subject")
        if not subject:
            raise HTTPException(403, "missing identity")
        return Principal(
            subject=subject,
            issuer=request.headers.get("test-issuer", ISSUER),
            display_name=request.headers.get("test-name", subject),
            email=request.headers.get("test-email", f"{subject}@example.test"),
            email_verified=request.headers.get("test-verified") == "true",
            actor=(),
            scope=(),
            groups=("operators",) if subject == "admin" else (),
            kind=PrincipalKind.HUMAN,
            authority=Authority.STANDING,
        )

    app.dependency_overrides[get_authenticated_identity] = identity
    with TestClient(app) as client:
        yield client


def headers(user, **extra):
    return {"test-subject": user, **extra}


def register(client, user, **extra):
    response = client.get("/api/grimoire/lobby", headers=headers(user, **extra))
    assert response.status_code == 200, response.text
    return response.json()


def campaign(client, user="owner"):
    response = client.post(
        "/api/grimoire/campaigns", headers=headers(user), json={"name": "Our table"}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def invite(client, campaign_id, player="player", owner="owner"):
    response = client.post(
        f"/api/grimoire/campaigns/{campaign_id}/invitations",
        headers=headers(owner),
        json={"email": f"{player}@example.test"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def decide(client, invitation_id, user="player", decision="accept"):
    return client.post(
        f"/api/grimoire/invitations/{invitation_id}/{decision}", headers=headers(user)
    )


def test_registration_is_idempotent_and_identity_survives_email_changes(table, client):
    first = register(client, "owner")
    cid = campaign(client)
    changed = register(
        client, "owner", **{"test-email": "new@example.test", "test-name": "New name"}
    )
    assert changed["user"]["id"] == first["user"]["id"]
    assert changed["user"]["display_name"] == "New name"
    assert changed["campaigns"][0]["id"] == cid
    assert changed["campaigns"][0]["is_owner"] is True
    assert len(table.exec(select(AppUser)).all()) == 1


def test_other_subject_or_issuer_cannot_claim_email(table, client):
    register(client, "owner")
    cid = campaign(client)
    for impostor in [
        headers("impostor", **{"test-email": "owner@example.test"}),
        headers("owner", **{"test-issuer": "https://other.example/"}),
    ]:
        assert client.get("/api/grimoire/lobby", headers=impostor).status_code == 409
        assert (
            client.get(f"/api/grimoire/campaigns/{cid}", headers=impostor).status_code
            == 409
        )
    assert len(table.exec(select(AppUser)).all()) == 1


def test_legacy_rows_require_verified_mailbox_to_link(table, client):
    legacy = AppUser(email="player@example.test")
    table.add(legacy)
    table.commit()
    original_id = legacy.id
    assert (
        client.get("/api/grimoire/lobby", headers=headers("player")).status_code == 409
    )
    user = register(client, "player", **{"test-verified": "true"})["user"]
    assert user["id"] == original_id


def test_pending_invite_grants_nothing_and_only_recipient_can_accept(table, client):
    cid = campaign(client)
    register(client, "player")
    iid = invite(client, cid)
    assert register(client, "player")["campaigns"] == []
    assert register(client, "stranger")["invitations"] == []
    assert (
        client.get(
            f"/api/grimoire/campaigns/{cid}", headers=headers("player")
        ).status_code
        == 404
    )
    assert decide(client, iid, "stranger").status_code == 404
    assert decide(client, iid).status_code == 200
    lobby = register(client, "player")
    assert lobby["invitations"] == []
    assert lobby["campaigns"][0]["role"] == "player"
    assert lobby["campaigns"][0]["is_owner"] is False
    assert decide(client, iid).status_code == 409
    assert len(table.exec(select(CampaignMember)).all()) == 2


def test_owner_invites_only_registered_users_and_cannot_bypass_acceptance(client):
    cid = campaign(client)
    url = f"/api/grimoire/campaigns/{cid}"
    assert (
        client.post(
            url + "/invitations",
            headers=headers("owner"),
            json={"email": "absent@example.test"},
        ).status_code
        == 404
    )
    register(client, "player")
    assert (
        client.post(
            url + "/members",
            headers=headers("owner"),
            json={"email": "player@example.test"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            url + "/invitations",
            headers=headers("player"),
            json={"email": "owner@example.test"},
        ).status_code
        == 404
    )
    iid = invite(client, cid)
    assert iid == invite(client, cid)
    assert decide(client, iid).status_code == 200
    assert (
        client.post(
            url + "/invitations",
            headers=headers("player"),
            json={"email": "owner@example.test"},
        ).status_code
        == 403
    )
    own = campaign(client, "player")
    assert own != cid
    assert register(client, "player")["campaigns"][1]["is_owner"] is True


def test_decline_cancel_and_revocation_cannot_be_replayed(client):
    cid = campaign(client)
    register(client, "player")
    first = invite(client, cid)
    assert decide(client, first, decision="decline").status_code == 200
    second = invite(client, cid)
    assert second != first
    assert decide(client, first).status_code == 404
    path = f"/api/grimoire/campaigns/{cid}/invitations/{second}"
    assert client.delete(path, headers=headers("player")).status_code == 404
    assert client.delete(path, headers=headers("owner")).status_code == 204
    assert decide(client, second).status_code == 409
    third = invite(client, cid)
    assert decide(client, third).status_code == 200
    members = client.get(
        f"/api/grimoire/campaigns/{cid}/members", headers=headers("owner")
    ).json()
    player = next(row for row in members if row["role"] == "player")
    assert (
        client.delete(
            f"/api/grimoire/campaigns/{cid}/members/{player['id']}",
            headers=headers("owner"),
        ).status_code
        == 204
    )
    assert decide(client, third).status_code == 409
    assert (
        client.get(
            f"/api/grimoire/campaigns/{cid}", headers=headers("player")
        ).status_code
        == 404
    )


def test_dm_cannot_manage_owner_invitations_and_cross_campaign_cancel_fails(
    table, client
):
    cid = campaign(client)
    other = campaign(client, "other")
    registered = register(client, "dm")["user"]
    table.add(CampaignMember(campaign_id=cid, app_user_id=registered["id"], role="dm"))
    table.commit()
    register(client, "player")
    iid = invite(client, cid)
    assert (
        client.post(
            f"/api/grimoire/campaigns/{cid}/invitations",
            headers=headers("dm"),
            json={"email": "player@example.test"},
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/grimoire/campaigns/{other}/invitations/{iid}",
            headers=headers("other"),
        ).status_code
        == 404
    )


def test_account_admin_link_is_not_a_campaign_owner_entitlement(client):
    campaign(client)
    assert register(client, "owner")["can_administer_accounts"] is False
    assert register(client, "admin")["can_administer_accounts"] is True
    assert client.get("/api/grimoire/lobby").status_code == 403
    assert (
        client.post(
            "/api/grimoire/campaigns", headers=headers("owner"), json={"name": "   "}
        ).status_code
        == 422
    )


def test_legacy_cloudflare_members_can_keep_using_their_existing_identity(
    table, client, monkeypatch
):
    monkeypatch.setenv("AUTH_CLOUDFLARE_ACCESS_ISSUER", ISSUER)
    legacy = AppUser(email="owner@example.test")
    table.add(legacy)
    table.commit()
    original_id = legacy.id
    assert register(client, "owner")["user"]["id"] == original_id


def test_authorization_uses_registered_id_after_concurrent_email_change(table, client):
    from grimoire.router import _get_member_or_404, _require_owner

    cid = campaign(client)
    owner = table.exec(
        select(AppUser).where(AppUser.email == "owner@example.test")
    ).one()
    owner_id = owner.id
    owner.email = "changed@example.test"
    table.add(owner)
    table.add(AppUser(email="owner@example.test", issuer=ISSUER, subject="other"))
    table.commit()
    # Simulate another request changing the email after this request verified
    # its principal, before a campaign handler queries the current membership.
    assert table.info["grimoire_user_id"] == owner_id
    assert _get_member_or_404(table, cid, "owner@example.test").app_user_id == owner_id
    assert _require_owner(table, cid, "owner@example.test").id == owner_id


def test_signed_grimoire_token_is_revalidated_and_not_an_operator_bearer(
    table, monkeypatch
):
    import json
    import time

    import jwt
    from auth.api import AuthSettings, AuthentikStandingVerifier
    from auth.verifier import TokenResolver
    from cryptography.hazmat.primitives.asymmetric import rsa

    from grimoire import access

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "grimoire", "alg": "RS256"})

    async def fetch(_url):
        return {"keys": [jwk]}

    settings = AuthSettings(
        authentik_jwks_url=f"{ISSUER}jwks/",
        authentik_issuer=ISSUER,
        authentik_audience="grimoire-friends",
        jwks_cache_ttl_s=300,
    )
    verifier = AuthentikStandingVerifier(settings, fetch=fetch)
    monkeypatch.setattr(access, "grimoire_verifier", lambda: verifier)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: table
    # Keep normal bearer resolution. No operator resolver accepts this issuer.
    app.state.auth_resolver = TokenResolver([])

    def token(**overrides):
        claims = {
            "iss": ISSUER,
            "sub": "new-player",
            "aud": "grimoire-friends",
            "exp": int(time.time()) + 300,
            "email": "player@example.test",
            "name": "Player",
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "grimoire"})

    with TestClient(app) as browser:
        good = token()
        response = browser.get(
            "/api/grimoire/lobby", headers={"x-grimoire-token": good}
        )
        assert response.status_code == 200
        assert response.json()["user"]["display_name"] == "Player"
        assert (
            browser.get(
                "/api/grimoire/lobby", headers={"authorization": f"Bearer {good}"}
            ).status_code
            == 401
        )
        for invalid in [
            "forged",
            token(aud="moving-friends"),
            token(exp=1),
            token(iss="https://foreign.example/"),
        ]:
            assert browser.get(
                "/api/grimoire/lobby", headers={"x-grimoire-token": invalid}
            ).status_code in (401, 403)
        assert (
            browser.get(
                "/api/grimoire/lobby", headers={"x-auth-email": "player@example.test"}
            ).status_code
            == 403
        )
        assert (
            browser.get(
                "/api/grimoire/lobby",
                headers={
                    "x-grimoire-token": good,
                    "x-auth-email": "other@example.test",
                },
            ).status_code
            == 403
        )
        assert (
            browser.get(
                "/api/grimoire/lobby",
                headers=[("x-grimoire-token", good), ("x-grimoire-token", good)],
            ).status_code
            == 401
        )
