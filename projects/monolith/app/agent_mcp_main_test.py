"""Authentication and catalogue tests for the Ember agent MCP surface."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent_mcp_main import agent_mcp, build_agent_mcp_app
from auth.api import Authority, Principal, PrincipalKind
from auth.verifier import TokenResolver

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_EXPECTED_TOOLS = {
    "search_knowledge",
    "report_knowledge",
    "dispute_fact",
}


class _MappingVerifier:
    def __init__(self, token: str, principal: Principal) -> None:
        self.token = token
        self.principal = principal

    async def verify(self, token: str) -> Principal | None:
        if token == self.token:
            return self.principal
        return None


def _mcp_response_json(response: httpx.Response) -> dict:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = next(
            line.removeprefix("data:").strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        )
        return json.loads(data)
    return response.json()


async def _post(app, *, authorization: str | None = None) -> httpx.Response:
    headers = dict(_MCP_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )


@pytest.mark.asyncio
async def test_anonymous_request_is_rejected_before_mcp_dispatch():
    response = await _post(build_agent_mcp_app(TokenResolver([])))

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_health_check_remains_available_to_kubelet_without_a_token():
    app = build_agent_mcp_app(TokenResolver([]))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_unresolvable_bearer_token_is_rejected():
    response = await _post(
        build_agent_mcp_app(TokenResolver([])),
        authorization="Bearer not-owned-by-any-verifier",
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "authentication failed: unrecognized",
        "reason": "unrecognized",
    }


@pytest.mark.asyncio
async def test_valid_authentik_principal_lists_only_agent_catalogue_tools():
    principal = Principal(
        subject="ember-agent",
        actor=(),
        scope=("tools:read",),
        groups=("agents",),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.STANDING,
    )
    response = await _post(
        build_agent_mcp_app(
            TokenResolver([_MappingVerifier("valid-authentik-token", principal)])
        ),
        authorization="Bearer valid-authentik-token",
    )

    assert response.status_code == 200
    tools = _mcp_response_json(response)["result"]["tools"]
    assert {tool["name"] for tool in tools} == _EXPECTED_TOOLS
    assert len(tools) == len(_EXPECTED_TOOLS)


@pytest.mark.asyncio
async def test_agent_catalogue_registers_no_other_mcp_tools():
    tools = await agent_mcp.list_tools()

    assert {tool.name for tool in tools} == _EXPECTED_TOOLS
    assert len(tools) == len(_EXPECTED_TOOLS)
