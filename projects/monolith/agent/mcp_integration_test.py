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
    use. We boot ``app.main:app`` via ``httpx.ASGITransport`` and do
    the real MCP handshake (SSE GET → POST ``initialize`` → POST
    ``tools/list`` on the per-session messages endpoint). If the SSE
    handler diverges from ``mcp.list_tools()`` for any tool, this
    fails where Layer 1 passes.
    """
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415
    import logging  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    logger = logging.getLogger(__name__)

    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "0")
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "stub")
    monkeypatch.setenv("SIGNOZ_URL", "http://signoz.test")

    from app.main import app  # noqa: PLC0415

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        timeout=httpx.Timeout(30.0),
    ) as client:
        # Open the SSE stream. The first event carries the per-session
        # POST endpoint URL — capture it, then drive the protocol.
        endpoint_event = asyncio.Future()
        tool_list_event = asyncio.Future()

        async def consume_sse():
            try:
                async with client.stream(
                    "GET", "/mcp/", headers={"Accept": "text/event-stream"}
                ) as resp:
                    assert resp.status_code == 200, (
                        f"SSE GET failed: {resp.status_code}"
                    )
                    current_event = None
                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            current_event = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            data = line.split(":", 1)[1].strip()
                            if (
                                current_event == "endpoint"
                                and not endpoint_event.done()
                            ):
                                endpoint_event.set_result(data)
                            else:
                                # JSON-RPC message
                                try:
                                    obj = json.loads(data)
                                    if (
                                        isinstance(obj, dict)
                                        and obj.get("id") == 2
                                        and not tool_list_event.done()
                                    ):
                                        tool_list_event.set_result(obj)
                                        return
                                except json.JSONDecodeError:
                                    pass
            except Exception as exc:
                logger.exception("SSE consumer failed")
                if not endpoint_event.done():
                    endpoint_event.set_exception(exc)
                if not tool_list_event.done():
                    tool_list_event.set_exception(exc)

        sse_task = asyncio.create_task(consume_sse())
        try:
            endpoint_url = await asyncio.wait_for(endpoint_event, timeout=10.0)

            # Send initialize.
            init_resp = await client.post(
                endpoint_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "integration-test", "version": "1"},
                    },
                },
            )
            assert init_resp.status_code in (200, 202), (
                f"initialize failed: {init_resp.status_code} {init_resp.text}"
            )

            # Send the initialized notification.
            await client.post(
                endpoint_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

            # Request tools/list.
            list_resp = await client.post(
                endpoint_url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert list_resp.status_code in (200, 202), (
                f"tools/list failed: {list_resp.status_code}"
            )

            # The response comes back through the SSE stream.
            obj = await asyncio.wait_for(tool_list_event, timeout=10.0)
        finally:
            sse_task.cancel()

    assert "result" in obj, f"tools/list response missing result: {obj}"
    served = {t["name"] for t in obj["result"]["tools"]}
    missing = EXPECTED_AGENT_TOOLS - served
    assert not missing, (
        f"Tools missing from SSE wire response: {sorted(missing)}. "
        f"Total agent tools served on wire: {len({t for t in served if 'agent' in t})}"
    )
