"""Asserts every registered MCP tool description survives Context Forge sanitization.

Context Forge (the in-cluster MCP gateway at
``context-forge-gateway-mcp-stack-mcpgateway`` in namespace ``mcp``)
runs a security validator on every tool description it discovers.
Tools whose description contains any of the patterns below are
silently dropped from the proxied surface, with only an ERROR log
line in the gateway pod. The result is partial-discovery (e.g. 14 of
16 tools visible to Claude.ai) that is invisible from monolith's side.

This test asserts at CI time that no future MCP tool addition can
sneak in a forbidden pattern and ship to production with silent
discovery loss.

Source of the rules (verified 2026-05-08 against the deployed gateway
image): ``/app/mcpgateway/schemas.py`` ``ToolCreate.validate_description``
plus ``MAX_DESCRIPTION_LENGTH`` from ``/app/mcpgateway/common/validators.py``.
If Context Forge is upgraded and the rules change, update the
``FORBIDDEN_PATTERNS`` and ``MAX_DESCRIPTION_LENGTH`` constants below.
"""

from __future__ import annotations

import pytest

# From mcpgateway/schemas.py:436 — copied verbatim. Backticks are
# explicitly allowed (commonly used for Markdown inline code).
FORBIDDEN_PATTERNS = ["&&", ";", "||", "$(", "|", "> ", "< "]

# From mcpgateway/common/validators.py — gateway default is 8192 (8KB).
# Real cap could differ if the gateway is configured with a smaller
# value, but we assert against the default; a smaller cap would
# similarly catch over-long descriptions in CI.
MAX_DESCRIPTION_LENGTH = 8192


def _register_all_tools() -> int:
    """Register every composed module's MCP tools; return how many modules did.

    Derived from the FastMonolith module registry (the single source of truth
    for tool registration since app/main.py became a build_app call) instead
    of hand-listed here, so a newly added tool module is covered
    automatically. A hand-maintained list previously omitted ``cluster.mcp``
    and shipped the k8s-* tools unvalidated (the gateway then silently dropped
    four of them); reading the real registration source closes that gap.
    """
    from app.modules_private import ALL_MODULES

    registered = 0
    for module in ALL_MODULES:
        if module.register_mcp is not None:
            module.register_mcp()
            registered += 1
    return registered


@pytest.mark.asyncio
async def test_all_mcp_tool_descriptions_pass_context_forge_validation():
    """Every registered tool's description must be Context Forge-safe."""
    registered = _register_all_tools()
    assert registered, (
        "no module in app.modules_private.ALL_MODULES declares register_mcp; "
        "the test cannot see the tool set"
    )
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    violations: list[str] = []
    for tool in tools:
        desc = tool.description or ""
        for pat in FORBIDDEN_PATTERNS:
            if pat in desc:
                violations.append(f"  {tool.name}: contains forbidden pattern {pat!r}")
        if len(desc) > MAX_DESCRIPTION_LENGTH:
            violations.append(
                f"  {tool.name}: description too long "
                f"({len(desc)} > {MAX_DESCRIPTION_LENGTH})"
            )
    assert not violations, (
        "These MCP tool descriptions will be silently dropped by Context Forge:\n"
        + "\n".join(violations)
        + "\n\nFix: rephrase the docstring to avoid the listed pattern. "
        "See feedback_context_forge_description_sanitization.md memory entry."
    )
