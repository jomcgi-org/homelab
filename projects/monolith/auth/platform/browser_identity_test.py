"""The friends lane's session cookie as a platform credential, with CSRF rules."""

import json
import time

import jwt
import pytest
from core.db import get_session
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from auth.api import AuthentikStandingVerifier, AuthSettings
from auth.browser_cookie import BROWSER_COOKIE, PLATFORM_BROWSER_API_PREFIXES
from auth.platform import identity
from auth.platform.router import router
from auth.verifier import TokenResolver

ISSUER = "https://auth.example.test/application/o/grimoire/"
AUDIENCE = "grimoire-friends"
ORIGIN = "https://friends.example.test"
SELF = "/api/auth/platform/self"
COMMANDS = "/api/auth/platform/commands"


@pytest.fixture(name="browser")
def browser_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_AUTH_LOGIN_ISSUER", ISSUER)
    monkeypatch.setenv("GRIMOIRE_BROWSER_ORIGIN", ORIGIN)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'browser-identity.db'}",
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"platform_auth": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            table
            for table in SQLModel.metadata.sorted_tables
            if table.schema == "platform_auth"
        ],
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "login", "alg": "RS256"})

    async def fetch(_url):
        return {"keys": [jwk]}

    verifier = AuthentikStandingVerifier(
        AuthSettings(
            authentik_jwks_url=f"{ISSUER}jwks/",
            authentik_issuer=ISSUER,
            authentik_audience=AUDIENCE,
            jwks_cache_ttl_s=300,
            allow_username_identity=True,
        ),
        fetch=fetch,
    )
    monkeypatch.setattr(identity, "login_verifier", lambda: verifier)

    def token(**overrides):
        claims = {
            "iss": ISSUER,
            "sub": "operator",
            "aud": AUDIENCE,
            "exp": int(time.time()) + 300,
            "email": "owner@example.test",
            "name": "Owner",
            "preferred_username": "owner",
            "platform_user_type": "internal",
            "groups": ["operators"],
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "login"})

    def session():
        with Session(engine) as connection:
            yield connection

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = session
    # No bearer credential resolves, so only the login token can authenticate.
    app.state.auth_resolver = TokenResolver([])
    with TestClient(app) as client:
        client.token = token
        yield client
    engine.dispose()


def _cookie(token):
    return {"cookie": f"{BROWSER_COOKIE}={token}"}


def _bootstrap(request_id="bootstrap-owner"):
    return {
        "action": "bootstrap",
        "request_id": request_id,
        "reason": "Explicit operator import",
    }


def test_session_cookie_is_a_verified_platform_credential(browser):
    cookie = _cookie(browser.token())
    created = browser.post(
        COMMANDS, json=_bootstrap(), headers={**cookie, "origin": ORIGIN}
    )
    assert created.status_code == 200
    profile = browser.get(SELF, headers=cookie)
    assert profile.status_code == 200
    assert profile.json()["username"] == "owner"
    users = browser.get("/api/auth/platform/management/users", headers=cookie)
    assert [user["username"] for user in users.json()["items"]] == ["owner"]
    assert users.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "overrides",
    [{"aud": "moving-friends"}, {"exp": 1}, {"iss": "https://foreign.example/"}],
)
def test_cookie_token_is_verified_like_the_header_token(browser, overrides):
    token = browser.token(**overrides)
    assert browser.get(SELF, headers=_cookie(token)).status_code in (401, 403)
    assert browser.get(SELF, headers={"x-platform-token": token}).status_code in (
        401,
        403,
    )


def test_cookie_grants_no_more_than_its_signed_groups(browser):
    cookie = _cookie(browser.token(groups=[]))
    denied = browser.post(
        COMMANDS, json=_bootstrap(), headers={**cookie, "origin": ORIGIN}
    )
    assert denied.status_code == 403
    assert (
        browser.get("/api/auth/platform/management/users", headers=cookie).status_code
        == 403
    )


def test_cookie_is_not_a_credential_until_an_origin_is_configured(browser, monkeypatch):
    monkeypatch.delenv("GRIMOIRE_BROWSER_ORIGIN")
    cookie = _cookie(browser.token())
    assert browser.get(SELF, headers=cookie).status_code == 403
    written = browser.post(
        COMMANDS, json=_bootstrap(), headers={**cookie, "origin": ORIGIN}
    )
    assert written.status_code == 403
    # The header transport does not depend on the browser switch.
    assert (
        browser.post(
            COMMANDS, json=_bootstrap(), headers={"x-platform-token": browser.token()}
        ).status_code
        == 200
    )


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
        COMMANDS, json=_bootstrap(), headers={**_cookie(browser.token()), **headers}
    )
    assert response.status_code == status
    if status == 403:
        assert response.json() == {"detail": "cross-origin request refused"}
        assert response.headers["cache-control"] == "no-store"


def test_invitation_delivery_is_a_guarded_write(browser):
    cookie = {**_cookie(browser.token()), "origin": ORIGIN}
    browser.post(COMMANDS, json=_bootstrap(), headers=cookie)
    issued = browser.post(
        COMMANDS,
        json={
            "action": "issue",
            "request_id": "issue-friend",
            "reason": "Invite a friend",
            "arguments": {"recipient_label": "Friend", "expires_in_days": 7},
        },
        headers=cookie,
    ).json()
    path = f"/api/auth/platform/invitations/{issued['id']}/deliver"
    body = {"request_id": "deliver-friend", "reason": "Share privately"}
    foreign = {**cookie, "origin": "https://evil.example"}
    assert browser.post(path, json=body, headers=foreign).status_code == 403
    delivered = browser.post(path, json=body, headers=cookie)
    assert delivered.status_code == 200
    assert len(delivered.json()["token"]) == 43


def test_repeated_origin_header_fails_closed(browser):
    response = browser.post(
        COMMANDS,
        json=_bootstrap(),
        headers=[
            ("cookie", f"{BROWSER_COOKIE}={browser.token()}"),
            ("origin", ORIGIN),
            ("origin", "https://evil.example"),
        ],
    )
    assert response.status_code == 403


def test_header_token_writes_need_no_origin(browser):
    # The Svelte BFF calls from inside the cluster and sends no Origin.
    headers = {"x-platform-token": browser.token()}
    assert browser.post(COMMANDS, json=_bootstrap(), headers=headers).status_code == 200
    assert browser.get(SELF, headers=headers).json()["username"] == "owner"


def test_header_token_takes_precedence_over_the_cookie(browser):
    browser.post(
        COMMANDS, json=_bootstrap(), headers={"x-platform-token": browser.token()}
    )
    # A cross-site write is judged by the header credential, not the cookie.
    response = browser.post(
        COMMANDS,
        json=_bootstrap("bootstrap-again"),
        headers={
            "x-platform-token": browser.token(),
            **_cookie(browser.token(groups=[])),
            "origin": "https://evil.example",
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        [("x-platform-token", "a"), ("x-platform-token", "b")],
        [("x-platform-token", "not-a-token")],
        [("x-platform-token", "")],
    ],
)
def test_ambiguous_or_invalid_header_is_never_rescued_by_the_cookie(browser, headers):
    browser.post(
        COMMANDS, json=_bootstrap(), headers={"x-platform-token": browser.token()}
    )
    response = browser.get(
        SELF, headers=[*headers, ("cookie", f"{BROWSER_COOKIE}={browser.token()}")]
    )
    assert response.status_code in (401, 403)


def test_no_credential_is_anonymous(browser):
    assert browser.get(SELF).status_code == 403
    assert browser.post(COMMANDS, json=_bootstrap()).status_code == 403


def _guarded(dependant):
    return dependant.call is identity.browser_or_operator or any(
        _guarded(child) for child in dependant.dependencies
    )


def test_only_capability_enrollment_routes_skip_the_identity_dependency():
    """Enrollment callbacks carry their own capability; keep them off the browser lane."""
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    assert len(routes) == 7
    unguarded = sorted(route.path for route in routes if not _guarded(route.dependant))
    assert unguarded == [
        "/api/auth/platform/enrollment/acknowledge",
        "/api/auth/platform/enrollment/complete",
        "/api/auth/platform/enrollment/validate",
    ]
    # The gateway routes exactly the guarded routes and none of the others.
    routed = lambda path: any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in PLATFORM_BROWSER_API_PREFIXES
    )
    assert not any(routed(path) for path in unguarded)
    assert all(routed(route.path) for route in routes if route.path not in unguarded)
