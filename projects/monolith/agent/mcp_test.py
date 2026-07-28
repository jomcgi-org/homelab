"""Smoke test that all 18 monolith-agent-* MCP tools are registered."""

from __future__ import annotations

import importlib

import pytest

EXPECTED_TOOLS = {
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
    "monolith_agent_list_agent_threads",
    "monolith_agent_get_agent_thread",
}


@pytest.mark.asyncio
async def test_all_agent_tools_registered():
    importlib.import_module("agent.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing agent tools: {missing}"


def test_expected_tool_count_is_eighteen():
    """Guard against silently dropping a tool from EXPECTED_TOOLS."""
    assert len(EXPECTED_TOOLS) == 18
