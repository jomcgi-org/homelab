"""Preflight checks for Codex-family agent sessions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic

import httpx

from factory.execution import model_family
from factory.execution import mcp

_DEFAULT_GRANT = mcp._DEFAULT_GRANT
logger = logging.getLogger(__name__)

_LOGIN_POLL_INTERVAL = 5
_LOGIN_TIMEOUT = 15 * 60
_login_watch_tasks: set[asyncio.Task] = set()


async def codex_login_gate(
    model: str | None, grant: str = _DEFAULT_GRANT
) -> dict | None:
    """Return a user-facing credential wait when a Codex grant is not ready.

    Non-Codex sessions skip the broker entirely. An unreachable broker does not
    block the turn, since the sidecar may still have a cached token.
    """
    family = model_family(model)
    grant = mcp._grant_or_raise(grant)
    if family != "codex":
        return None

    try:
        status = await mcp._broker_request("GET", f"/grants/{grant}/login/status")
    except Exception as exc:
        # If we cannot ask the broker, proceed so a cached sidecar token can
        # still serve the turn. Only a positive non-granted response blocks.
        reason = str(exc) or exc.__class__.__name__
        logger.warning("Could not check Codex login status: %s", reason)
        return None
    if status.get("state") == "granted":
        return None
    if status.get("state") == "blocked":
        return {
            "login_required": False,
            "refresh_blocked": True,
            "grant": grant,
            "message": (
                "Codex is waiting for refreshed credentials. This session will "
                "resume automatically when they arrive. If recovery is needed, "
                "start a new device login from the Codex login page."
            ),
        }

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
    if data.get("state") == "pending" and data.get("persistence_retry") is True:
        return {
            "login_required": True,
            "pending": True,
            "persistence_retry": True,
            "grant": grant,
            "message": "Codex login is approved and is being saved. Waiting sessions will resume when saving succeeds.",
        }
    return {
        "login_required": True,
        "verification_url": data.get("verification_url"),
        "user_code": data.get("user_code"),
        "grant": grant,
        "message": "Approve the Codex login in your browser within about 15 minutes.",
    }


def watch_for_login(grant: str, on_granted: Callable[[], Awaitable[None]]) -> None:
    """Watch approval and persistence, then invoke ``on_granted`` once.

    The device flow belongs to the broker, so callers that had to pause a
    session need a small bridge from the broker's eventual state change back to
    their pending work. The broker owns approval expiry; a retained login may
    need a persistence retry long after that window. A polling failure cannot
    establish whether credentials are retained, even on the very first poll.
    Only a successful response confirming no flow after the local timeout, or
    an explicit failure, ends the watch without resuming the session.
    Keep the task strongly referenced until it finishes. The pending-message
    sweep independently reconciles durable login waits after a process restart.
    """

    async def wait() -> None:
        deadline = monotonic() + _LOGIN_TIMEOUT
        while True:
            try:
                status = await mcp._broker_request(
                    "GET", f"/grants/{grant}/login/status"
                )
                state = status.get("state")
            except Exception:
                logger.warning("Could not poll Codex login status")
            else:
                if state == "granted":
                    await on_granted()
                    return
                if state == "blocked":
                    deadline = None
                elif deadline is None:
                    deadline = monotonic() + _LOGIN_TIMEOUT
                if state == "failed":
                    logger.error("Codex broker login failed")
                    return
                if state == "none" and deadline is not None and monotonic() >= deadline:
                    return
            await asyncio.sleep(_LOGIN_POLL_INTERVAL)

    task = asyncio.create_task(wait())
    _login_watch_tasks.add(task)
    task.add_done_callback(_login_watch_tasks.discard)
    task.add_done_callback(
        lambda done: done.exception() if not done.cancelled() else None
    )
