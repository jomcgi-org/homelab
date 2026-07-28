"""Smoke test that the run_python MCP tool is registered."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.asyncio
async def test_run_python_tool_registered():
    importlib.import_module("sandbox.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    assert "run_python" in registered, (
        f"run_python not registered; got: {sorted(registered)}"
    )
