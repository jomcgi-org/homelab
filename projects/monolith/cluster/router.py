"""HTTP routes for the cluster domain (``/api/cluster/...``).

One read-only route: the ``verify_deployment`` rollout verdict over REST, so
callers without an MCP client (the ``homelab pr land`` CLI) get the same answer
the MCP tool gives. The body is the tool's dict unchanged, including its
``error`` key, so both surfaces stay one implementation.
"""

from __future__ import annotations

from fastapi import APIRouter

from cluster.mcp import verify_deployment

router = APIRouter(prefix="/api/cluster", tags=["cluster"])


@router.get(
    "/applications/{app}/verdict",
    summary="Rollout verdict for one ArgoCD Application",
)
async def application_verdict(app: str, expected_revision: str | None = None) -> dict:
    return await verify_deployment(app, expected_revision=expected_revision)
