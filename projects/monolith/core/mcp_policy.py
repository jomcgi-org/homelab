"""Group authorization for the shared monolith MCP surface.

Every tool on this instance requires the ``operators`` group unless it carries
the public tag. The default is deny on purpose: before this, ``/mcp`` resolved
a principal and then ignored it, so anything that could reach the ClusterIP
could call the cluster tools or destroy an agent session with no credential at
all. Only the gateway's own ``MCP_REQUIRE_AUTH`` kept that off the internet,
and an authorization boundary that lives in one process nobody in this repo
controls is not a boundary.

Listing is deliberately not gated the same way, and the asymmetry is
load-bearing rather than an oversight. Context Forge refreshes its cached tool
catalogue by calling this surface ANONYMOUSLY, roughly once a minute. Filter
the list for an anonymous caller and the gateway learns that the monolith
serves nothing, caches that, and serves the empty result to every real caller
until someone notices. So an anonymous caller may see the catalogue and may
call nothing in it; an identified caller sees exactly what it may call.

The tag lives at the tool definition site rather than in a list here, because a
per-tool list in a config file is how tools go silently missing, and because a
tool and its reachability should be reviewable in one diff.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from typing import TYPE_CHECKING

import mcp.types as mt
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools.base import Tool, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from auth.api import Principal

# auth/ is imported function-locally throughout this module. The PUBLIC tier
# globs core/**/*.py into its own source set but deliberately does not ship
# auth/, so a module-scope import here would break main_public_imports_test the
# moment anything in that closure reached this file. framework/core.py takes
# the same precaution for the same reason.

logger = logging.getLogger("monolith.mcp.policy")

OPERATOR_GROUP = "operators"

# A tool tagged this way is callable by anyone who reaches the surface,
# anonymous included. Reserved for paths that have a recorded decision to work
# without an identified caller: today that is the ADR 058 voice companion,
# whose tools record principal facts but never gate on them.
PUBLIC_TAG = "mcp:public"

# Escape hatch. This gate sits in front of the tool surface an operator would
# use to diagnose it, so a mistake here costs the means of fixing it. Setting
# this to a false value restores the previous behaviour without a revert and
# redeploy of the image.
_ENFORCED_ENV = "MCP_GROUP_POLICY_ENFORCED"


def enforced() -> bool:
    """Whether the gate denies. Read per call so a rolled pod picks it up."""
    return os.environ.get(_ENFORCED_ENV, "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def permitted(tool: Tool | None, principal: Principal) -> bool:
    """Whether ``principal`` may call ``tool``.

    An unresolvable tool is denied rather than allowed: the caller named
    something this server could not classify, and guessing is the wrong side to
    err on for a surface that carries cluster reads and session destruction.
    """
    if tool is None:
        return False
    if PUBLIC_TAG in tool.tags:
        return True
    return principal.has_group(OPERATOR_GROUP)


class GroupPolicyMiddleware(Middleware):
    """Filter the tool list per caller, and deny unauthorized calls."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        from auth.api import Authority, current_principal  # noqa: PLC0415

        tools = await call_next(context)
        principal = current_principal()
        # The gateway's catalogue refresh arrives here anonymous and unsigned.
        # Hand it everything: it is building the menu, not ordering from it,
        # and on_call_tool is what actually decides.
        if principal.authority is Authority.ANONYMOUS:
            return tools
        if not enforced():
            return tools
        return [tool for tool in tools if permitted(tool, principal)]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        from auth.api import current_principal  # noqa: PLC0415

        name = context.message.name
        principal = current_principal()
        server_context = context.fastmcp_context
        tool = None
        if server_context is not None:
            tool = await server_context.fastmcp.get_tool(name)
        elif enforced():
            # No server context means no tags to read, so the call cannot be
            # classified. Deny rather than assume the permissive branch.
            logger.warning("mcp policy: no server context for tool %s, denying", name)
            raise AuthorizationError(f"{name} is not available to this caller")

        if not enforced():
            return await call_next(context)

        if not permitted(tool, principal):
            # Logged at warning with the subject and groups presented, because
            # the two ways this fires (a genuinely unauthorized caller, and a
            # token that lost its groups claim) look identical from outside.
            logger.warning(
                "mcp policy: denied tool=%s subject=%s authority=%s groups=%s",
                name,
                principal.subject,
                principal.authority,
                ",".join(principal.groups),
            )
            raise AuthorizationError(f"{name} is not available to this caller")
        return await call_next(context)
