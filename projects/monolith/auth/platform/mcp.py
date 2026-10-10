"""Management metadata only: invitation capabilities never enter tool results."""

import asyncio

from core.db import get_engine
from fastapi import HTTPException
from sqlmodel import Session

from auth.api import current_principal
from auth.platform import service


def _run(principal, operation, arguments):
    with Session(get_engine()) as session:
        try:
            return operation(session, principal, **arguments)
        except HTTPException as error:
            return {"error": error.detail, "status": error.status_code}


async def _command(action, request_id, reason, **arguments):
    return await asyncio.to_thread(
        _run,
        current_principal(),
        service.command,
        {"action": action, "request_id": request_id, "reason": reason, **arguments},
    )


async def _read(kind, **arguments):
    return await asyncio.to_thread(
        _run, current_principal(), service.read, {"kind": kind, **arguments}
    )


async def platform_invitation_issue(
    recipient_label: str, request_id: str, reason: str, expires_in_days: int = 7
) -> dict:
    """Prepare a possession-based invitation. Deliver its link privately to that friend."""
    return await _command(
        "issue",
        request_id,
        reason,
        recipient_label=recipient_label,
        expires_in_days=expires_in_days,
    )


async def platform_operator_bootstrap(
    request_id: str, reason: str, user_id: str | None = None
) -> dict:
    """Explicitly import this signed operator, or link this identity to an exact existing platform user.

    Linking a second browser/MCP issuer requires an explicit target. Matching
    email or username never links identities automatically.
    """
    return await _command(
        "bootstrap", request_id, reason, **({"user_id": user_id} if user_id else {})
    )


async def platform_invitation_list(limit: int = 50, after: str | None = None) -> dict:
    """List bounded invitation metadata, without usable links."""
    return await _read("invitations", limit=limit, after=after)


async def platform_invitation_revoke(
    invitation_id: str, request_id: str, reason: str
) -> dict:
    """Revoke one invitation; this does not disable an already enrolled user."""
    return await _command(
        "revoke_invitation", request_id, reason, invitation_id=invitation_id
    )


async def platform_user_list(limit: int = 50, after: str | None = None) -> dict:
    """List platform account status and application grants."""
    return await _read("users", limit=limit, after=after)


async def platform_user_get(user_id: str) -> dict:
    """Read one exact platform user."""
    return await _read("user", user_id=user_id)


async def platform_user_set_active(
    user_id: str, active: bool, request_id: str, reason: str
) -> dict:
    """Change platform status. Does not disable the Authentik identity or Moving."""
    return await _command(
        "set_active", request_id, reason, user_id=user_id, active=active
    )


async def platform_permission_list() -> list | dict:
    """List the application permission registry, excluding Authentik administration."""
    return await _read("permissions")


async def platform_user_grant(
    user_id: str, permission: str, request_id: str, reason: str
) -> dict:
    """Grant one registered application permission. Cannot grant operator authority."""
    return await _command(
        "grant", request_id, reason, user_id=user_id, permission=permission
    )


async def platform_user_revoke(
    user_id: str, permission: str, request_id: str, reason: str
) -> dict:
    """Revoke one exact application grant."""
    return await _command(
        "revoke_grant", request_id, reason, user_id=user_id, permission=permission
    )


def register_mcp_tools():
    from core.mcp_app import mcp

    if service.enabled():
        for tool in (
            platform_operator_bootstrap,
            platform_invitation_issue,
            platform_invitation_list,
            platform_invitation_revoke,
            platform_user_list,
            platform_user_get,
            platform_user_set_active,
            platform_permission_list,
            platform_user_grant,
            platform_user_revoke,
        ):
            mcp.tool(name=tool.__name__)(tool)
