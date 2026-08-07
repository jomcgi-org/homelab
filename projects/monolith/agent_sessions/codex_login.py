"""Preflight checks for Codex-family agent sessions."""

from __future__ import annotations

import httpx

from agent_sessions import model_family
from agent_sessions import mcp

_DEFAULT_GRANT = mcp._DEFAULT_GRANT


async def codex_login_gate(
    model: str | None, grant: str = _DEFAULT_GRANT
) -> dict | None:
    """Return a user-facing login response when a Codex grant is not ready.

    Non-Codex sessions skip the broker entirely. Broker failures are returned as
    login responses because a preflight check must not turn into a 500 response.
    """
    family = model_family(model)
    grant = mcp._grant_or_raise(grant)
    if family != "codex":
        return None

    try:
        status = await mcp._broker_request("GET", f"/grants/{grant}/login/status")
    except Exception as exc:
        return {
            "login_required": True,
            "error": str(exc) or exc.__class__.__name__,
            "grant": grant,
            "message": "Codex login could not be checked. Try again after checking the token broker.",
        }
    if status.get("state") == "granted":
        return None

    try:
        data = await mcp._broker_request("POST", f"/grants/{grant}/login/start")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return {
                "login_required": True,
                "pending": True,
                "grant": grant,
                "message": "Device flow already in flight. Check your browser.",
            }
        return {
            "login_required": True,
            "error": str(exc) or exc.__class__.__name__,
            "grant": grant,
            "message": "Codex login could not be checked. Try again after checking the token broker.",
        }
    except Exception as exc:
        return {
            "login_required": True,
            "error": str(exc) or exc.__class__.__name__,
            "grant": grant,
            "message": "Codex login could not be checked. Try again after checking the token broker.",
        }
    return {
        "login_required": True,
        "verification_url": data.get("verification_url"),
        "user_code": data.get("user_code"),
        "grant": grant,
        "message": "Approve the Codex login in your browser within about 15 minutes.",
    }
