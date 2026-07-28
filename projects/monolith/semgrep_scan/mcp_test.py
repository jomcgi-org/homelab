"""Smoke test that the semgrep_scan MCP tool is registered."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.asyncio
async def test_semgrep_scan_tool_registered():
    importlib.import_module("semgrep_scan.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    assert "semgrep_scan" in registered, (
        f"semgrep_scan not registered; got: {sorted(registered)}"
    )
