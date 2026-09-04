"""Tests for shared FastAPI and mounted-ASGI request authentication."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

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
async def test_middleware_logs_anonymous_principal_without_authorization(caplog):
    caplog.set_level(logging.INFO, logger="monolith.auth")
    app = PrincipalMiddleware(_successful_asgi_app, TokenResolver([]))

    await _call_middleware(app, path="/mcp/anonymous")

    record = _principal_resolution_record(caplog)
    assert record.subject == "anonymous"
    assert record.kind == PrincipalKind.HUMAN.value
    assert record.authority == Authority.ANONYMOUS.value
    assert record.groups == ""
    assert record.authorization_present is False
    assert record.path == "/mcp/anonymous"


@pytest.mark.asyncio
async def test_principal_log_message_renders_fields_for_the_pod_log(caplog):
    """The root formatter emits only %(message)s, so extra= alone reaches nobody.

    This asserts on the RENDERED message rather than the record attributes,
    because the rendered message is the whole artifact in `kubectl logs`, and
    telling "no token arrived" from "a token arrived" is the point of the task.
    """

    caplog.set_level(logging.INFO, logger="monolith.auth")
    app = PrincipalMiddleware(_successful_asgi_app, TokenResolver([]))

    await _call_middleware(app, path="/mcp/anonymous")

    rendered = _principal_resolution_record(caplog).getMessage()
    assert "authorization_present=False" in rendered
    assert f"authority={Authority.ANONYMOUS.value}" in rendered
    assert "subject=anonymous" in rendered
    assert "path=/mcp/anonymous" in rendered


@pytest.mark.asyncio
async def test_middleware_logs_and_traces_resolved_bearer_principal(
    caplog,
):
    token = "valid-observability-token"
    principal = Principal(
        subject="workload-1",
        actor=(),
        scope=("tools:read",),
        groups=("agents", "operators"),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.DELEGATED,
    )
    resolver = TokenResolver([MappingVerifier(token, principal)])
    app = PrincipalMiddleware(_successful_asgi_app, resolver)
    span = MagicMock()
    span.is_recording.return_value = True
    caplog.set_level(logging.INFO, logger="monolith.auth")

    with patch("auth.middleware.trace.get_current_span", return_value=span):
        await _call_middleware(
            app,
            headers=[(b"authorization", f"Bearer {token}".encode())],
            path="/mcp/tools",
        )

    record = _principal_resolution_record(caplog)
    assert record.subject == principal.subject
    assert record.kind == PrincipalKind.WORKLOAD.value
    assert record.authority == Authority.DELEGATED.value
    assert record.groups == "agents,operators"
    assert record.authorization_present is True
    assert record.path == "/mcp/tools"
    span.set_attributes.assert_called_once_with(
        {
            "monolith.auth.subject": principal.subject,
            "monolith.auth.kind": PrincipalKind.WORKLOAD.value,
            "monolith.auth.authority": Authority.DELEGATED.value,
            "monolith.auth.groups": "agents,operators",
            "monolith.auth.authorization_present": True,
        }
    )


@pytest.mark.asyncio
async def test_middleware_instrumentation_preserves_response_and_principal(
    human_principal,
):
    token = "instrumentation-failure-token"
    captured = {}

    async def downstream(scope, receive, send):
        captured["state"] = scope["state"]["principal"]
        captured["context"] = current_principal()
        await _successful_asgi_app(scope, receive, send)

    app = PrincipalMiddleware(
        downstream,
        TokenResolver([MappingVerifier(token, human_principal)]),
    )
    span = MagicMock()
    span.is_recording.return_value = True
    span.set_attributes.side_effect = RuntimeError("otel unavailable")

    with patch("auth.middleware.trace.get_current_span", return_value=span):
        messages = await _call_middleware(
            app,
            headers=[(b"authorization", f"Bearer {token}".encode())],
        )

    assert messages == [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b'{"ok":true}'},
    ]
    assert captured["state"] == human_principal
    assert captured["context"] == human_principal
    span.set_attributes.assert_called_once()


@pytest.mark.asyncio
async def test_middleware_principal_log_never_contains_token(caplog, human_principal):
    secret_token = "highly-secret-middleware-token-material-4942"
    app = PrincipalMiddleware(
        _successful_asgi_app,
        TokenResolver([MappingVerifier(secret_token, human_principal)]),
    )
    caplog.set_level(logging.INFO, logger="monolith.auth")

    await _call_middleware(
        app,
        headers=[(b"authorization", f"Bearer {secret_token}".encode())],
    )

    _principal_resolution_record(caplog)
    assert all(secret_token not in record.getMessage() for record in caplog.records)
    assert all(secret_token not in repr(record.__dict__) for record in caplog.records)
    assert all(
        human_principal.email not in repr(record.__dict__) for record in caplog.records
    )


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


async def _successful_asgi_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


async def _call_middleware(app, *, headers=(), path="/mcp") -> list[dict]:
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": list(headers),
        },
        receive,
        send,
    )
    return messages


def _principal_resolution_record(caplog):
    records = [
        record
        for record in caplog.records
        if record.name == "monolith.auth"
        and record.getMessage().startswith("principal resolved ")
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    return records[0]


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
