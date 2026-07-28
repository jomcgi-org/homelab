"""Smoke test that all six k8s-* cluster MCP tools are registered."""

from __future__ import annotations

import importlib

import pytest

EXPECTED_TOOLS = {
    "k8s_health_summary",
    "k8s_list_resources",
    "k8s_get_resource",
    "k8s_get_pod_logs",
    "k8s_get_events",
    "k8s_sync_argocd_app",
}


@pytest.mark.asyncio
async def test_all_cluster_tools_registered():
    importlib.import_module("cluster.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing cluster tools: {missing}"


def test_expected_tool_count_is_six():
    """Guard against silently dropping a tool from EXPECTED_TOOLS."""
    assert len(EXPECTED_TOOLS) == 6
