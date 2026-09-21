"""The factory MCP tools: registration, the operator gate, and the trim."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind
from factory.orchestration import mcp


def _principal(
    *,
    authority: Authority = Authority.STANDING,
    kind: PrincipalKind = PrincipalKind.HUMAN,
    groups: tuple[str, ...] = ("operators",),
) -> Principal:
    return Principal(
        subject="joe",
        actor=(),
        scope=(),
        groups=groups,
        email=None,
        kind=kind,
        authority=authority,
    )


def _as(principal: Principal, coro_factory):
    token = set_current_principal(principal)
    try:
        return asyncio.run(coro_factory())
    finally:
        reset_current_principal(token)


def test_factory_tools_are_registered():
    importlib.import_module("factory.orchestration.mcp")
    from core.mcp_app import mcp as shared

    registered = {tool.name for tool in asyncio.run(shared.list_tools())}
    assert {
        "factory_status",
        "factory_escalations",
        "factory_decide",
        "factory_request_brief",
        "factory_context",
    } <= registered, f"factory tools not registered; got: {sorted(registered)}"


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
        _principal(groups=("friends",)),
    ],
)
@pytest.mark.parametrize("tool", [mcp.factory_status, mcp.factory_escalations])
def test_tools_refuse_callers_below_the_operator_floor(principal, tool, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("refused caller must not reach the database")

    monkeypatch.setattr(mcp, "_status_payload", explode)
    monkeypatch.setattr(mcp, "_escalations_payload", explode)

    result = _as(principal, tool)
    assert result["ok"] is False
    assert result["error"] == "standing operator authority is required"
    # The refusal says what was presented so a missing groups claim is
    # distinguishable from a caller who is simply not an operator.
    assert set(result["presented"]) == {"authority", "kind", "operators_group"}


def test_status_reaches_the_composer_for_an_operator(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "_status_payload",
        lambda include_recent, offset=0, limit=20, session=None: {"ok": True},
    )
    assert _as(_principal(), mcp.factory_status) == {"ok": True}


def test_escalations_reach_the_composer_for_an_operator(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "_escalations_payload",
        lambda include_resolved, offset=0, limit=20, session=None: {"ok": True},
    )
    assert _as(_principal(), mcp.factory_escalations) == {"ok": True}


def test_task_row_keeps_only_tripped_limits():
    row = mcp._task_row(
        {
            "issue_number": 7,
            "title": "fix it",
            "state": "admitted",
            "turns_used": 9,
            "allowance": {"turns": 10},
            "policy": {"task_budget_usd": 4.0},
            "unresolved_starts": 2,
            "limits": {
                "deadline_expired": False,
                "turn_limit_reached": True,
                "planner_turn_limit_reached": False,
                "budget_limit_reached": True,
            },
        }
    )
    assert row["limits_tripped"] == ["budget_limit_reached", "turn_limit_reached"]
    assert row["allowance_turns"] == 10
    assert row["task_budget_usd"] == 4.0
    assert row["unresolved_starts"] == 2
    assert row["escalation_open"] is False


def test_task_row_reports_an_unresolved_escalation_as_open():
    assert mcp._task_row({"escalation": {"question": "which?"}})["escalation_open"]
    resolved = mcp._task_row({"escalation": {"resolved": {"effect": "close"}}})
    assert resolved["escalation_open"] is False


def test_progress_counts_node_states_and_names_the_running_node():
    progress = mcp._progress(
        [
            {"node_key": "implement", "state": "running"},
            {"node_key": "plan", "state": "done"},
            {"node_key": "review", "state": "done"},
            {"node_key": "merge"},
        ]
    )
    assert progress["counts"] == {"running": 1, "done": 2, "pending": 1}
    assert progress["running"] == ["implement"]


def test_status_payload_trims_the_board_and_stamps_coverage(monkeypatch):
    board = {
        "ok": True,
        "state": "enabled",
        "generated_at": "2026-09-12T10:00:00+00:00",
        "version": 3,
        "actor": "joe",
        "admitted_count": 11,
        "policy": {"task_budget_usd": 4.0},
        "intake": {"admitted_today": 2, "max_per_day": 3},
        "lanes": {"delivery": 1},
        "review_routing": {"model": "opus"},
        "active": [{"issue_number": 1, "title": "a", "limits": {}}],
        "queued": [
            {"issue_number": 2, "title": "b", "state": "queued", "generation": 0}
        ],
        "recent": [{"issue_number": 3, "title": "c"}],
        "escalations": [
            {"issue_number": 4, "title": "d", "question": "which?", "open": True},
            {"issue_number": 5, "title": "e", "question": "gone?", "open": False},
        ],
    }
    monkeypatch.setattr(
        "factory.private_view.build_factory_view", lambda session=None: board
    )

    payload = mcp._status_payload(include_recent=False)

    assert payload["observed_at"] == "2026-09-12T10:00:00+00:00"
    assert [task["issue_number"] for task in payload["active"]] == [1]
    assert [task["issue_number"] for task in payload["queued"]] == [2]
    # Recent is opt-in, and only the open escalation is owed a decision.
    assert "recent" not in payload
    assert [item["issue_number"] for item in payload["needs_operator"]] == [4]
    # An empty active list must never read as an idle fleet.
    assert payload["coverage"]["cloud_sessions"] == "not_indexed"


def test_status_payload_includes_recent_on_request(monkeypatch):
    board = {
        "ok": True,
        "state": "enabled",
        "active": [],
        "queued": [],
        "recent": [{"issue_number": 3, "title": "c"}],
        "escalations": [],
    }
    monkeypatch.setattr(
        "factory.private_view.build_factory_view", lambda session=None: board
    )

    payload = mcp._status_payload(include_recent=True)
    assert [task["issue_number"] for task in payload["recent"]] == [3]


def test_status_payload_pages_each_bucket_and_keeps_queue_positions(monkeypatch):
    board = {
        "ok": True,
        "state": "enabled",
        "active": [{"issue_number": number} for number in range(1, 7)],
        "queued": [
            {"issue_number": number, "state": "queued", "generation": 0}
            for number in range(11, 18)
        ],
        "recent": [],
        "escalations": [],
    }
    monkeypatch.setattr(
        "factory.private_view.build_factory_view", lambda session=None: board
    )

    payload = mcp._status_payload(False, offset=2, limit=3)

    assert [row["issue_number"] for row in payload["active"]] == [3, 4, 5]
    assert [row["issue_number"] for row in payload["queued"]] == [13, 14, 15]
    assert [row["queue_position"] for row in payload["queued"]] == [3, 4, 5]
    assert payload["pagination"]["active"] == {
        "offset": 2,
        "limit": 3,
        "returned": 3,
        "total": 6,
        "next_offset": 5,
        "truncated": True,
    }
    assert payload["pagination"]["queued"]["total"] == 7
    assert payload["capabilities"]["mutations"]["priority_change"] == (
        "unavailable_no_owner"
    )


def test_status_queue_positions_exclude_stale_generations(monkeypatch):
    board = {
        "ok": True,
        "state": "enabled",
        "policy": {"generation": 2},
        "active": [],
        "queued": [
            {
                "id": 1,
                "repo": "old/repo",
                "generation": 1,
                "issue_number": 1,
                "state": "queued",
            },
            {
                "id": 2,
                "repo": "one/repo",
                "generation": 2,
                "issue_number": 2,
                "state": "queued",
            },
            {
                "id": 3,
                "repo": "two/repo",
                "generation": 2,
                "issue_number": 3,
                "state": "queued",
            },
        ],
        "recent": [],
        "escalations": [],
    }
    monkeypatch.setattr(
        "factory.private_view.build_factory_view", lambda session=None: board
    )

    payload = mcp._status_payload(False)

    assert [row["issue_number"] for row in payload["queued"]] == [2, 3]
    assert [row["queue_position"] for row in payload["queued"]] == [1, 2]
    assert payload["pagination"]["queued"]["total"] == 2


def test_status_payload_passes_through_an_uninitialised_factory(monkeypatch):
    monkeypatch.setattr(
        "factory.private_view.build_factory_view",
        lambda session=None: {
            "ok": False,
            "reason": "not_initialized",
            "state": "disabled",
        },
    )

    payload = mcp._status_payload(include_recent=False)
    assert payload["ok"] is False
    assert payload["reason"] == "not_initialized"
    assert payload["coverage"]["cloud_sessions"] == "not_indexed"


def test_escalations_payload_hides_resolved_cards_by_default(monkeypatch):
    cards = [
        {"issue_number": 9, "open": True},
        {"issue_number": 8, "open": False},
    ]
    monkeypatch.setattr(
        "factory.orchestration.factory_controls.status",
        lambda session=None: {"ok": True, "receipts": ["r"]},
    )
    monkeypatch.setattr(
        "factory.orchestration.factory_controls.escalations", lambda receipts: cards
    )

    default = mcp._escalations_payload(include_resolved=False)
    assert [card["issue_number"] for card in default["escalations"]] == [9]
    assert default["open_count"] == 1

    everything = mcp._escalations_payload(include_resolved=True)
    assert [card["issue_number"] for card in everything["escalations"]] == [9, 8]
    assert everything["open_count"] == 1

    second = mcp._escalations_payload(include_resolved=True, offset=1, limit=1)
    assert [card["issue_number"] for card in second["escalations"]] == [8]
    assert second["pagination"] == {
        "offset": 1,
        "limit": 1,
        "returned": 1,
        "total": 2,
        "next_offset": None,
        "truncated": False,
    }


def test_escalations_payload_passes_through_an_uninitialised_factory(monkeypatch):
    monkeypatch.setattr(
        "factory.orchestration.factory_controls.status",
        lambda session=None: {"ok": False, "reason": "not_initialized"},
    )

    payload = mcp._escalations_payload(include_resolved=False)
    assert payload["ok"] is False
    assert payload["escalations"] == []


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
    ],
)
@pytest.mark.parametrize(
    "name,args",
    [
        ("factory_submit_issue", {"repo": "owner/repo", "issue_number": 7}),
        (
            "factory_control",
            {"action": "stop", "request_key": "stop", "expected_version": 1},
        ),
        ("factory_task_detail", {"receipt_id": 1}),
        (
            "factory_decide",
            {
                "receipt_id": 1,
                "decision_id": "decision:" + "a" * 64,
                "option_key": "close",
                "request_key": "r",
            },
        ),
        (
            "factory_request_brief",
            {
                "receipt_id": 1,
                "decision_id": "decision:" + "a" * 64,
                "note": "why?",
                "request_key": "r",
            },
        ),
        ("factory_context", {"receipt_id": 1}),
    ],
)
def test_operation_adapters_refuse_before_calling_owners(
    principal, name, args, monkeypatch
):
    def explode(*_args, **_kwargs):
        raise AssertionError("unauthorized caller reached an operation")

    for seam in ("_submit_issue", "_request_control", "_detail_payload", "_decide"):
        monkeypatch.setattr(mcp, seam, explode)
    result = _as(principal, lambda: getattr(mcp, name)(**args))
    assert result["ok"] is False
    assert result["error"] == "standing operator authority is required"


def test_mcp_client_decisions_preserve_identity_and_verified_actor(monkeypatch):
    from fastmcp import Client
    from core.mcp_app import mcp as shared

    calls = []

    def decide(*args):
        calls.append(args)
        return {"ok": True, "state": "completed"}

    monkeypatch.setattr(mcp, "_decide", decide)
    identity = "decision:" + "a" * 64

    async def exercise():
        async with Client(shared) as client:
            args = dict(
                receipt_id=7, decision_id=identity, option_key="close", request_key="r"
            )
            assert (await client.call_tool("factory_decide", args)).data["ok"]
            for bad in (
                dict(args, receipt_id=True),
                dict(args, decision_id="stale"),
                dict(args, actor="spoof"),
            ):
                assert (
                    await client.call_tool("factory_decide", bad, raise_on_error=False)
                ).is_error
            assert (
                await client.call_tool(
                    "factory_request_brief",
                    dict(
                        receipt_id=7,
                        decision_id=identity,
                        note="Which scope?",
                        request_key="q",
                    ),
                )
            ).data["ok"]

    _as(_principal(), exercise)
    assert calls == [
        (7, identity, "close", "r", None, "joe"),
        (7, identity, "chat", "q", "Which scope?", "joe", "chat"),
    ]


def test_context_knowledge_is_scoped_and_does_not_expand_neighbours(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from factory.orchestration import conductor_context as context

    async def embed(text):
        assert text == "query"
        return [0.1]

    def search(vector, **kwargs):
        assert kwargs == {
            "limit": 2,
            "scope_filter": "repo:owner/repo",
            "exclude_invalidated": True,
        }
        return [
            {
                "note_id": "allowed",
                "scope": "repo:owner/repo",
                "snippet": "context",
                "verification_state": "disputed",
                "disputed": True,
                "edges": [{"target_id": "private-note"}],
                "provenance": [{"raw_id": "raw", "secret": "do not expand"}],
            },
            {"note_id": "private", "scope": "personal:someone-else"},
        ]

    monkeypatch.setattr(
        "shared.embedding.EmbeddingClient", lambda: SimpleNamespace(embed=embed)
    )
    monkeypatch.setattr("core.db.get_engine", lambda: object())
    monkeypatch.setattr(context, "Session", lambda _: nullcontext(object()))
    monkeypatch.setattr(
        "knowledge.api.KnowledgeStore",
        lambda _: SimpleNamespace(search_notes_with_context=search),
    )
    result = asyncio.run(context.retrieve_knowledge("query", "repo:owner/repo", 2))
    assert [note["note_id"] for note in result["notes"]] == ["allowed"]
    assert result["notes"][0]["disputed"] is True
    assert result["notes"][0]["evidence_raw_ids"] == ["raw"]
    assert "edges" not in result["notes"][0]


def test_receipt_context_has_a_stable_fresh_session_identity(monkeypatch):
    from contextlib import nullcontext
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from factory.orchestration import conductor_context as context

    row = SimpleNamespace(
        id=7,
        repo="owner/repo",
        task_id="task-7",
        updated_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )
    db = SimpleNamespace(get=lambda model, identity: row if identity == 7 else None)
    monkeypatch.setattr(context.controls, "_read_session", lambda: nullcontext(db))
    monkeypatch.setattr(
        context.controls,
        "_snapshot",
        lambda db, receipt: {
            "id": receipt.id,
            "repo": receipt.repo,
            "task_id": receipt.task_id,
            "title": "Current task",
            "direction": None,
        },
    )
    monkeypatch.setattr(context, "_request_records", lambda db: {})
    monkeypatch.setattr(context.controls, "escalation_view", lambda snapshot: None)

    result = context._factory_context(7)

    assert result["conversation"] == {
        "id": "factory-receipt:owner/repo:7",
        "kind": "receipt_context",
        "receipt_id": 7,
        "task_id": "task-7",
        "selectable_in_fresh_session": True,
    }
    assert result["coverage"]["standalone_conductor_conversations"] == (
        "unavailable_no_shared_owner"
    )


def test_knowledge_failure_does_not_undo_a_completed_decision(monkeypatch):
    from factory.orchestration import conductor_context as context

    monkeypatch.setattr(
        "factory.orchestration.factory_decisions.request_decision",
        lambda *a, **kw: {"ok": True, "state": "completed"},
    )

    def unavailable(*args):
        raise RuntimeError("KG unavailable")

    monkeypatch.setattr(context, "maintain_request_knowledge", unavailable)
    result = mcp._decide(1, "decision:abc", "close", "r", None, "joe")
    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["knowledge"]["status"] == "unavailable"


def test_control_attributes_authenticated_actor_and_preserves_owner_result(monkeypatch):
    calls = []
    acknowledgement = {
        "ok": True,
        "version": 4,
        "state": "stopped",
        "request_key": "stop",
    }

    def request(action, actor, **kwargs):
        calls.append((action, actor, kwargs))
        return acknowledgement

    monkeypatch.setattr(
        "factory.orchestration.factory_controls.request_control", request
    )
    result = _as(
        _principal(),
        lambda: mcp.factory_control(
            "stop",
            "stop",
            3,
        ),
    )
    assert result == acknowledgement
    assert calls == [
        ("stop", "joe", {"request_key": "stop", "expected_version": 3, "task_id": None})
    ]


def test_submit_preserves_durable_receipt_identity_and_duplicate_result(monkeypatch):
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.get_issue_receipt", lambda *args: None
    )
    calls = []

    def receive(body, principal):
        calls.append((body.model_dump(), principal.subject))
        return {
            "ok": True,
            "created": False,
            "receipt": {
                "id": 42,
                "repo": "owner/repo",
                "issue_number": 7,
                "generation": 2,
                "state": "queued",
                "task_id": None,
            },
        }

    monkeypatch.setattr("factory.orchestration.factory_router.factory_receipt", receive)
    result = _as(_principal(), lambda: mcp.factory_submit_issue("owner/repo", 7, 2))
    assert result["created"] is False
    assert result["receipt"]["receipt_id"] == 42
    assert result["receipt"]["generation"] == 2
    assert result["receipt"]["task_id"] is None
    assert calls == [
        ({"repo": "owner/repo", "issue_number": 7, "generation": 2}, "joe")
    ]


def test_submit_reports_ineligible_issue_without_claiming_acceptance(monkeypatch):
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.get_issue_receipt", lambda *args: None
    )
    from fastapi import HTTPException

    def refuse(body, principal):
        raise HTTPException(409, "issue is not open eligible work")

    monkeypatch.setattr("factory.orchestration.factory_router.factory_receipt", refuse)
    result = _as(_principal(), lambda: mcp.factory_submit_issue("owner/repo", 7))
    assert result == {
        "ok": False,
        "status": 409,
        "reason": "issue is not open eligible work",
    }


def test_mcp_client_lists_and_calls_controls_with_schema_validation(monkeypatch):
    from fastmcp import Client
    from core.mcp_app import mcp as shared

    calls = []

    def request(*args):
        calls.append(args)
        return {"ok": True, "state": "paused", "version": 2}

    monkeypatch.setattr(mcp, "_request_control", request)

    async def exercise():
        async with Client(shared) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert {
                "factory_control",
                "factory_submit_issue",
                "factory_task_detail",
            } <= tools.keys()
            assert "actor" not in tools["factory_control"].inputSchema["properties"]
            result = await client.call_tool(
                "factory_control",
                {
                    "action": "pause_admissions",
                    "request_key": "pause",
                    "expected_version": 1,
                },
            )
            assert result.data["ok"]
            invalid = await client.call_tool(
                "factory_control",
                {
                    "action": "configure",
                    "request_key": "bad",
                    "expected_version": 1,
                },
                raise_on_error=False,
            )
            assert invalid.is_error
            for version in (True, "1"):
                invalid = await client.call_tool(
                    "factory_control",
                    {
                        "action": "stop",
                        "request_key": "bad-version",
                        "expected_version": version,
                    },
                    raise_on_error=False,
                )
                assert invalid.is_error
            assert calls == [("pause_admissions", "pause", 1, None, "joe")]

    _as(_principal(), exercise)


def test_read_pagination_schema_rejects_integer_coercion(monkeypatch):
    from core.mcp_app import mcp as shared
    from fastmcp import Client

    monkeypatch.setattr(mcp, "_status_payload", lambda *args: {"ok": True})
    monkeypatch.setattr(mcp, "_escalations_payload", lambda *args: {"ok": True})

    async def exercise():
        async with Client(shared) as client:
            for name in ("factory_status", "factory_escalations"):
                for value in (True, "1"):
                    result = await client.call_tool(
                        name, {"offset": value, "limit": 1}, raise_on_error=False
                    )
                    assert result.is_error

    _as(_principal(), exercise)


def test_detail_pages_nodes_and_bounds_attempt_history(monkeypatch):
    from contextlib import nullcontext
    from datetime import datetime, timezone
    from types import SimpleNamespace

    row = SimpleNamespace(
        id=42, task_id="task", updated_at=datetime(2026, 9, 14, tzinfo=timezone.utc)
    )
    db = SimpleNamespace(get=lambda model, identity: row if identity == 42 else None)
    monkeypatch.setattr(
        "factory.orchestration.factory_controls._read_session", lambda: nullcontext(db)
    )
    monkeypatch.setattr(
        "factory.orchestration.factory_controls._snapshot",
        lambda db, row: {
            "id": row.id,
            "task_id": row.task_id,
            "state": "admitted",
        },
    )
    monkeypatch.setattr(
        "factory.orchestration.graph.load_graph",
        lambda task, session: [
            {"node_key": f"node-{i}", "deps": ["node-0"] if i else []} for i in range(5)
        ],
    )
    monkeypatch.setattr(
        "factory.orchestration.graph.node_runs",
        lambda task, session: [
            {"node_key": "node-1", "attempt": i, "status": "failed", "session_id": i}
            for i in range(1, 6)
        ],
    )
    monkeypatch.setattr(
        mcp, "_work_item_context", lambda *args: {"status": "not_linked"}
    )
    monkeypatch.setattr(mcp, "_queue_context", lambda *args: None)
    monkeypatch.setattr(
        mcp,
        "_lifecycle_evidence",
        lambda *args: {
            "deployment": {"status": "unknown", "coverage": "not_tracked_by_factory"}
        },
    )
    result = _as(
        _principal(), lambda: mcp.factory_task_detail(42, node_offset=1, limit=2)
    )
    assert result["receipt"]["receipt_id"] == 42
    assert result["node_count"] == 5
    assert result["next_node_offset"] == 3
    assert [n["node_key"] for n in result["nodes"]] == ["node-1", "node-2"]
    node = result["nodes"][0]
    assert node["deps"] == ["node-0"]
    assert node["attempt_count"] == 5
    assert [a["session_id"] for a in node["attempts"]] == [3, 4, 5]
    assert node["state"] == "failed"
    assert result["updated_at"] == "2026-09-14T00:00:00+00:00"
    assert result["lifecycle"]["deployment"]["status"] == "unknown"
    assert (
        _as(_principal(), lambda: mcp.factory_task_detail(99))["reason"]
        == "unknown_receipt"
    )


def test_work_item_and_queue_context_use_real_owner_schemas(tmp_path):
    from datetime import datetime, timezone

    from sqlmodel import Session, SQLModel, create_engine

    from factory.orchestration.factory_models import (
        FactoryControl,
        FactoryGithubIssueState,
        FactoryReceipt,
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
    )

    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp-owner-contract.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryGithubIssueState,
            )
        ],
    )
    source_time = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add(
            FactoryControl(
                id="factory",
                state="enabled",
                policy_json='{"generation":2}',
                actor="test",
            )
        )
        blocker = WorkItem(
            title="Blocking work",
            state="open",
            source_kind="github",
            source_ref="https://github.com/owner/repo/issues/6",
            trust="trusted",
            authority="github",
            github_repo="owner/repo",
            github_issue_number=6,
        )
        item = WorkItem(
            title="Current work",
            state="ready",
            source_kind="github",
            source_ref="https://github.com/owner/repo/issues/7",
            trust="trusted",
            authority="github",
            github_repo="owner/repo",
            github_issue_number=7,
            labels=["agent-ready"],
        )
        db.add_all([blocker, item])
        db.flush()
        db.add(WorkItemEdge(from_id=blocker.id, to_id=item.id, kind="blocks"))
        for version in range(1, 4):
            db.add(
                WorkItemEvent(
                    work_item_id=item.id,
                    version=version,
                    op="sync",
                    author_kind="factory",
                    author="factory:webhook",
                    change_json='{"state":"ready"}',
                    cause_kind="github_sync",
                    cause_ref=f"delivery:{version}",
                    stated_reason=f"correction {version}",
                )
            )
        db.add(
            FactoryGithubIssueState(
                repo="owner/repo",
                issue_number=7,
                source_updated_at=source_time,
                source_state="open",
                source_ref="delivery:3",
            )
        )
        stale = FactoryReceipt(
            repo="old/repo",
            issue_number=5,
            generation=1,
            title="Stale work",
            body="",
            url="https://github.com/old/repo/issues/5",
            actor="joe",
        )
        other_repo = FactoryReceipt(
            repo="other/repo",
            issue_number=9,
            generation=2,
            title="Earlier work in another repo",
            body="",
            url="https://github.com/other/repo/issues/9",
            actor="joe",
        )
        first = FactoryReceipt(
            repo="owner/repo",
            issue_number=7,
            generation=2,
            title="Current work",
            body="",
            url="https://github.com/owner/repo/issues/7",
            actor="joe",
            work_item_id=item.id,
        )
        second = FactoryReceipt(
            repo="owner/repo",
            issue_number=8,
            generation=2,
            title="Later work",
            body="",
            url="https://github.com/owner/repo/issues/8",
            actor="joe",
        )
        db.add_all([stale, other_repo, first, second])
        db.commit()
        db.refresh(first)
        db.refresh(stale)

        context = mcp._work_item_context(db, first, limit=2)
        queue = mcp._queue_context(db, first)
        stale_queue = mcp._queue_context(db, stale)

    engine.dispose()
    assert context["blocked"] is True
    assert context["blocked_by"][0]["issue_number"] == 6
    assert context["source_updated_at"] == source_time.isoformat()
    assert [event["version"] for event in context["corrections"]] == [3, 2]
    assert context["corrections_truncated"] is True
    assert queue["position"] == 2
    assert queue["count"] == 3
    assert queue["priority_mutation"] == "unavailable_no_owner"
    assert stale_queue["position"] is None
    assert stale_queue["count"] is None
    assert stale_queue["coverage"] == "not_in_current_generation_queue"


def test_lifecycle_keeps_artifact_acceptance_and_deployment_distinct(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from sqlmodel import Session, SQLModel, create_engine

    from factory.orchestration.factory_models import (
        FactoryAudit,
        FactoryReviewVerdict,
        FactoryStart,
    )

    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp-lifecycle-contract.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (FactoryStart, FactoryAudit, FactoryReviewVerdict)
        ],
    )
    observed = datetime(2026, 9, 20, 13, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add_all(
            [
                FactoryStart(
                    task_id="task-1",
                    start_key="factory-node:task-1:implement:1",
                    actor="factory",
                    model="luna",
                    max_cost_usd=2,
                    status="succeeded",
                    cost_usd=1,
                    created_at=observed,
                ),
                FactoryReviewVerdict(
                    task_id="task-1",
                    review_run_id=9,
                    task_class="bug-fix",
                    sample_kind="delivery",
                    verdict="changes_requested",
                    summary="First pass requested changes",
                    head_sha="b" * 40,
                    reviewed_at=observed,
                ),
                FactoryAudit(
                    actor="factory:landing",
                    action="repository_delivery_complete",
                    task_id="task-1",
                    created_at=observed,
                ),
            ]
        )
        db.commit()
        lifecycle = mcp._lifecycle_evidence(
            db,
            SimpleNamespace(task_id="task-1"),
            {
                "starts": [
                    {
                        "start_key": "factory-node:task-1:implement:1",
                        "created_at": observed,
                    }
                ],
                "evidence": {
                    "state": "ready_for_review",
                    "pr_url": "https://github.com/owner/repo/pull/9",
                    "head_sha": "a" * 40,
                    "reviewer_model": "opus",
                },
            },
        )

    engine.dispose()
    assert lifecycle["work_started"]["status"] == "observed"
    assert lifecycle["artifact_produced"]["status"] == "observed"
    assert lifecycle["delivery_accepted"]["status"] == "observed"
    assert lifecycle["delivery_accepted"]["evidence"]["head_sha"] == "a" * 40
    assert lifecycle["delivery_accepted"]["first_pass_review"] == {
        "verdict": "changes_requested",
        "summary": "First pass requested changes",
        "head_sha": "b" * 40,
        "reviewed_at": observed.isoformat(),
    }
    assert "last_review" not in lifecycle["delivery_accepted"]
    assert lifecycle["repository_delivery"]["status"] == "complete"
    assert lifecycle["deployment"] == {
        "status": "unknown",
        "coverage": "not_tracked_by_factory",
        "evidence": None,
    }


def test_submission_retry_reads_receipt_without_github(monkeypatch):
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.get_issue_receipt",
        lambda *args: {
            "ok": True,
            "created": False,
            "receipt": {"id": 42, "state": "succeeded", "repo": "owner/repo"},
        },
    )

    def closed_or_offline(*args):
        raise AssertionError("a durable retry must not depend on GitHub")

    monkeypatch.setattr(
        "factory.orchestration.factory_router.factory_receipt", closed_or_offline
    )
    result = _as(_principal(), lambda: mcp.factory_submit_issue("owner/repo", 7))
    assert result["created"] is False
    assert result["receipt"]["receipt_id"] == 42
    assert result["receipt"]["state"] == "succeeded"
