"""Shared FastMCP instance for the monolith.

Each domain module (knowledge, chat, etc.) imports ``mcp`` and registers
tools with ``@mcp.tool``.  The instance is mounted once in ``app/main.py``.
"""

from fastmcp import FastMCP

from core.mcp_policy import GroupPolicyMiddleware

# Authorization travels with the instance rather than with the mount, so a
# binary that composes this surface cannot serve it ungated by forgetting a
# wiring step in framework/core.py. See core/mcp_policy.py for why listing and
# calling are gated differently.
mcp = FastMCP("Monolith", middleware=[GroupPolicyMiddleware()])
