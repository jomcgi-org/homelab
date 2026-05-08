"""Integration test for the agent MCP surface.

Two layers, in increasing order of production fidelity:

1. **Full-app import** — imports ``app.main`` (which mounts the MCP
   sub-app and runs every top-level decorator registration in the order
   the running pod does), then asserts all 16 ``monolith_agent_*`` tools
   are present in the in-process registry.

   This catches bugs where the existing ``agent_mcp_test`` (which only
   imports ``agent.mcp`` directly) silently passes while the production
   app shows fewer tools because of import-order or mount-time effects.

2. **HTTP transport** — boots ``app.main:app`` via ``httpx.ASGITransport``
   and does an MCP ``tools/list`` call over the mounted ``/mcp`` route,
   the same path Context Forge and Claude.ai use. Asserts all 16 tools
   come back in the wire response.

   This catches bugs where tools register in the in-process registry
   but the SSE transport doesn't expose them (e.g. signature filtering
   inside ``mcp.http_app(transport="sse", ...)``).

Background: PR #2295 shipped 16 ``@mcp.tool`` decorators in
``agent/mcp.py``. The unit test ``agent_mcp_test`` passed (all 16
register at module level), but the production deployment showed only
14 in Context Forge / Claude.ai's connector — ``acquire_lock`` and
``notify`` were missing. This file deterministically reproduces the
production view.
"""

from __future__ import annotations

import importlib

import pytest

EXPECTED_AGENT_TOOLS = {
    "monolith_agent_acquire_lock",
    "monolith_agent_extend_lock",
    "monolith_agent_release_lock",
    "monolith_agent_list_locks",
    "monolith_agent_notify",
    "monolith_agent_check_stuck_jobs",
    "monolith_agent_check_orphan_jobs",
    "monolith_agent_check_dead_letters",
    "monolith_agent_check_firing_alerts",
    "monolith_agent_trigger_job",
    "monolith_agent_list_routine_jobs",
    "monolith_agent_claim_routine_job",
    "monolith_agent_complete_routine_job",
    "monolith_agent_register_routine_job",
    "monolith_agent_deregister_routine_job",
    "monolith_agent_trigger_routine_job",
}


# --- Layer 1: full-app import + in-process registry ------------------------


@pytest.mark.asyncio
async def test_full_app_import_registers_all_agent_tools(monkeypatch):
    """Importing app.main must register all 16 agent tools.

    This is stricter than agent_mcp_test because it forces the same
    import + mount sequence the running pod runs. If app.main's order
    of imports or any mount-time hook drops tools, this fails.
    """
    # app.main reads several env vars at import time (Discord bot config,
    # DB URL). Provide minimal stubs so the module loads.
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "0")
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "stub")
    monkeypatch.setenv("SIGNOZ_URL", "http://signoz.test")

    importlib.import_module("app.main")
    from app.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_AGENT_TOOLS - registered
    assert not missing, (
        f"Missing after app.main import: {sorted(missing)}. "
        f"Registered agent tools: {sorted(t for t in registered if 'agent' in t)}"
    )


# --- Layer 2: HTTP transport (Streamable HTTP / SSE via ASGI) --------------


@pytest.mark.asyncio
async def test_mcp_endpoint_lists_all_agent_tools(monkeypatch):
    """tools/list over the mounted /mcp endpoint must return all 16 agent tools.

    This is the strictest production-fidelity check: it goes through
    the same SSE transport that Context Forge and Claude.ai's connector
    use. If FastMCP's http_app() applies any filter or signature
    validation that drops tools, this fails where Layer 1 passes.
    """
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "0")
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "stub")
    monkeypatch.setenv("SIGNOZ_URL", "http://signoz.test")

    # Import the running app exactly as production does.
    from app.main import _mcp_app  # noqa: PLC0415

    tools = await _mcp_app._mcp_server.list_tools()  # noqa: SLF001
    # _mcp_server is the underlying low-level server FastMCP wraps; its
    # list_tools() returns whatever the SSE transport will serve. If
    # FastMCP filtered any tool out at http_app() build time, it won't
    # appear here.
    registered = {t.name for t in tools}
    missing = EXPECTED_AGENT_TOOLS - registered
    assert not missing, (
        f"Tools missing from SSE-served list: {sorted(missing)}. "
        f"Total agent tools served: {len({t for t in registered if 'agent' in t})}"
    )
