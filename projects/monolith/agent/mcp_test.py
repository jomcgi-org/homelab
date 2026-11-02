"""Smoke test that all monolith-agent-* MCP tools are registered."""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

EXPECTED_TOOLS = {
    "monolith_agent_completed_raw_recovery",
    "monolith_agent_acquire_lock",
    "monolith_agent_extend_lock",
    "monolith_agent_release_lock",
    "monolith_agent_list_locks",
    "monolith_agent_notify",
    "monolith_agent_check_stuck_jobs",
    "monolith_agent_check_dead_letters",
    "monolith_agent_trigger_job",
    "monolith_agent_list_routine_jobs",
    "monolith_agent_claim_routine_job",
    "monolith_agent_complete_routine_job",
    "monolith_agent_register_routine_job",
    "monolith_agent_deregister_routine_job",
    "monolith_agent_trigger_routine_job",
    "monolith_agent_fact_review_recovery",
    "monolith_agent_set_factory_goals",
    "monolith_agent_schema_retirement_inventory",
}


@pytest.mark.asyncio
async def test_all_agent_tools_registered():
    importlib.import_module("agent.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing agent tools: {missing}"
    assert "monolith_agent_check_orphan_jobs" not in registered


def test_expected_tool_count_is_eighteen():
    """Guard against silently dropping a tool from EXPECTED_TOOLS."""
    assert len(EXPECTED_TOOLS) == 18


def test_admission_api_import_does_not_require_private_operator_module(monkeypatch):
    import builtins
    import factory.api

    original = builtins.__import__

    def without_private_access(name, *args, **kwargs):
        if name == "factory.access":
            raise ModuleNotFoundError(
                "private operator module absent from admission library"
            )
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_private_access)
    importlib.reload(factory.api)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority,kind,groups,allowed",
    [
        ("standing", "human", ("operators",), True),
        ("standing", "human", (), False),
        ("standing", "workload", ("operators",), False),
        ("delegated", "human", ("operators",), False),
    ],
)
async def test_schema_inventory_requires_standing_human_operator(
    monkeypatch, authority, kind, groups, allowed
):
    import threading
    from auth.api import Authority, Principal, PrincipalKind
    from auth.dependencies import reset_current_principal, set_current_principal
    from agent import mcp, schema_retirement

    caller, calls = threading.get_ident(), []

    def read():
        assert threading.get_ident() != caller
        calls.append(True)
        return {"complete": True}

    monkeypatch.setattr(schema_retirement, "read_inventory", read)
    token = set_current_principal(
        Principal(
            subject="operator",
            actor=(),
            scope=(),
            groups=groups,
            email=None,
            kind=PrincipalKind(kind),
            authority=Authority(authority),
        )
    )
    try:
        result = await mcp.monolith_agent_schema_retirement_inventory()
    finally:
        reset_current_principal(token)
    assert result["complete"] is allowed
    assert bool(calls) is allowed


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
                "last_status": "x" * 100000,
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
    assert len(result["jobs"][0]["last_status"]) == 256
    assert result["jobs"][0]["truncated_fields"] == ["last_status"]
    monkeypatch.setattr(mcp.routine_jobs, "list_jobs", lambda **kwargs: [])
    assert (await mcp.monolith_agent_list_routine_jobs())["next_offset"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority,kind,groups,allowed",
    [
        ("standing", "human", ("operators",), True),
        ("standing", "human", (), False),
        ("standing", "workload", ("operators",), False),
        ("delegated", "human", ("operators",), False),
    ],
)
async def test_explicit_review_recovery_requires_existing_operator_gate(
    monkeypatch, authority, kind, groups, allowed
):
    import threading
    from auth.api import Authority, Principal, PrincipalKind
    from auth.dependencies import reset_current_principal, set_current_principal
    from agent import mcp, routine_reconciliation

    caller, calls = threading.get_ident(), []

    def recover(**kwargs):
        assert threading.get_ident() != caller
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(routine_reconciliation, "recover_review_job", recover)
    token = set_current_principal(
        Principal(
            subject="operator",
            actor=(),
            scope=(),
            groups=groups,
            email=None,
            kind=PrincipalKind(kind),
            authority=Authority(authority),
        )
    )
    recovery = {"mode": "preview", "session_id": 20412, "permit_id": 17827}
    try:
        result = await mcp.monolith_agent_trigger_routine_job(
            "kg-fact-review", recovery
        )
        assert result["ok"] is allowed
        if allowed:
            assert calls == [{"actor": "operator", "recovery": recovery}]
            with pytest.raises(ValueError, match="limited"):
                await mcp.monolith_agent_trigger_routine_job("other", recovery)
        else:
            assert calls == []
    finally:
        reset_current_principal(token)


@pytest.mark.asyncio
async def test_recovery_tool_publishes_typed_schema():
    from agent import mcp as agent_mcp

    tools = {tool.name: tool for tool in await agent_mcp.mcp.list_tools()}
    schema = tools["monolith_agent_fact_review_recovery"].parameters
    assert set(schema["required"]) == {"operation", "session_id", "permit_id"}
    assert set(schema["properties"]) == {
        "operation",
        "session_id",
        "permit_id",
        "expected_state_sha256",
        "reconciliation_key",
        "acknowledgment",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["operation"]["enum"] == ["preview", "apply"]
    for name in ("session_id", "permit_id"):
        assert schema["properties"][name] == {"type": "integer", "exclusiveMinimum": 0}
    assert {
        "const": "accept_duplicate_compute_and_writes",
        "type": "string",
    } in schema["properties"]["acknowledgment"]["anyOf"]
    assert {
        "pattern": "^[0-9a-f]{64}$",
        "type": "string",
    } in schema["properties"]["expected_state_sha256"]["anyOf"]


def _operator(authorized=True):
    from auth.api import Authority, Principal, PrincipalKind

    return Principal(
        subject="operator",
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind("human"),
        authority=Authority("standing" if authorized else "delegated"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["preview", "apply"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_registered_recovery_tool_cannot_fall_back_to_trigger(
    monkeypatch, mode, authorized
):
    from auth.dependencies import reset_current_principal, set_current_principal
    from agent import mcp, routine_reconciliation

    calls = []

    def recover(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    def forbidden_trigger(*args, **kwargs):
        pytest.fail("Dedicated recovery must never call the legacy trigger")

    monkeypatch.setattr(routine_reconciliation, "recover_review_job", recover)
    monkeypatch.setattr(mcp.routine_jobs, "trigger_job", forbidden_trigger)
    token = set_current_principal(_operator(authorized))
    fields = {"session_id": 20412, "permit_id": 17827}
    if mode == "apply":
        fields.update(
            expected_state_sha256="a" * 64,
            reconciliation_key="review-once",
            acknowledgment="accept_duplicate_compute_and_writes",
        )
    args = {"operation": mode, **fields}
    try:
        tool = await mcp.mcp.get_tool("monolith_agent_fact_review_recovery")
        await tool.run(args)
        assert calls == (
            [{"actor": "operator", "recovery": {"mode": mode, **fields}}]
            if authorized
            else []
        )
        calls.clear()
        invalid = [
            {**args, "mode": "apply"},
            {**args, "operation": "trigger"},
            {**args, "name": "kg-fact-review"},
            {**args, "session_id": True},
            {**args, "permit_id": 0},
        ]
        if mode == "apply":
            invalid.extend(
                [
                    {**args, "acknowledgment": "yes"},
                    {**args, "expected_state_sha256": "bad"},
                ]
            )
        for bad in invalid:
            with pytest.raises(ValidationError):
                await tool.run(bad)
        assert calls == []
    finally:
        reset_current_principal(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", ["expected_state_sha256", "reconciliation_key", "acknowledgment"]
)
async def test_fact_review_apply_without_preview_fields_is_refused(missing):
    from auth.dependencies import reset_current_principal, set_current_principal
    from agent import mcp

    args = {
        "operation": "apply",
        "session_id": 20412,
        "permit_id": 17827,
        "expected_state_sha256": "a" * 64,
        "reconciliation_key": "review-once",
        "acknowledgment": "accept_duplicate_compute_and_writes",
    }
    del args[missing]
    token = set_current_principal(_operator())
    try:
        tool = await mcp.mcp.get_tool("monolith_agent_fact_review_recovery")
        with pytest.raises(Exception, match="Invalid|SHA256|acknowledgment"):
            await tool.run(args)
    finally:
        reset_current_principal(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["preview", "apply"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_completed_raw_recovery_requires_standing_operator(
    monkeypatch, mode, authorized
):
    from auth.dependencies import reset_current_principal, set_current_principal
    from agent import mcp, routine_reconciliation

    calls = []
    monkeypatch.setattr(
        routine_reconciliation,
        "recover_completed_raw",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )
    token = set_current_principal(_operator(authorized))
    fields = {"job_name": "kg:raw", "session_id": 1, "permit_id": 2}
    if mode == "apply":
        fields.update(expected_state_sha256="a" * 64, reconciliation_key="one")
    args = {"operation": mode, **fields}
    try:
        tool = await mcp.mcp.get_tool("monolith_agent_completed_raw_recovery")
        assert set(tool.parameters["required"]) == {
            "operation",
            "job_name",
            "session_id",
            "permit_id",
        }
        await tool.run(args)
        assert calls == (
            [{"actor": "operator", "recovery": {"mode": mode, **fields}}]
            if authorized
            else []
        )
        with pytest.raises(ValidationError):
            await tool.run({**args, "mode": "apply"})
    finally:
        reset_current_principal(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["expected_state_sha256", "reconciliation_key"])
async def test_completed_raw_apply_without_preview_fields_is_refused(missing):
    from auth.dependencies import reset_current_principal, set_current_principal
    from agent import mcp

    args = {
        "operation": "apply",
        "job_name": "kg:raw",
        "session_id": 1,
        "permit_id": 2,
        "expected_state_sha256": "a" * 64,
        "reconciliation_key": "one",
    }
    del args[missing]
    token = set_current_principal(_operator())
    try:
        tool = await mcp.mcp.get_tool("monolith_agent_completed_raw_recovery")
        with pytest.raises(Exception, match="Invalid|SHA256"):
            await tool.run(args)
    finally:
        reset_current_principal(token)


@pytest.mark.asyncio
async def test_daily_learning_metadata_omits_original_prompt(monkeypatch):
    from agent import mcp as module

    monkeypatch.setattr(
        module.routine_jobs,
        "list_jobs",
        lambda **kwargs: [
            {
                "name": "factory-retro-daily",
                "payload": {
                    "model": "luna",
                    "learning_stage": "evidence",
                    "prompt": "PRIVATE",
                    "learning_activation": {
                        "previous_fields": {"prompt": "OLD PRIVATE"}
                    },
                },
            }
        ],
    )

    data = await module.monolith_agent_list_routine_jobs()
    import json

    metadata = data["jobs"][0]["learning_configuration"]
    assert len(metadata.pop("payload_sha256")) == 64
    assert metadata == {
        "model": "luna",
        "stage": "evidence",
        "reconciled": True,
    }
    assert "PRIVATE" not in json.dumps(data)
