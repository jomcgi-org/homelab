"""Tests for shared FastAPI and mounted-ASGI request authentication."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

from auth.dependencies import current_principal, get_principal, resolve_authorization
from auth.errors import AuthError, AuthErrorReason
from auth.middleware import PrincipalMiddleware
from auth.principal import Authority, Principal, PrincipalKind
from auth.verifier import TokenResolver


class MappingVerifier:
    def __init__(self, token: str, principal: Principal) -> None:
        self.token = token
        self.principal = principal

    async def verify(self, token: str):
        if token == self.token:
            return self.principal
        return None


@pytest.fixture
def human_principal() -> Principal:
    return Principal(
        subject="human-1",
        actor=(),
        scope=("openid", "tools:read"),
        groups=("friends",),
        email="human@example.com",
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Basic abc123", "Digest value"])
async def test_absent_or_non_bearer_authorization_is_anonymous(authorization):
    resolver = TokenResolver([])

    principal = await resolve_authorization(authorization, resolver)

    assert principal.authority is Authority.ANONYMOUS
    assert principal.subject == "anonymous"
    assert principal.groups == ()
    assert principal.scope == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["Bearer", "Bearer ", "Bearer     "])
async def test_empty_bearer_credential_is_invalid_not_anonymous(authorization):
    with pytest.raises(AuthError) as raised:
        await resolve_authorization(authorization, TokenResolver([]))

    assert raised.value.reason is AuthErrorReason.MALFORMED
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_failure_logging_never_contains_token(caplog):
    secret_token = "highly-secret-token-material-4942"
    caplog.set_level(logging.DEBUG, logger="monolith.auth")

    with pytest.raises(AuthError) as raised:
        await resolve_authorization(
            f"Bearer {secret_token}",
            TokenResolver([]),
        )

    assert raised.value.reason is AuthErrorReason.UNRECOGNIZED
    assert any("unrecognized" in record.message for record in caplog.records)
    assert all(secret_token not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_fastapi_and_mounted_asgi_paths_have_principal_parity(human_principal):
    bearer = "same-token"
    resolver = TokenResolver([MappingVerifier(bearer, human_principal)])

    fastapi_app = FastAPI()
    fastapi_app.state.auth_resolver = resolver

    @fastapi_app.get("/whoami")
    async def whoami(
        request: Request,
        principal: Principal = Depends(get_principal),
    ):
        assert request.state.principal == principal
        assert current_principal() == principal
        return _principal_dict(principal)

    captured: dict = {}

    async def mounted_app(scope, receive, send):
        captured["state"] = scope["state"]["principal"]
        captured["context"] = current_principal()
        response = httpx.Response(200, json={"ok": True})
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    mounted = PrincipalMiddleware(mounted_app, resolver)
    headers = {"Authorization": f"Bearer {bearer}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://test",
    ) as client:
        api_response = await client.get("/whoami", headers=headers)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mounted),
        base_url="http://test",
    ) as client:
        mcp_response = await client.get("/", headers=headers)

    assert api_response.status_code == 200
    assert mcp_response.status_code == 200
    assert api_response.json() == _principal_json(human_principal)
    assert captured["state"] == human_principal
    assert captured["context"] == human_principal


@pytest.mark.asyncio
async def test_fastapi_dependency_does_not_trust_proxy_identity_headers():
    app = FastAPI()
    app.state.auth_resolver = TokenResolver([])

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(get_principal)):
        return _principal_dict(principal)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/whoami",
            headers={"X-Auth-Email": "forged@example.com"},
        )

    assert response.status_code == 200
    assert response.json()["authority"] == Authority.ANONYMOUS.value
    assert response.json()["email"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (AuthErrorReason.UNRECOGNIZED, 401),
        (AuthErrorReason.JWKS_UNREACHABLE, 503),
    ],
)
async def test_middleware_renders_auth_error_owner_status(reason, status):
    class FailingVerifier:
        async def verify(self, token: str):
            raise AuthError(reason)

    async def unreachable_app(scope, receive, send):
        raise AssertionError("invalid bearer material reached the application")

    app = PrincipalMiddleware(unreachable_app, TokenResolver([FailingVerifier()]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/",
            headers={"Authorization": "Bearer present-token"},
        )

    assert response.status_code == status
    assert response.json()["reason"] == reason.value
    if status == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"
    else:
        assert "WWW-Authenticate" not in response.headers


def _principal_dict(principal: Principal) -> dict:
    return {
        "subject": principal.subject,
        "actor": principal.actor,
        "scope": principal.scope,
        "groups": principal.groups,
        "email": principal.email,
        "kind": principal.kind,
        "authority": principal.authority,
    }


def _principal_json(principal: Principal) -> dict:
    return {
        "subject": principal.subject,
        "actor": list(principal.actor),
        "scope": list(principal.scope),
        "groups": list(principal.groups),
        "email": principal.email,
        "kind": principal.kind.value,
        "authority": principal.authority.value,
    }


@pytest.mark.asyncio
async def test_bare_token_no_scheme_logging_never_contains_token(caplog):
    """A bare token with no scheme must not be logged even at DEBUG level."""
    secret_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9-secret-material-4942"
    caplog.set_level(logging.DEBUG, logger="monolith.auth")

    # No exception expected; bare tokens are treated as absent headers (return anonymous).
    principal = await resolve_authorization(secret_token, TokenResolver([]))

    assert principal.authority is Authority.ANONYMOUS
    # Verify that even at DEBUG level, no part of the token appears.
    assert all(secret_token not in record.getMessage() for record in caplog.records)
    assert all(
        part not in record.getMessage()
        for part in secret_token.split("-")
        if len(part) > 5  # Avoid checking common words like "secret"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_fastapi_path_auth_error_has_reason_in_body(human_principal):
    """The handler renders `reason` on a route, GIVEN it is registered.

    This test registers it itself, so it proves the handler works and NOT that
    anything installs it. That the private app actually registers it is asserted
    separately in framework/core_test.py, where the app is built the production
    way.
    """
    from auth.api import auth_error_handler

    fastapi_app = FastAPI()
    fastapi_app.state.auth_resolver = TokenResolver([])
    fastapi_app.add_exception_handler(AuthError, auth_error_handler)

    @fastapi_app.get("/whoami")
    async def whoami(principal: Principal = Depends(get_principal)):
        return {"subject": principal.subject}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/whoami",
            headers={"Authorization": "Bearer unrecognized"},
        )

    assert response.status_code == 401
    body = response.json()
    assert "detail" in body
    assert "reason" in body
    assert body["reason"] == AuthErrorReason.UNRECOGNIZED.value
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_middleware_and_fastapi_paths_consistent_error_body(human_principal):
    """Both paths render errors with the same body shape: detail + reason."""
    bearer = "same-token"
    resolver = TokenResolver([MappingVerifier(bearer, human_principal)])

    fastapi_app = FastAPI()
    fastapi_app.state.auth_resolver = resolver
    from auth.api import auth_error_handler

    fastapi_app.add_exception_handler(AuthError, auth_error_handler)

    middleware_errors = []

    @fastapi_app.get("/whoami")
    async def whoami(principal: Principal = Depends(get_principal)):
        return {"subject": principal.subject}

    async def middleware_app(scope, receive, send):
        if scope["type"] != "http":
            await send({"type": "http.response.start", "status": 500, "headers": []})
            return
        middleware_errors.append(scope.get("state", {}).get("principal"))
        response = httpx.Response(200, json={"ok": True})
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    from auth.middleware import PrincipalMiddleware

    mounted = PrincipalMiddleware(middleware_app, resolver)

    # Test FastAPI 401 response
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://test",
    ) as client:
        response = await client.get("/whoami", headers={"Authorization": "Bearer bad"})

    assert response.status_code == 401
    fastapi_body = response.json()
    assert "detail" in fastapi_body
    assert "reason" in fastapi_body

    # Test middleware 401 response
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mounted),
        base_url="http://test",
    ) as client:
        response = await client.get("/", headers={"Authorization": "Bearer bad"})

    assert response.status_code == 401
    middleware_body = response.json()
    assert "detail" in middleware_body
    assert "reason" in middleware_body
    assert fastapi_body["reason"] == middleware_body["reason"]
