"""The browser-facing Grimoire API: cookie credential, CSRF and route audit."""

from __future__ import annotations

import json
import time

import jwt
import pytest
from auth.api import AuthentikStandingVerifier, AuthSettings
from auth.verifier import TokenResolver
from core.db import get_session
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire import access, register
from grimoire.browser_lane import (
    BROWSER_API_PREFIXES,
    GRIMOIRE_API_PREFIXES,
    is_browser_path,
)
from grimoire.models import Campaign, CampaignMember

ISSUER = "https://auth.example.test/application/o/grimoire/"
ORIGIN = "https://friends.example.test"


@pytest.fixture(name="browser")
def browser_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_ENFORCEMENT_ENABLED", "false")
    monkeypatch.delenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", raising=False)
    monkeypatch.delenv("GRIMOIRE_CREATOR_EMAILS", raising=False)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'browser-lane.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "grimoire", "alg": "RS256"})

    async def fetch(_url):
        return {"keys": [jwk]}

    verifier = AuthentikStandingVerifier(
        AuthSettings(
            authentik_jwks_url=f"{ISSUER}jwks/",
            authentik_issuer=ISSUER,
            authentik_audience="grimoire-friends",
            jwks_cache_ttl_s=300,
        ),
        fetch=fetch,
    )
    monkeypatch.setattr(access, "grimoire_verifier", lambda: verifier)
    monkeypatch.setenv("GRIMOIRE_BROWSER_ORIGIN", ORIGIN)

    def token(**overrides):
        claims = {
            "iss": ISSUER,
            "sub": "player-one",
            "aud": "grimoire-friends",
            "exp": int(time.time()) + 300,
            "email": "player@example.test",
            "name": "Player",
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "grimoire"})

    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            app = FastAPI()
            register(app)
            app.dependency_overrides[get_session] = lambda: session
            app.state.auth_resolver = TokenResolver([])
            with TestClient(app) as client:
                client.token = token
                client.table = session
                yield client
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


def _cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{access.BROWSER_COOKIE}={token}"}


def test_session_cookie_is_a_verified_credential(browser):
    response = browser.get("/api/grimoire/lobby", headers=_cookie(browser.token()))
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "player@example.test"


@pytest.mark.parametrize(
    "overrides",
    [{"aud": "moving-friends"}, {"exp": 1}, {"iss": "https://foreign.example/"}],
)
def test_cookie_token_is_verified_like_the_header_token(browser, overrides):
    response = browser.get(
        "/api/grimoire/lobby", headers=_cookie(browser.token(**overrides))
    )
    assert response.status_code in (401, 403)


def test_cookie_is_not_a_credential_until_an_origin_is_configured(browser, monkeypatch):
    monkeypatch.delenv("GRIMOIRE_BROWSER_ORIGIN")
    response = browser.get("/api/grimoire/lobby", headers=_cookie(browser.token()))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"origin": ORIGIN}, 200),
        ({"origin": ORIGIN, "sec-fetch-site": "same-origin"}, 200),
        # A form post from another site still carries the cookie.
        ({"origin": "https://evil.example"}, 403),
        ({"origin": "null"}, 403),
        ({}, 403),
        # A sibling subdomain is same-site but not same-origin.
        ({"origin": ORIGIN, "sec-fetch-site": "same-site"}, 403),
        ({"origin": ORIGIN, "sec-fetch-site": "cross-site"}, 403),
    ],
)
def test_cookie_writes_require_the_configured_origin(browser, headers, status):
    response = browser.post(
        "/api/grimoire/campaigns",
        json={"name": "Curse of Strahd"},
        headers={**_cookie(browser.token()), **headers},
    )
    assert response.status_code == status


@pytest.mark.parametrize(
    "headers,status", [({"origin": ORIGIN}, 201), ({"origin": "null"}, 403), ({}, 403)]
)
def test_feedback_writes_share_the_cookie_rules(browser, headers, status):
    response = browser.post(
        "/api/grimoire/feedback",
        json={"text": "The dice feel slow.", "page": "/"},
        headers={**_cookie(browser.token()), **headers},
    )
    assert response.status_code == status


def test_repeated_origin_header_fails_closed(browser):
    response = browser.post(
        "/api/grimoire/campaigns",
        json={"name": "Curse of Strahd"},
        headers=[
            ("cookie", f"{access.BROWSER_COOKIE}={browser.token()}"),
            ("origin", ORIGIN),
            ("origin", "https://evil.example"),
        ],
    )
    assert response.status_code == 403


def test_header_token_writes_need_no_origin(browser):
    # The Svelte BFF calls from inside the cluster and sends no Origin.
    response = browser.post(
        "/api/grimoire/campaigns",
        json={"name": "Curse of Strahd"},
        headers={"x-grimoire-token": browser.token()},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "allowed,verified,expected",
    [
        ("", True, False),
        (", ,", True, False),
        ("other@example.test", True, False),
        ("player@example.test", False, False),
        ("player@example.test", "true", False),
        ("player@example.test", None, False),
        ("player@example.test", True, True),
        ("other@example.test, PLAYER@EXAMPLE.TEST ", True, True),
    ],
)
def test_local_creation_policy_at_cookie_routes(
    browser, monkeypatch, allowed, verified, expected
):
    monkeypatch.setenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_CREATOR_EMAILS", allowed)
    headers = {
        **_cookie(browser.token(email_verified=verified)),
        "origin": ORIGIN,
    }
    lobby = browser.get("/api/grimoire/lobby", headers=headers)
    assert lobby.status_code == 200, lobby.text
    assert lobby.json()["can_create_game"] is expected
    response = browser.post(
        "/api/grimoire/campaigns", json={"name": "Our table"}, headers=headers
    )
    assert response.status_code == (200 if expected else 403), response.text
    # Account synchronization may register a principal, but denial must not
    # create either a campaign or its initial DM membership.
    campaigns = browser.table.exec(select(Campaign)).all()
    members = browser.table.exec(select(CampaignMember)).all()
    assert len(campaigns) == len(members) == int(expected)
    if expected:
        assert members[0].role == "dm"
        assert campaigns[0].owner_app_user_id == members[0].app_user_id


@pytest.mark.parametrize("enabled", [None, "false"])
def test_disabled_creation_policy_keeps_signed_email_compatibility(
    browser, monkeypatch, enabled
):
    if enabled is not None:
        monkeypatch.setenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", enabled)
    monkeypatch.setenv("GRIMOIRE_CREATOR_EMAILS", "other@example.test")
    headers = {"x-grimoire-token": browser.token(email_verified=False)}
    assert (
        browser.get("/api/grimoire/lobby", headers=headers).json()["can_create_game"]
        is True
    )
    assert (
        browser.post(
            "/api/grimoire/campaigns", json={"name": "Compatible"}, headers=headers
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "claims,projection",
    [
        ({"email": "other@example.test", "email_verified": True}, None),
        (
            {"email": "other@example.test", "email_verified": True},
            "player@example.test",
        ),
        (
            {"email": "player@example.test", "email_verified": True},
            "other@example.test",
        ),
        ({"email": None, "email_verified": True}, None),
        ({"aud": "wrong", "email_verified": True}, None),
        ({"email_verified": True, "platform_user_type": "service_account"}, None),
    ],
)
def test_creation_restriction_uses_verified_identity_not_request_fields(
    browser, monkeypatch, claims, projection
):
    monkeypatch.setenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_CREATOR_EMAILS", "player@example.test")
    headers = {"x-grimoire-token": browser.token(**claims)}
    if projection:
        headers["x-auth-email"] = projection
    lobby = browser.get("/api/grimoire/lobby", headers=headers)
    assert lobby.status_code in (200, 401, 403)
    if lobby.status_code == 200:
        assert lobby.json()["can_create_game"] is False
    response = browser.post(
        "/api/grimoire/campaigns",
        json={"name": "Spoofed", "email": "player@example.test", "role": "dm"},
        headers=headers,
    )
    assert response.status_code in (401, 403), response.text
    assert browser.table.exec(select(Campaign)).all() == []
    assert browser.table.exec(select(CampaignMember)).all() == []


def test_projection_alone_cannot_create_an_allowlisted_campaign(browser, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_CREATOR_EMAILS", "player@example.test")
    for path in ("/api/grimoire/lobby", "/api/grimoire/campaigns"):
        response = browser.request(
            "GET" if path.endswith("lobby") else "POST",
            path,
            headers={"x-auth-email": "player@example.test"},
            json={"name": "Spoofed"},
        )
        assert response.status_code == 403
    assert browser.table.exec(select(Campaign)).all() == []
    assert browser.table.exec(select(CampaignMember)).all() == []


def test_creation_normalizes_the_signed_mailbox(browser, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_CREATOR_EMAILS", "player@example.test")
    headers = {
        "x-grimoire-token": browser.token(
            email=" PLAYER@EXAMPLE.TEST ", email_verified=True
        )
    }
    assert (
        browser.get("/api/grimoire/lobby", headers=headers).json()["can_create_game"]
        is True
    )
    assert (
        browser.post(
            "/api/grimoire/campaigns", json={"name": "Normalized"}, headers=headers
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "trusted_issuer,expected", [(ISSUER, 200), ("https://other.test/", 403)]
)
def test_creation_preserves_trusted_cloudflare_mailbox_proof(
    browser, monkeypatch, trusted_issuer, expected
):
    monkeypatch.setenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_CREATOR_EMAILS", "player@example.test")
    monkeypatch.setenv("AUTH_CLOUDFLARE_ACCESS_ISSUER", trusted_issuer)
    # The verifier has checked this token's signature and issuer. The configured
    # Access issuer supplies mailbox proof without an email_verified claim.
    headers = {"x-grimoire-token": browser.token()}
    assert browser.get("/api/grimoire/lobby", headers=headers).json()[
        "can_create_game"
    ] is (expected == 200)
    assert (
        browser.post(
            "/api/grimoire/campaigns", json={"name": "Access identity"}, headers=headers
        ).status_code
        == expected
    )


def test_existing_owner_and_dm_cannot_create_when_not_allowlisted(browser, monkeypatch):
    headers = {"x-grimoire-token": browser.token(email_verified=True)}
    first = browser.post(
        "/api/grimoire/campaigns", json={"name": "Existing"}, headers=headers
    )
    assert first.status_code == 200
    monkeypatch.setenv("GRIMOIRE_CREATION_RESTRICTION_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_CREATOR_EMAILS", "other@example.test")
    lobby = browser.get("/api/grimoire/lobby", headers=headers).json()
    assert lobby["can_create_game"] is False
    assert lobby["campaigns"][0]["role"] == "dm"
    assert lobby["campaigns"][0]["is_owner"] is True
    assert (
        browser.post(
            "/api/grimoire/campaigns", json={"name": "Denied"}, headers=headers
        ).status_code
        == 403
    )
    assert len(browser.table.exec(select(Campaign)).all()) == 1
    assert len(browser.table.exec(select(CampaignMember)).all()) == 1


def _depends_on(dependant, call) -> bool:
    return dependant.call is call or any(
        _depends_on(child, call) for child in dependant.dependencies
    )


def _routes():
    app = FastAPI()
    register(app)
    for context in iter_route_contexts(app.routes):
        if not context.path.startswith("/api/"):
            continue  # FastAPI's own docs routes.
        assert isinstance(context.original_route, APIRoute), context.path
        guarded = _depends_on(
            context.original_route.dependant, access.get_authenticated_identity
        )
        yield context.path, sorted(context.methods), guarded


def test_every_browser_routed_path_enforces_grimoire_identity():
    routed = [route for route in _routes() if is_browser_path(route[0])]
    assert len(routed) > 50, "the campaign API should be browser-routed"
    assert [route for route in routed if not route[2]] == []


def test_routes_without_an_identity_check_stay_off_the_browser_lane():
    """Library reads rely on the private lane's gate, so never route them."""
    open_paths = {path for path, _methods, guarded in _routes() if not guarded}
    assert "/api/grimoire/books" in open_paths
    assert not any(is_browser_path(path) for path in open_paths)


def test_prefixes_match_whole_segments():
    assert GRIMOIRE_API_PREFIXES == (
        "/api/grimoire/lobby",
        "/api/grimoire/campaigns",
        "/api/grimoire/invitations",
        "/api/grimoire/feedback",
    )
    assert BROWSER_API_PREFIXES[: len(GRIMOIRE_API_PREFIXES)] == GRIMOIRE_API_PREFIXES
    assert is_browser_path("/api/grimoire/campaigns/abc/notes")
    assert not is_browser_path("/api/grimoire/campaigns-export")
    assert not is_browser_path("/api/grimoire/chunks/abc/image")
