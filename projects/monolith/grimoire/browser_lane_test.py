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
from sqlmodel import Session, SQLModel, create_engine

from grimoire import access, register
from grimoire.browser_lane import (
    BROWSER_API_PREFIXES,
    GRIMOIRE_API_PREFIXES,
    is_browser_path,
)

ISSUER = "https://auth.example.test/application/o/grimoire/"
ORIGIN = "https://friends.example.test"


@pytest.fixture(name="browser")
def browser_fixture(tmp_path, monkeypatch):
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
    )
    assert BROWSER_API_PREFIXES[: len(GRIMOIRE_API_PREFIXES)] == GRIMOIRE_API_PREFIXES
    assert is_browser_path("/api/grimoire/campaigns/abc/notes")
    assert not is_browser_path("/api/grimoire/campaigns-export")
    assert not is_browser_path("/api/grimoire/chunks/abc/image")
