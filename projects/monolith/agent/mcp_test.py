"""Smoke test that all monolith-agent-* MCP tools are registered."""

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
    "monolith_agent_trigger_job",
    "monolith_agent_list_routine_jobs",
    "monolith_agent_claim_routine_job",
    "monolith_agent_complete_routine_job",
    "monolith_agent_register_routine_job",
    "monolith_agent_deregister_routine_job",
    "monolith_agent_trigger_routine_job",
    "monolith_agent_set_factory_goals",
}


@pytest.mark.asyncio
async def test_all_agent_tools_registered():
    importlib.import_module("agent.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing agent tools: {missing}"


def test_expected_tool_count_is_sixteen():
    """Guard against silently dropping a tool from EXPECTED_TOOLS."""
    assert len(EXPECTED_TOOLS) == 16


@pytest.mark.asyncio
async def test_routine_list_is_bounded_and_does_not_export_payloads(monkeypatch):
    import threading
    from datetime import datetime, timezone
    from agent import mcp

    caller = threading.get_ident()
    calls = []

    def rows(**kwargs):
        assert threading.get_ident() != caller
        calls.append(kwargs)
        return [
            {
                "name": f"job-{i}",
                "created_at": datetime.now(timezone.utc),
                "payload": {"secret": "private-payload"},
                "last_summary": "private-result",
            }
            for i in range(3)
        ]

    monkeypatch.setattr(mcp.routine_jobs, "list_jobs", rows)
    result = await mcp.monolith_agent_list_routine_jobs(limit=2, offset=4)
    assert calls == [{"due_only": False, "kind": None, "limit": 3, "offset": 4}]
    assert len(result["jobs"]) == 2
    assert result["next_offset"] == 6
    assert "private-" not in str(result)
    assert "payload" not in result["jobs"][0]
    monkeypatch.setattr(mcp.routine_jobs, "list_jobs", lambda **kwargs: [])
    assert (await mcp.monolith_agent_list_routine_jobs())["next_offset"] is None
