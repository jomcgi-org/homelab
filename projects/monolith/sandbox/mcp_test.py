"""Smoke test that the run_code MCP tool is registered."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.asyncio
async def test_run_code_tool_registered():
    importlib.import_module("sandbox.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    assert "run_code" in registered, (
        f"run_code not registered; got: {sorted(registered)}"
    )
    assert "run_python" not in registered

    tool = next(t for t in tools if t.name == "run_code")
    properties = tool.parameters["properties"]
    assert set(properties) == {"code", "language", "files"}
    assert properties["language"]["default"] == "python"
