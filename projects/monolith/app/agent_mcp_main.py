"""Entrypoint for the authenticated Ember agent MCP surface."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from auth.api import (
    Authority,
    PrincipalMiddleware,
    current_principal,
    get_default_resolver,
)
from knowledge.mcp import dispute_fact, report_knowledge, search_knowledge

agent_mcp = FastMCP("Agent Catalogue")
agent_mcp.add_tool(search_knowledge)
agent_mcp.add_tool(report_knowledge)
agent_mcp.add_tool(dispute_fact)


class _AuthenticatedPrincipalGate:
    """Reject anonymous callers after identity resolution and before MCP."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and current_principal().authority is Authority.ANONYMOUS
        ):
            response = JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _McpRootAdapter:
    """Serve the root-path FastMCP app at the external /mcp endpoint."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        adapted_scope = dict(scope)
        adapted_scope["path"] = "/"
        adapted_scope["raw_path"] = b"/"
        await self.app(adapted_scope, receive, send)


async def _healthz(request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_agent_mcp_app(resolver=None) -> Starlette:
    """Build the health endpoint and authenticated, stateless MCP mount."""

    raw_mcp_http_app = agent_mcp.http_app(
        transport="http",
        path="/",
        stateless_http=True,
    )
    authenticated_mcp_app = PrincipalMiddleware(
        _AuthenticatedPrincipalGate(raw_mcp_http_app),
        resolver or get_default_resolver(),
    )

    @asynccontextmanager
    async def lifespan(application: Starlette):
        async with raw_mcp_http_app.lifespan(application):
            yield

    return Starlette(
        routes=[
            Route("/healthz", endpoint=_healthz, methods=["GET"]),
            Route(
                "/mcp",
                endpoint=_McpRootAdapter(authenticated_mcp_app),
                methods=["GET", "POST", "DELETE"],
            ),
            Mount("/mcp", app=authenticated_mcp_app),
        ],
        lifespan=lifespan,
    )


app = build_agent_mcp_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8092, log_level="warning")
