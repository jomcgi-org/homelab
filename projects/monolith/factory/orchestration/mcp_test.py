"""The factory MCP tools: registration, the operator gate, and the trim."""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind
import factory.orchestration.factory_controls as controls
from factory.orchestration import mcp
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmTask


def test_planner_derived_context_marks_due_and_unknown_stale():
    from datetime import datetime, timedelta, timezone
    from factory.orchestration.conductor_context import _is_stale

    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    current = {
        "observed_at": (now - timedelta(days=1)).isoformat(),
        "review_after": (now + timedelta(seconds=1)).isoformat(),
        "verification_state": "verified",
    }
    assert not _is_stale(current, now=now)
    assert _is_stale(current, now=now + timedelta(seconds=1))
    assert _is_stale({**current, "review_after": None}, now=now)
    assert _is_stale({**current, "verification_state": "invalidated"}, now=now)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                SwarmTask,
                FactoryControl,
                FactoryReceipt,
                FactoryStart,
                FactoryAudit,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def seed_policy(db, issue_numbers):
    with Session(db) as session:
        control = session.get(FactoryControl, "factory")
        control.policy_json = json.dumps({"issue_numbers": list(issue_numbers)})
        session.add(control)
        session.commit()


def stored_receipts(db):
    with Session(db) as session:
        return session.exec(select(FactoryReceipt)).all()


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
        "factory_amend_policy",
        "factory_configure_refill",
        "factory_release_landing_hold",
        "factory_find_receipts",
        "factory_prepare_validation",
        "factory_preview_refinement_batch",
        "factory_apply_refinement_batch",
        "factory_request_brief",
        "factory_context",
        "factory_verify_synthetics",
    } <= registered, f"factory tools not registered; got: {sorted(registered)}"


def test_status_distinguishes_native_registration_from_caller_catalog(monkeypatch):
    monkeypatch.setattr(mcp, "_status_payload", lambda *_: {"ok": True})
    result = _as(_principal(), mcp.factory_status)
    catalog = result["control_catalog"]
    assert catalog["source"] == "native_server_registry"
    assert catalog["caller_visibility"] == "not_observed"
    assert set(catalog["tools"]) == {
        "factory_configure_refill",
        "factory_amend_policy",
        "factory_reconcile_learning_execution",
    }
    refill = catalog["tools"]["factory_configure_refill"]
    assert refill["registered"] is True
    assert refill["operations"] == ["preview", "apply"]
    assert refill["models"] == ["astra", "opus"]
    assert len(refill["schema_sha256"]) == 64
    assert len(refill["description_sha256"]) == 64
    assert len(json.dumps(catalog)) < 2500


def test_status_reports_missing_native_control_without_inventing_visibility(
    monkeypatch,
):
    monkeypatch.setattr(mcp, "_status_payload", lambda *_: {"ok": True})
    original = mcp.mcp.get_tool

    async def missing(name):
        return None if name == "factory_configure_refill" else await original(name)

    monkeypatch.setattr(mcp.mcp, "get_tool", missing)
    catalog = _as(_principal(), mcp.factory_status)["control_catalog"]
    assert catalog["tools"]["factory_configure_refill"] == {"registered": False}
    assert catalog["tools"]["factory_amend_policy"]["registered"] is True


def test_status_does_not_probe_registry_for_a_nonoperator(monkeypatch):
    async def forbidden(_name):
        raise AssertionError("nonoperator reached native catalogue")

    monkeypatch.setattr(mcp.mcp, "get_tool", forbidden)
    assert (
        _as(_principal(groups=()), mcp.factory_status)["error"] == mcp.OPERATOR_REQUIRED
    )


def _synthetic_request():
    return {
        "run_id": "3f58f64d-1eaa-47a5-b989-032061f6e399",
        "chart_version": "1.2.3",
        "image": "example@sha256:" + "b" * 64,
        "source_commit": "a" * 40,
        "promotion": "prod.synthetic",
    }


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
    ],
)
@pytest.mark.parametrize("dry_run", [True, False])
def test_synthetics_refuses_before_preflight_or_execution(
    principal, dry_run, monkeypatch
):
    def forbidden(*args):
        raise AssertionError("non-operator reached synthetic actions")

    monkeypatch.setattr(mcp, "_synthetics_payload", forbidden)
    result = _as(
        principal,
        lambda: mcp.factory_verify_synthetics(**_synthetic_request(), dry_run=dry_run),
    )
    assert result["error"] == mcp.OPERATOR_REQUIRED


def test_synthetics_defaults_to_preflight_and_uses_existing_endpoint(monkeypatch):
    from core import db
    from factory.orchestration import factory_router, synthetic_actions
    from fastapi import HTTPException

    engine = object()
    calls = []
    monkeypatch.setattr(db, "get_engine", lambda: engine)

    def preflight(received_engine, **identity):
        assert received_engine is engine
        assert identity == {
            k: _synthetic_request()[k] for k in ("chart_version", "image")
        }
        calls.append("preflight")
        return {"verdict": "ready", "synthetic_actions_executed": False}

    def verify(received_engine, **request):
        assert received_engine is engine
        assert request == _synthetic_request()
        calls.append("run")
        return {"verdict": "pass"}

    monkeypatch.setattr(synthetic_actions, "preflight", preflight)
    monkeypatch.setattr(synthetic_actions, "verify", verify)
    monkeypatch.delenv("FACTORY_SYNTHETICS_ENABLED", raising=False)
    result = _as(
        _principal(), lambda: mcp.factory_verify_synthetics(**_synthetic_request())
    )
    assert result["synthetic_actions_executed"] is False
    assert calls == ["preflight"]
    with pytest.raises(HTTPException) as refused:
        _as(
            _principal(),
            lambda: mcp.factory_verify_synthetics(
                **_synthetic_request(), dry_run=False
            ),
        )
    assert refused.value.status_code == 404
    assert calls == ["preflight"]
    monkeypatch.setenv("FACTORY_SYNTHETICS_ENABLED", "true")
    original = factory_router.factory_synthetics

    def endpoint(body, principal):
        assert principal == _principal()
        return original(body, principal)

    monkeypatch.setattr(factory_router, "factory_synthetics", endpoint)
    assert _as(
        _principal(),
        lambda: mcp.factory_verify_synthetics(**_synthetic_request(), dry_run=False),
    ) == {"verdict": "pass"}
    assert calls == ["preflight", "run"]


def test_planner_preview_adapter_does_not_register_a_tool_or_relax_operator_floor():
    from factory.orchestration import planner_preview
    from core.mcp_app import mcp as shared

    registered = {tool.name for tool in asyncio.run(shared.list_tools())}
    assert "preview_planner_decision" not in registered
    assert "factory_planner_preview" not in registered
    principal = _principal(authority=Authority.DELEGATED, kind=PrincipalKind.WORKLOAD)
    assert mcp._refuse(principal)["error"] == mcp.OPERATOR_REQUIRED
    assert planner_preview.verified_planner_binding(principal) is None
    assert mcp._refuse(_principal()) is None
    assert planner_preview.verified_planner_binding(_principal()) is None


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
    result = _as(_principal(), mcp.factory_status)
    assert result["ok"] is True
    assert result["control_catalog"]["source"] == "native_server_registry"


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
            },
        }
    )
    assert row["limits_tripped"] == ["turn_limit_reached"]
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


@pytest.mark.parametrize(
    "receipt,state",
    [
        ({}, "not_requested"),
        ({"cancellation_requested": True}, "no_owned_work"),
        (
            {
                "cancellation_requested": True,
                "nodes": [{"node_key": "work", "state": "running"}],
            },
            "work_still_running",
        ),
        (
            {
                "cancellation_requested": True,
                "starts": [
                    {"start_key": "factory-node:t:work:1", "status": "reserved"}
                ],
                "nodes": [{"node_key": "work", "state": "running"}],
            },
            "work_still_running",
        ),
        (
            {
                "cancellation_requested": True,
                "starts": [
                    {"start_key": "factory-node:t:work:1", "status": "uncertain"}
                ],
            },
            "unknown_or_unreachable",
        ),
        (
            {
                "cancellation_requested": True,
                "starts": [
                    {"start_key": "factory-node:t:work:1", "status": "cancelled"}
                ],
            },
            "cancellation_requested",
        ),
        (
            {
                "cancellation_requested": True,
                "starts": [
                    {"start_key": "factory-node:t:work:1", "status": "cancelled"}
                ],
                "stop_events": [
                    {
                        "workflow_id": "factory-node:t:work:1",
                        "cessation_confirmed": True,
                    }
                ],
            },
            "cessation_confirmed",
        ),
    ],
)
def test_stop_summary_never_promotes_cancellation_or_expiry_to_cessation(
    receipt, state
):
    assert mcp._stop_summary(receipt)["state"] == state


def test_stop_summary_requires_exact_proof_for_every_owned_workflow():
    summary = mcp._stop_summary(
        {
            "cancellation_requested": True,
            "starts": [
                {"start_key": "factory-node:t:one:1", "status": "cancelled"},
                {"start_key": "factory-node:t:two:1", "status": "cancelled"},
            ],
            "stop_events": [
                {
                    "workflow_id": "factory-node:t:one:1",
                    "cessation_confirmed": True,
                },
                {
                    "workflow_id": "factory-node:t:two:1",
                    "cessation_confirmed": False,
                },
            ],
        }
    )
    assert summary["state"] == "cancellation_requested"
    assert summary["cessation_confirmed_workflows"] == ["factory-node:t:one:1"]


def seed_operator_rows(db, specs):
    from datetime import datetime, timezone

    # Equal timestamps exercise the identity tie-breaker in both SQL dialects.
    when = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    with Session(db) as session, session.begin():
        session.add_all(
            [
                FactoryReceipt(
                    repo="owner/repo",
                    title="card",
                    body="body",
                    url="https://example.test",
                    actor="operator",
                    created_at=when,
                    updated_at=when,
                    **spec,
                )
                for spec in specs
            ]
        )


def test_status_payload_trims_the_board_and_stamps_coverage(db, monkeypatch):
    monkeypatch.setattr(mcp, "_now", lambda: "2026-09-12T10:00:00+00:00")
    seed_operator_rows(
        db,
        [
            {"issue_number": 1, "state": "admitted"},
            {"issue_number": 2},
            {"issue_number": 3, "state": "succeeded"},
            {
                "issue_number": 4,
                "state": "succeeded",
                "escalation_json": '{"question":"which?"}',
            },
            {
                "issue_number": 5,
                "state": "cancelled",
                "escalation_json": '{"question":"gone?","resolved":{}}',
            },
        ],
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


def test_status_payload_includes_recent_on_request(db):
    seed_operator_rows(db, [{"issue_number": 3, "state": "succeeded"}])

    payload = mcp._status_payload(include_recent=True)
    assert [task["issue_number"] for task in payload["recent"]] == [3]


def test_status_payload_pages_each_bucket_and_keeps_queue_positions(db):
    seed_operator_rows(
        db,
        [
            *({"issue_number": n, "state": "admitted"} for n in range(1, 7)),
            *({"issue_number": n} for n in range(11, 18)),
        ],
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


def test_status_queue_positions_exclude_stale_generations(db):
    with Session(db) as session, session.begin():
        session.get(FactoryControl, "factory").policy_json = '{"generation":2}'
    seed_operator_rows(
        db,
        [
            {"issue_number": 1, "generation": 1},
            {"issue_number": 2, "generation": 2},
            {"issue_number": 3, "generation": 2},
        ],
    )

    payload = mcp._status_payload(False)

    assert [row["issue_number"] for row in payload["queued"]] == [2, 3]
    assert [row["queue_position"] for row in payload["queued"]] == [1, 2]
    assert payload["pagination"]["queued"]["total"] == 2


def test_status_payload_passes_through_an_uninitialised_factory(db):
    with Session(db) as session, session.begin():
        session.delete(session.get(FactoryControl, "factory"))

    payload = mcp._status_payload(include_recent=False)
    assert payload["ok"] is False
    assert payload["reason"] == "not_initialized"
    assert payload["coverage"]["cloud_sessions"] == "not_indexed"


def test_escalations_payload_hides_resolved_cards_by_default(db):
    seed_operator_rows(
        db,
        [
            {"issue_number": 9, "escalation_json": '{"question":"which?"}'},
            {
                "issue_number": 8,
                "escalation_json": '{"question":"gone?","resolved":{}}',
            },
        ],
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


def test_escalations_payload_passes_through_an_uninitialised_factory(db):
    with Session(db) as session, session.begin():
        session.delete(session.get(FactoryControl, "factory"))

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
        (
            "factory_control",
            {
                "action": "cancel_task",
                "request_key": "cancel",
                "expected_version": 1,
                "task_id": "t",
                "cancellation_reason": "No longer needed",
            },
        ),
        ("factory_task_detail", {"receipt_id": 1}),
        (
            "factory_amend_policy",
            {
                "operation": "apply",
                "receipt_id": 1,
                "overlay": {"task_budget_usd": 65},
                "direction": "continue",
                "request_key": "amend",
                "expected_preview_sha256": "a" * 64,
            },
        ),
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
            "exclude_invalidated": False,
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
        context,
        "_current_records",
        lambda db, receipt, *, actor: (
            {"control": {}, "queue": {}, "task": {"id": receipt.id}},
            {"direction": None},
            None,
        ),
    )
    monkeypatch.setattr(context, "_request_records", lambda db: {})
    monkeypatch.setattr(context, "continuity_enabled", lambda: False)

    result = context._factory_context(7, actor="joe")

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


def test_context_knowledge_preserves_supersession_provenance_and_stale_state(
    monkeypatch,
):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from factory.orchestration import conductor_context as context

    async def embed(_text):
        return [0.1]

    def search(_vector, **_kwargs):
        return [
            {
                "note_id": "corrected",
                "scope": "repo:owner/repo",
                "snippet": "Use the corrected constraint.",
                "verification_state": "unverified",
                "disputed": False,
                "edges": [
                    {
                        "target_id": "old",
                        "edge_type": "supersedes",
                        "target_title": "Old constraint",
                    },
                    {"target_id": "private-note", "edge_type": "related"},
                ],
                "provenance": [{"raw_id": "raw-new"}],
            },
            {
                "note_id": "old",
                "scope": "repo:owner/repo",
                "snippet": "Use the old constraint.",
                "verification_state": "invalidated",
                "disputed": False,
                "edges": [],
                "provenance": [{"raw_id": "raw-old"}],
            },
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
    result = asyncio.run(context.retrieve_knowledge("constraint", "repo:owner/repo", 5))
    assert result["notes"][0]["supersedes"] == ["old"]
    assert result["notes"][0]["evidence_raw_ids"] == ["raw-new"]
    assert result["notes"][1]["verification_state"] == "invalidated"
    assert result["notes"][1]["stale"] is True
    assert result["notes"][1]["evidence_raw_ids"] == ["raw-old"]
    assert "private-note" not in result["notes"][0]["supersedes"]


def test_factory_context_passes_authenticated_operator_to_shared_contract(monkeypatch):
    calls = []

    async def read(receipt_id, query, limit, *, actor):
        calls.append((receipt_id, query, limit, actor))
        return {"ok": True, "audience": "conductor"}

    monkeypatch.setattr("factory.orchestration.conductor_context.read_context", read)
    result = _as(
        _principal(),
        lambda: mcp.factory_context(7, query="current priorities", knowledge_limit=3),
    )
    assert result == {"ok": True, "audience": "conductor"}
    assert calls == [(7, "current priorities", 3, "joe")]


def test_knowledge_failure_does_not_undo_a_completed_decision(monkeypatch):
    from factory.orchestration import conductor_context as context

    monkeypatch.setenv("CONDUCTOR_CONTINUITY_ENABLED", "true")
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


def test_request_knowledge_maintenance_is_staged_off(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_CONTINUITY_ENABLED", raising=False)
    monkeypatch.setattr(
        "factory.orchestration.factory_decisions.request_decision",
        lambda *a, **kw: {"ok": True, "state": "completed"},
    )
    result = mcp._decide(1, "decision:abc", "close", "r", None, "joe")
    assert result["state"] == "completed"
    assert result["knowledge"] == {"status": "disabled"}


@pytest.mark.parametrize("action", ["stop", "enable_auto_merge", "disable_auto_merge"])
def test_control_attributes_authenticated_actor_and_preserves_owner_result(
    monkeypatch, action
):
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
            action,
            "stop",
            3,
        ),
    )
    assert result == acknowledgement
    assert calls == [
        (
            action,
            "joe",
            {
                "request_key": "stop",
                "expected_version": 3,
                "task_id": None,
                "receipt_id": None,
                "cancellation_reason": None,
            },
        )
    ]


def test_control_passes_receipt_id_for_cancel_receipt(monkeypatch):
    calls = []
    acknowledgement = {
        "ok": True,
        "state": "cancelled",
        "receipt_id": 9,
        "request_key": "cancel-9",
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
            "cancel_receipt",
            "cancel-9",
            3,
            receipt_id=9,
        ),
    )
    assert result == acknowledgement
    assert calls == [
        (
            "cancel_receipt",
            "joe",
            {
                "request_key": "cancel-9",
                "expected_version": 3,
                "task_id": None,
                "receipt_id": 9,
                "cancellation_reason": None,
            },
        )
    ]


def test_cancel_task_control_preserves_reason_and_verified_actor(monkeypatch):
    calls = []

    def request(action, actor, **kwargs):
        calls.append((action, actor, kwargs))
        return {"ok": True, "state": "enabled"}

    monkeypatch.setattr(controls, "request_control", request)
    result = _as(
        _principal(),
        lambda: mcp.factory_control(
            "cancel_task",
            "cancel",
            3,
            task_id="t",
            cancellation_reason="No longer needed",
        ),
    )
    assert result["ok"]
    assert calls == [
        (
            "cancel_task",
            "joe",
            {
                "request_key": "cancel",
                "expected_version": 3,
                "task_id": "t",
                "receipt_id": None,
                "cancellation_reason": "No longer needed",
            },
        )
    ]


def test_submit_preserves_durable_receipt_identity_and_duplicate_result(monkeypatch):
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.get_issue_receipt", lambda *args: None
    )
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.current_operator_allowlist",
        lambda **kwargs: {7},
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
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.current_operator_allowlist",
        lambda **kwargs: {7},
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


@pytest.mark.parametrize("issue_numbers", [[7], []])
def test_submit_refuses_issue_outside_policy_allowlist(db, monkeypatch, issue_numbers):
    """#6483: an MCP submission admit_next can never admit creates no receipt."""
    seed_policy(db, issue_numbers)
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.get_issue_receipt", lambda *args: None
    )

    def offline(*args):
        raise AssertionError("an ineligible submit must not reach the receipt owner")

    monkeypatch.setattr("factory.orchestration.factory_router.factory_receipt", offline)
    result = _as(_principal(), lambda: mcp.factory_submit_issue("owner/repo", 8))
    assert result["ok"] is False
    assert result["state"] == "ineligible"
    assert result["reason"] == "ineligible: not in policy issue_numbers"
    assert result["issue_number"] == 8
    assert stored_receipts(db) == []


def test_submit_queues_allowlisted_issue(db, monkeypatch):
    seed_policy(db, [7])
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.get_issue_receipt", lambda *args: None
    )
    calls = []

    def receive(body, principal):
        calls.append((body.model_dump(), principal.subject))
        return {
            "ok": True,
            "created": True,
            "receipt": {
                "id": 1,
                "repo": "owner/repo",
                "issue_number": 7,
                "generation": 0,
                "state": "queued",
                "task_id": None,
            },
        }

    monkeypatch.setattr("factory.orchestration.factory_router.factory_receipt", receive)
    result = _as(_principal(), lambda: mcp.factory_submit_issue("owner/repo", 7))
    assert result["ok"] is True
    assert result["created"] is True
    assert result["receipt"]["receipt_id"] == 1
    assert calls == [
        ({"repo": "owner/repo", "issue_number": 7, "generation": 0}, "joe")
    ]


def test_submit_returns_existing_receipt_for_ineligible_issue_on_retry(db, monkeypatch):
    """A retry after the REFUSE still reads the durable receipt, never a second."""
    seed_policy(db, [7])
    with Session(db) as session:
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=8,
                generation=0,
                title="old",
                body="",
                url="https://github.com/owner/repo/issues/8",
                actor="joe",
                state="queued",
            )
        )
        session.commit()
        receipt_id = (
            session.exec(select(FactoryReceipt).where(FactoryReceipt.issue_number == 8))
            .one()
            .id
        )
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})

    def offline(*args):
        raise AssertionError("a durable retry must not create another receipt")

    monkeypatch.setattr("factory.orchestration.factory_router.factory_receipt", offline)
    result = _as(_principal(), lambda: mcp.factory_submit_issue("owner/repo", 8))
    assert result["ok"] is True
    assert result["created"] is False
    assert result["receipt"]["receipt_id"] == receipt_id
    assert len(stored_receipts(db)) == 1


def test_submit_without_posted_policy_keeps_previous_behaviour(db, monkeypatch):
    """No control policy yet: nothing to check against, so the submit proceeds."""
    monkeypatch.setattr("goosecracker.api.REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(
        "factory.orchestration.factory_intake.get_issue_receipt", lambda *args: None
    )

    def receive(body, principal):
        return {
            "ok": True,
            "created": True,
            "receipt": {
                "id": 1,
                "repo": "owner/repo",
                "issue_number": 7,
                "generation": 0,
                "state": "queued",
                "task_id": None,
            },
        }

    monkeypatch.setattr("factory.orchestration.factory_router.factory_receipt", receive)
    result = _as(_principal(), lambda: mcp.factory_submit_issue("owner/repo", 7))
    assert result["ok"] is True
    assert result["created"] is True


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
            schema = tools["factory_control"].inputSchema["properties"]
            assert {"cancel_task", "enable_auto_merge", "disable_auto_merge"} <= set(
                schema["action"]["enum"]
            )
            assert "cancellation_reason" in schema
            reason_schema = schema["cancellation_reason"]
            string_schema = next(
                variant
                for variant in reason_schema["anyOf"]
                if variant["type"] == "string"
            )
            assert string_schema["maxLength"] == 1024
            assert "4096 ASCII-escaped JSON characters" in reason_schema["description"]
            assert "cancel_task" in tools["factory_control"].description
            assert (
                "4096 ASCII-escaped JSON characters"
                in tools["factory_control"].description
            )
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
            cancelled = await client.call_tool(
                "factory_control",
                {
                    "action": "cancel_task",
                    "request_key": "cancel",
                    "expected_version": 2,
                    "task_id": "t",
                    "cancellation_reason": "No longer needed",
                },
            )
            assert cancelled.data["ok"]
            assert calls == [
                ("pause_admissions", "pause", 1, None, "joe", None, None),
                ("cancel_task", "cancel", 2, "t", "joe", None, "No longer needed"),
            ]

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
    monkeypatch.setattr(mcp, "_watchdog_detail", lambda *args: None)
    monkeypatch.setattr(
        "factory.orchestration.factory_funding.continuation_status",
        lambda session, task: {"reason": "progress_authority_changed", "task_id": task},
    )
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
    assert result["progress_continuation"] == {
        "reason": "progress_authority_changed",
        "task_id": "task",
    }
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


def test_permit_diagnostics_require_operator_before_read(monkeypatch):
    from factory.execution import admission_diagnostics

    def fail(*_args, **_kwargs):
        raise AssertionError("unauthorized read")

    monkeypatch.setattr(admission_diagnostics, "inspect_permits", fail)
    result = _as(_principal(groups=()), lambda: mcp.factory_permit_diagnostics([11254]))
    assert result["error"] == mcp.OPERATOR_REQUIRED


def test_permit_diagnostics_use_worker_thread(monkeypatch):
    import threading
    from factory.execution import admission_diagnostics

    caller = threading.get_ident()

    def read(ids):
        assert threading.get_ident() != caller
        return {"ids": ids, "recovery_authorized": False}

    monkeypatch.setattr(admission_diagnostics, "inspect_permits", read)
    assert _as(_principal(), lambda: mcp.factory_permit_diagnostics([11254])) == {
        "ids": [11254],
        "recovery_authorized": False,
    }


def test_permit_diagnostics_wire_schema_rejects_invalid_ids(monkeypatch):
    from fastmcp import Client
    from core.mcp_app import mcp as shared
    from factory.execution import admission_diagnostics

    calls = []

    def inspect(ids):
        calls.append(ids)
        return {"items": [], "recovery_authorized": False}

    monkeypatch.setattr(admission_diagnostics, "inspect_permits", inspect)

    async def exercise():
        async with Client(shared) as client:
            for ids in ([], [True], ["11254"], [0], [2**63], list(range(1, 22))):
                result = await client.call_tool(
                    "factory_permit_diagnostics",
                    {"permit_ids": ids},
                    raise_on_error=False,
                )
                assert result.is_error
            result = await client.call_tool(
                "factory_permit_diagnostics", {"permit_ids": [11254]}
            )
            assert result.data["recovery_authorized"] is False
        assert calls == [[11254]]

    _as(_principal(), exercise)


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {
            "classification": "private_internal_reversible",
            "evidence": "Inspected exact diff",
        },
    ],
)
def test_landing_release_requires_human_operator_and_reviewed_preview(
    monkeypatch, scope
):
    from factory.orchestration import factory_landing_release as releases

    calls = []
    monkeypatch.setattr(
        releases,
        "apply",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    async def invoke():
        return await mcp.factory_release_landing_hold(
            operation="apply",
            receipt_id=890,
            pr_number=7170,
            head_sha="a" * 40,
            reason="Joe authorized this reviewed delivery",
            request_key="one",
            expected_preview_sha256="b" * 64,
            scope=scope,
        )

    assert _as(_principal(), invoke)["ok"]
    assert calls[0][1]["actor"] == "joe"
    assert calls[0][1].get("scope") == scope
    calls.clear()
    for principal in (
        _principal(groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(principal, invoke)["ok"]
    assert calls == []

    async def unpreviewed():
        return await mcp.factory_release_landing_hold(
            operation="apply",
            receipt_id=890,
            pr_number=7170,
            head_sha="a" * 40,
            reason="approved",
        )

    assert _as(_principal(), unpreviewed)["reason"] == (
        "landing_release_requires_preview_and_request_key"
    )
    assert calls == []


def test_expired_task_recovery_requires_human_and_reviewed_preview(monkeypatch):
    from factory.orchestration import factory_task_recovery as recovery

    calls = []
    monkeypatch.setattr(
        recovery, "apply", lambda *a, **kw: calls.append((a, kw)) or {"ok": True}
    )

    async def invoke():
        return await mcp.factory_recover_expired_task(
            "apply",
            838,
            "2026-10-18T23:00:00+00:00",
            9,
            "Recover the original task",
            request_key="once",
            expected_preview_sha256="a" * 64,
        )

    assert _as(_principal(), invoke)["ok"]
    assert calls[0][1]["actor"] == "joe"
    calls.clear()
    for principal in (
        _principal(groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(principal, invoke)["ok"]
    assert calls == []

    async def unpreviewed():
        return await mcp.factory_recover_expired_task(
            "apply", 838, "2026-10-18T23:00:00+00:00", 9, "Recover"
        )

    assert (
        _as(_principal(), unpreviewed)["reason"]
        == "task_recovery_requires_preview_and_request_key"
    )
    assert calls == []


def test_review_recovery_requires_human_operator_and_uses_subject(monkeypatch):
    from factory.orchestration import review_recovery

    calls = []
    monkeypatch.setattr(
        review_recovery,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )

    async def invoke():
        return await mcp.factory_review_recovery(
            receipt_id=822,
            pr_number=6953,
            head_sha="a" * 40,
            request_key="explicit",
            deadline_at="2026-10-12T00:00:00Z",
            cumulative_budget_usd=80,
            cumulative_turn_limit=18,
        )

    assert _as(_principal(), invoke)["ok"]
    assert calls[0][1]["actor"] == "joe"
    calls.clear()
    assert not _as(_principal(groups=()), invoke)["ok"]
    assert calls == []


@pytest.mark.parametrize(
    "proof,expected",
    [
        (None, "pending"),
        ("rollout_observed", "pending"),
        ("rollout_verified", "verified"),
    ],
)
def test_lifecycle_reports_merge_separately_from_rollout(tmp_path, proof, expected):
    from types import SimpleNamespace
    from sqlmodel import Session, SQLModel, create_engine
    from factory.orchestration.factory_models import FactoryAudit, FactoryReviewVerdict

    engine = create_engine(
        f"sqlite:///{tmp_path / 'merge-proof.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[model.__table__ for model in (FactoryAudit, FactoryReviewVerdict)],
    )
    with Session(engine) as db:
        db.add(
            FactoryAudit(
                task_id="task-1",
                actor="factory:landing",
                action="owned_pr_merged",
                detail_json="{}",
            )
        )
        if proof:
            db.add(
                FactoryAudit(
                    task_id="task-1",
                    actor="factory:landing",
                    action=proof,
                    detail_json=json.dumps(
                        {
                            "verified": proof == "rollout_verified",
                            "applications": [],
                            "approved_head_sha": "a" * 40,
                            "pr_number": 3,
                        }
                    ),
                )
            )
        db.commit()
        result = mcp._lifecycle_evidence(
            db,
            SimpleNamespace(task_id="task-1", repo="owner/repo"),
            {
                "evidence": {
                    "head_sha": "a" * 40,
                    "pr_url": "https://github.com/owner/repo/pull/3",
                }
            },
        )
        assert result["repository_delivery"]["status"] == "merged"
        assert result["deployment"]["status"] == expected
        assert result["delivery_accepted"]["status"] == "not_observed"
    engine.dispose()


@pytest.mark.parametrize("recovery", [False, True])
def test_lifecycle_does_not_reuse_old_rollout_proof(tmp_path, recovery):
    from types import SimpleNamespace
    from sqlmodel import Session, SQLModel, create_engine
    from factory.orchestration.factory_models import FactoryAudit, FactoryReviewVerdict

    engine = create_engine(
        f"sqlite:///{tmp_path / 'old-proof.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[model.__table__ for model in (FactoryAudit, FactoryReviewVerdict)],
    )
    with Session(engine) as db:
        db.add(
            FactoryAudit(
                task_id="task-1",
                actor="factory:landing",
                action="rollout_verified",
                detail_json=json.dumps(
                    {"verified": True, "approved_head_sha": "a" * 40, "pr_number": 3}
                ),
            )
        )
        db.flush()
        if recovery:
            db.add(
                FactoryAudit(
                    task_id="task-1",
                    actor="factory",
                    action="landing_recovery_requested",
                )
            )
            db.flush()
            db.add(
                FactoryAudit(task_id="task-1", actor="factory", action="delivery_ready")
            )
            db.flush()
            db.add(
                FactoryAudit(
                    task_id="task-1",
                    actor="factory:landing",
                    action="rollout_observed",
                    detail_json='{"verified":false}',
                )
            )
        db.commit()
        result = mcp._lifecycle_evidence(
            db,
            SimpleNamespace(task_id="task-1", repo="owner/repo"),
            {
                "evidence": {
                    "head_sha": "b" * 40,
                    "pr_url": "https://github.com/owner/repo/pull/3",
                }
            },
        )
        assert result["deployment"]["status"] != "verified"
        assert result["repository_delivery"]["status"] == "not_observed"
    engine.dispose()


@pytest.mark.parametrize("tool", ["prepare", "locate"])
def test_validation_tools_retain_operator_floor(monkeypatch, tool):
    from factory.orchestration import factory_validation as validation

    calls = []
    monkeypatch.setattr(
        validation, "preview", lambda *a: calls.append(a) or {"ok": True}
    )
    monkeypatch.setattr(
        validation, "locate", lambda *a, **kw: calls.append(a) or {"ok": True}
    )

    async def invoke():
        if tool == "prepare":
            return await mcp.factory_prepare_validation("preview", 934, "bounded check")
        return await mcp.factory_find_receipts("org/repo", task_id="exact")

    for principal in (
        _principal(groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(principal, invoke)["ok"]
    assert not calls
    assert _as(_principal(), invoke)["ok"]
    assert len(calls) == 1


def test_validation_apply_requires_preview_identity(monkeypatch):
    from factory.orchestration import factory_validation as validation

    def unexpected(*a, **kw):
        pytest.fail("unpreviewed apply reached mutation")

    monkeypatch.setattr(validation, "apply", unexpected)

    async def invoke():
        return await mcp.factory_prepare_validation("apply", 934, "bounded check")

    assert (
        _as(_principal(), invoke)["reason"]
        == "validation_requires_preview_and_request_key"
    )


def test_receipt_locator_intersects_exact_identity_and_retains_ambiguity(
    db, monkeypatch
):
    from factory.orchestration import (
        factory_validation as validation,
        factory_conductor,
    )

    monkeypatch.setattr(validation, "_available_repo", lambda repo: repo)
    with Session(db) as session, session.begin():
        session.add_all(
            [
                SwarmTask(
                    id="delivery",
                    task_text="work",
                    conductor_model="astra",
                    budget_usd=50,
                ),
                SwarmTask(
                    id="advisory",
                    task_text="brief",
                    conductor_model="astra",
                    budget_usd=50,
                ),
            ]
        )
    with Session(db) as session, session.begin():
        session.add_all(
            [
                FactoryReceipt(
                    id=1,
                    repo="org/repo",
                    issue_number=6995,
                    title="brief",
                    body="",
                    url="https://example.invalid",
                    actor="operator",
                    task_id="advisory",
                    task_class="refine",
                    state="succeeded",
                ),
                FactoryReceipt(
                    id=2,
                    repo="org/repo",
                    issue_number=6995,
                    title="delivery",
                    body="",
                    url="https://example.invalid",
                    actor="operator",
                    task_id="delivery",
                    state="escalated",
                ),
            ]
        )
    pull = {
        "number": 7199,
        "head": {"ref": "factory/delivery", "repo": {"full_name": "org/repo"}},
        "base": {"repo": {"full_name": "org/repo"}},
    }
    monkeypatch.setattr(factory_conductor, "github_get", lambda *a: pull)
    result = validation.locate("org/repo", issue_number=6995, limit=1)
    assert result["ambiguous"] and result["total"] == 2 and result["next_offset"] == 1
    assert (
        validation.locate("org/repo", issue_number=6995, offset=1)["receipts"][0][
            "receipt_id"
        ]
        == 2
    )
    result = validation.locate(
        "org/repo", task_id="delivery", pr_number=7199, issue_number=6995
    )
    assert [r["receipt_id"] for r in result["receipts"]] == [2]
    assert not validation.locate("org/repo", task_id="advisory", pr_number=7199)[
        "receipts"
    ]
    assert not validation.locate("org/other", task_id="delivery")["receipts"]
    with pytest.raises(ValueError, match="receipt_selector_required"):
        validation.locate("org/repo")
    pull["head"]["repo"]["full_name"] = "foreign/repo"
    with pytest.raises(ValueError, match="receipt_pr_identity_changed"):
        validation.locate("org/repo", pr_number=7199)
    assert len(stored_receipts(db)) == 2


@pytest.mark.parametrize(
    "prior,adopted", [(False, False), (True, False), (False, True)]
)
def test_receipt_locator_resolves_granted_queued_delivery(
    db, monkeypatch, prior, adopted
):
    from factory.orchestration import (
        factory_validation as validation,
        factory_conductor,
    )

    monkeypatch.setattr(validation, "_available_repo", lambda repo: repo)
    branch = "fix/existing" if adopted else "factory/existing"
    prefix = "prior" if prior else "delivery"
    direction = {f"{prefix}_branch": branch, f"{prefix}_pr_number": 7199}
    if adopted:
        direction["delivery_adoption"] = True
    with Session(db) as session, session.begin():
        session.add(
            FactoryReceipt(
                id=7,
                repo="org/repo",
                issue_number=6995,
                title="existing",
                body="",
                url="https://example.invalid",
                actor="operator",
                state="queued",
                direction_json=json.dumps(direction),
            )
        )
    pull = {
        "number": 7199,
        "head": {"ref": branch, "repo": {"full_name": "org/repo"}},
        "base": {"repo": {"full_name": "org/repo"}},
    }
    monkeypatch.setattr(factory_conductor, "github_get", lambda *a: pull)
    result = validation.locate("org/repo", pr_number=7199)
    assert [r["receipt_id"] for r in result["receipts"]] == [7]
    assert result["receipts"][0]["recorded_pr_number"] == 7199
    pull["head"]["ref"] = "factory/unrelated"
    assert not validation.locate("org/repo", pr_number=7199)["receipts"]


def test_routine_permit_lookup_operator_gate_and_worker_thread(monkeypatch):
    import threading
    from factory.execution import admission_diagnostics

    caller = threading.get_ident()
    calls = []

    def read(name, **kwargs):
        assert threading.get_ident() != caller
        calls.append((name, kwargs))
        return {"items": [], "recovery_authorized": False}

    monkeypatch.setattr(admission_diagnostics, "find_routine_permits", read)
    result = _as(
        _principal(groups=()),
        lambda: mcp.factory_find_routine_permits("kg-fact-review"),
    )
    assert result["error"] == mcp.OPERATOR_REQUIRED
    assert calls == []
    result = _as(
        _principal(),
        lambda: mcp.factory_find_routine_permits("kg-fact-review", offset=1, limit=2),
    )
    assert result["recovery_authorized"] is False
    assert calls == [("kg-fact-review", {"offset": 1, "limit": 2})]


def test_fresh_issue_preparation_wire_and_authority(monkeypatch):
    from fastmcp import Client
    from core.mcp_app import mcp as shared
    from factory.orchestration import factory_validation as validation

    calls = []
    monkeypatch.setattr(
        validation, "preview", lambda *a, **kw: calls.append((a, kw)) or {"ok": True}
    )

    async def invoke():
        return await mcp.factory_prepare_validation(
            "preview", None, "selected work", issue_number=7275
        )

    assert not _as(_principal(authority=Authority.DELEGATED), invoke)["ok"]
    assert not calls

    async def wire():
        async with Client(shared) as client:
            for issue in (True, "7275", 0, 2**63):
                result = await client.call_tool(
                    "factory_prepare_validation",
                    dict(
                        operation="preview",
                        receipt_id=None,
                        reason="selected work",
                        issue_number=issue,
                    ),
                    raise_on_error=False,
                )
                assert result.is_error
            result = await client.call_tool(
                "factory_prepare_validation",
                dict(
                    operation="preview",
                    receipt_id=None,
                    reason="selected work",
                    issue_number=7275,
                ),
            )
            assert result.data["ok"]

    _as(_principal(), wire)
    assert calls == [((None, "selected work"), {"issue_number": 7275})]


def test_fresh_issue_dedicated_tools_require_typed_apply_and_operator(monkeypatch):
    from fastmcp import Client
    from core.mcp_app import mcp as shared
    from factory.orchestration import factory_validation as validation

    calls = []
    monkeypatch.setattr(
        validation,
        "preview",
        lambda *a, **kw: calls.append(("preview", a, kw)) or {"ok": True},
    )
    monkeypatch.setattr(
        validation,
        "apply",
        lambda *a, **kw: calls.append(("apply", a, kw)) or {"ok": True},
    )

    async def preview():
        return await mcp.factory_preview_issue_admission(7275, "selected work")

    async def apply():
        return await mcp.factory_apply_issue_admission(
            7275, "selected work", "a" * 64, "exact-key"
        )

    for principal in (
        _principal(groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(principal, preview)["ok"]
        assert not _as(principal, apply)["ok"]
    assert not calls

    async def wire():
        async with Client(shared) as client:
            valid = {
                "issue_number": 7275,
                "reason": "selected work",
                "expected_preview_sha256": "a" * 64,
                "request_key": "exact-key",
            }
            for field in valid:
                result = await client.call_tool(
                    "factory_apply_issue_admission",
                    {k: v for k, v in valid.items() if k != field},
                    raise_on_error=False,
                )
                assert result.is_error
            result = await client.call_tool(
                "factory_preview_issue_admission",
                {"issue_number": 7275, "reason": "selected work"},
            )
            assert result.data["ok"]
            result = await client.call_tool("factory_apply_issue_admission", valid)
            assert result.data["ok"]

    _as(_principal(), wire)
    assert calls[0] == ("preview", (None, "selected work"), {"issue_number": 7275})
    assert calls[1][0:2] == ("apply", (None, "selected work"))
    assert calls[1][2]["issue_number"] == 7275
    assert calls[1][2]["expected_preview_sha256"] == "a" * 64
    assert calls[1][2]["request_key"] == "exact-key"


def test_session_dispatch_diagnostics_operator_and_thread(monkeypatch):
    import threading
    from factory.execution import admission_diagnostics

    caller = threading.get_ident()
    calls = []

    def read(sid, seq):
        assert threading.get_ident() != caller
        calls.append((sid, seq))
        return {"recovery_authorized": False}

    monkeypatch.setattr(admission_diagnostics, "inspect_session_turn", read)
    result = _as(
        _principal(groups=()),
        lambda: mcp.factory_session_dispatch_diagnostics(21073, 2),
    )
    assert result["error"] == mcp.OPERATOR_REQUIRED and not calls
    assert _as(
        _principal(), lambda: mcp.factory_session_dispatch_diagnostics(21073, 2)
    ) == {"recovery_authorized": False}
    assert calls == [(21073, 2)]


def test_session_dispatch_diagnostics_strict_wire_identity(monkeypatch):
    from fastmcp import Client
    from core.mcp_app import mcp as shared
    from factory.execution import admission_diagnostics

    calls = []
    monkeypatch.setattr(
        admission_diagnostics,
        "inspect_session_turn",
        lambda sid, seq: calls.append((sid, seq)) or {"recovery_authorized": False},
    )
    for principal in (
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(
            principal, lambda: mcp.factory_session_dispatch_diagnostics(21073, 2)
        )["ok"]
    assert not calls

    async def wire():
        async with Client(shared) as client:
            for field in ("session_id", "seq"):
                for value in (True, "2", 0, -1, 2**63):
                    args = {"session_id": 21073, "seq": 2, field: value}
                    result = await client.call_tool(
                        "factory_session_dispatch_diagnostics",
                        args,
                        raise_on_error=False,
                    )
                    assert result.is_error
            result = await client.call_tool(
                "factory_session_dispatch_diagnostics", {"session_id": 21073, "seq": 2}
            )
            assert result.data["recovery_authorized"] is False

    _as(_principal(), wire)
    assert calls == [(21073, 2)]


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
    ],
)
def test_refinement_delivery_operator_gate_precedes_owner(principal, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized operation")

    monkeypatch.setattr(mcp, "_refinement_delivery", forbidden)
    for fn in (
        lambda: mcp.factory_preview_refinement_delivery(902, "continue"),
        lambda: mcp.factory_apply_refinement_delivery(902, "continue", "key", "a" * 64),
    ):
        assert _as(principal, fn)["error"] == mcp.OPERATOR_REQUIRED


def test_refinement_delivery_adapter_passes_only_server_actor_and_exact_request(
    monkeypatch,
):
    calls = []

    def owner(*args):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(mcp, "_refinement_delivery", owner)
    assert _as(
        _principal(), lambda: mcp.factory_preview_refinement_delivery(902, "continue")
    ) == {"ok": True}
    assert _as(
        _principal(),
        lambda: mcp.factory_apply_refinement_delivery(902, "continue", "key", "a" * 64),
    ) == {"ok": True}
    assert calls == [
        ("preview", 902, "continue", "joe"),
        ("apply", 902, "continue", "joe", "key", "a" * 64),
    ]


def test_refinement_delivery_tool_schema_has_no_restriction_override():
    from core.mcp_app import mcp as shared

    tools = {tool.name: tool for tool in asyncio.run(shared.list_tools())}
    for name, fields in (
        ("factory_preview_refinement_delivery", {"receipt_id", "reason"}),
        (
            "factory_apply_refinement_delivery",
            {"receipt_id", "reason", "request_key", "expected_preview_sha256"},
        ),
    ):
        assert set(tools[name].parameters["properties"]) == fields


def test_raw_extraction_diagnostics_operator_and_worker_thread(monkeypatch):
    import threading
    from factory.execution import admission_diagnostics

    caller = threading.get_ident()
    calls = []

    def read(name):
        assert threading.get_ident() != caller
        calls.append(name)
        return {"recovery_authorized": False}

    monkeypatch.setattr(admission_diagnostics, "inspect_raw_extraction", read)
    name = "kg:" + "a" * 64
    result = _as(
        _principal(groups=()), lambda: mcp.factory_raw_extraction_diagnostics(name)
    )
    assert result["error"] == mcp.OPERATOR_REQUIRED
    assert calls == []
    assert _as(_principal(), lambda: mcp.factory_raw_extraction_diagnostics(name)) == {
        "recovery_authorized": False
    }
    assert calls == [name]


def test_raw_extraction_diagnostics_declared_schema(monkeypatch):
    from fastmcp import Client
    from core.mcp_app import mcp as shared
    from factory.execution import admission_diagnostics

    calls = []

    def read(name):
        calls.append(name)
        return {"recovery_authorized": False}

    monkeypatch.setattr(admission_diagnostics, "inspect_raw_extraction", read)

    async def exercise():
        async with Client(shared) as client:
            for name in (
                True,
                "",
                "kg-fact-review",
                "kg:" + "a" * 65,
                "kg:" + "a" * 64 + "\n",
                "kg:" + "A" * 64,
            ):
                result = await client.call_tool(
                    "factory_raw_extraction_diagnostics",
                    {"job_name": name},
                    raise_on_error=False,
                )
                assert result.is_error
            result = await client.call_tool(
                "factory_raw_extraction_diagnostics", {"job_name": "kg:" + "a" * 64}
            )
            assert result.data["recovery_authorized"] is False
        assert calls == ["kg:" + "a" * 64]

    _as(_principal(), exercise)


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
    ],
)
def test_quota_probe_status_requires_operator_before_read(monkeypatch, principal):
    from factory import quota_probe

    def forbidden(**kwargs):
        raise AssertionError("unauthorized read")

    monkeypatch.setattr(quota_probe, "probe_status", forbidden)
    assert (
        _as(principal, mcp.factory_quota_probe_status)["error"] == mcp.OPERATOR_REQUIRED
    )


def test_quota_probe_status_delegates_only_bounded_selectors(monkeypatch):
    from factory import quota_probe

    monkeypatch.setattr(quota_probe, "probe_status", lambda **kwargs: kwargs)
    assert _as(_principal(), lambda: mcp.factory_quota_probe_status(7, 3)) == {
        "after_session_id": 7,
        "limit": 3,
    }


def test_backlog_preparation_requires_standing_operator_before_any_read(monkeypatch):
    from factory.orchestration import factory_validation as validation

    calls = []
    monkeypatch.setattr(
        validation,
        "preview_backlog",
        lambda *a, **kw: calls.append((a, kw)) or {"ok": True},
    )
    monkeypatch.setattr(
        validation,
        "apply_backlog",
        lambda *a, **kw: calls.append((a, kw)) or {"ok": True},
    )
    for principal in (_principal(groups=()), _principal(authority=Authority.DELEGATED)):
        assert not _as(principal, lambda: mcp.factory_preview_backlog_intake("resume"))[
            "ok"
        ]
        assert not _as(
            principal,
            lambda: mcp.factory_apply_backlog_intake("resume", "a" * 64, "key"),
        )["ok"]
    assert not calls
    assert _as(_principal(), lambda: mcp.factory_preview_backlog_intake("resume"))["ok"]
    assert _as(
        _principal(),
        lambda: mcp.factory_apply_backlog_intake("resume", "a" * 64, "key"),
    )["ok"]
    assert calls[0] == (("resume",), {"include_refinement": False})
    assert calls[1][1]["expected_preview_sha256"] == "a" * 64
    assert calls[1][1]["request_key"] == "key"


def test_refinement_batch_requires_standing_operator_before_any_read(monkeypatch):
    from factory.orchestration import factory_refinement_batch as batch

    calls = []
    monkeypatch.setattr(
        batch, "preview", lambda *a, **kw: calls.append((a, kw)) or {"ok": True}
    )
    monkeypatch.setattr(
        batch, "apply", lambda *a, **kw: calls.append((a, kw)) or {"ok": True}
    )
    for principal in (
        _principal(groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(
            principal, lambda: mcp.factory_preview_refinement_batch([7249], "briefs")
        )["ok"]
        assert not _as(
            principal,
            lambda: mcp.factory_apply_refinement_batch(
                [7249], "briefs", "a" * 64, "key"
            ),
        )["ok"]
    assert not calls
    assert _as(
        _principal(), lambda: mcp.factory_preview_refinement_batch([7249], "briefs")
    )["ok"]
    assert _as(
        _principal(),
        lambda: mcp.factory_apply_refinement_batch([7249], "briefs", "a" * 64, "key"),
    )["ok"]
    assert calls[0] == (([7249], "briefs"), {})
    assert calls[1][1]["request_key"] == "key"
    assert calls[1][1]["expected_preview_sha256"] == "a" * 64


def test_progress_continuation_requires_standing_human_and_preview(monkeypatch):
    from factory.orchestration import factory_funding as progress

    calls = []
    monkeypatch.setattr(
        progress,
        "apply_continuation",
        lambda *a, **kw: calls.append((a, kw)) or {"ok": True},
    )

    async def invoke():
        return await mcp.factory_amend_policy(
            "apply",
            927,
            {"progress_continuation": {"pr_number": 7436, "head_sha": "a" * 40}},
            "Continue saved work",
            "request",
            "b" * 64,
        )

    assert _as(_principal(), invoke)["ok"]
    assert calls[0][1]["actor"] == "joe"
    calls.clear()
    for principal in (
        _principal(groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(principal, invoke)["ok"]
    assert calls == []

    async def unpreviewed():
        return await mcp.factory_amend_policy(
            "apply",
            927,
            {"progress_continuation": {"pr_number": 7436, "head_sha": "a" * 40}},
            "Continue saved work",
        )

    assert (
        _as(_principal(), unpreviewed)["reason"]
        == "progress_requires_preview_and_request_key"
    )
    assert calls == []


def test_model_restriction_requires_standing_human_and_preview(monkeypatch):
    from factory.orchestration import factory_policy_amendment as amendments

    calls = []
    monkeypatch.setattr(
        amendments, "apply", lambda *a, **kw: calls.append((a, kw)) or {"ok": True}
    )

    async def invoke():
        return await mcp.factory_amend_policy(
            "apply",
            927,
            {"remove_models": ["spark"]},
            "Restrict unused lane",
            "request",
            "b" * 64,
        )

    for principal in (
        _principal(groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(kind=PrincipalKind.WORKLOAD),
    ):
        assert not _as(principal, invoke)["ok"]
    assert not calls
    assert _as(_principal(), invoke)["ok"]
    assert calls[0][0] == (927, {"remove_models": ["spark"]}, "Restrict unused lane")
    assert calls[0][1]["actor"] == "joe"
    calls.clear()

    async def unpreviewed():
        return await mcp.factory_amend_policy(
            "apply", 927, {"remove_models": ["spark"]}, "Restrict unused lane"
        )

    assert (
        _as(_principal(), unpreviewed)["reason"]
        == "amendment_requires_preview_and_request_key"
    )
    assert not calls


@pytest.mark.parametrize(
    "overlay",
    [
        {"progress_continuation": {"pr_number": 7436}},
        {
            "progress_continuation": {"pr_number": 7436, "head_sha": "a" * 40},
            "task_budget_usd": 500,
        },
    ],
)
def test_progress_mode_cannot_mix_policy_or_omit_identity(monkeypatch, overlay):
    from factory.orchestration import factory_funding as progress

    monkeypatch.setattr(
        progress,
        "preview_continuation",
        lambda *a: pytest.fail("Invalid mode reached continuation"),
    )

    async def invoke():
        return await mcp.factory_amend_policy(
            "preview", 927, overlay, "Continue saved work"
        )

    assert _as(_principal(), invoke)["reason"] == "progress_exact_target_required"


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
    ],
)
def test_probe_reconciliation_requires_standing_operator(monkeypatch, principal):
    from factory import probe_reconciliation

    def forbidden(*a, **kw):
        raise AssertionError("unauthorized")

    monkeypatch.setattr(probe_reconciliation, "preview", forbidden)
    assert (
        _as(
            principal,
            lambda: mcp.factory_reconcile_quota_probe(
                "preview", 6463, 1, 3339, "reason"
            ),
        )["error"]
        == mcp.OPERATOR_REQUIRED
    )


def test_probe_reconciliation_adapter_binds_actor_and_requires_preview(monkeypatch):
    from factory import probe_reconciliation

    calls = []
    monkeypatch.setattr(
        probe_reconciliation, "apply", lambda *args: calls.append(args) or {"ok": True}
    )
    p = _principal()
    assert (
        _as(
            p,
            lambda: mcp.factory_reconcile_quota_probe("apply", 6463, 1, 3339, "reason"),
        )["reason"]
        == "probe_preview_required"
    )
    assert not calls
    assert _as(
        p,
        lambda: mcp.factory_reconcile_quota_probe(
            "apply", 6463, 1, 3339, "reason", "key", "digest"
        ),
    )["ok"]
    assert calls == [(p.subject, 6463, 1, 3339, "reason", "key", "digest")]


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
    ],
)
def test_refill_control_requires_standing_operator(monkeypatch, principal):
    from factory.orchestration import refill_control

    def forbidden(*args):
        raise AssertionError("unauthorized")

    monkeypatch.setattr(refill_control, "preview", forbidden)
    assert (
        _as(
            principal,
            lambda: mcp.factory_configure_refill(
                "preview", True, "astra", True, 0, "reason"
            ),
        )["error"]
        == mcp.OPERATOR_REQUIRED
    )


def test_refill_adapter_binds_actor_and_requires_preview(monkeypatch):
    from factory.orchestration import refill_control

    calls = []
    monkeypatch.setattr(
        refill_control,
        "apply",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )
    p = _principal()
    assert (
        _as(
            p,
            lambda: mcp.factory_configure_refill(
                "apply", True, "astra", True, 0, "reason"
            ),
        )["reason"]
        == "refill_preview_required"
    )
    assert not calls
    assert _as(
        p,
        lambda: mcp.factory_configure_refill(
            "apply", True, "astra", True, 0, "reason", "key", "digest"
        ),
    )["ok"]
    assert calls == [
        (
            (p.subject, True, "astra", True, 0, "reason", "key", "digest"),
            {"reset_breaker": False, "min_new_issues": None},
        )
    ]


def test_progress_preview_returns_bounded_subscription_refusal(monkeypatch):
    from factory.orchestration import factory_funding as progress

    diagnostics = {
        "refused_model": {
            "model": "spark",
            "provider": None,
            "state": "unsupported_subscription_feed",
        }
    }

    def refuse(*args):
        raise progress.SubscriptionRefusal(
            "progress_subscription_evidence_stale", diagnostics
        )

    monkeypatch.setattr(progress, "preview_continuation", refuse)
    result = _as(
        _principal(),
        lambda: mcp.factory_amend_policy(
            "preview",
            927,
            {"progress_continuation": {"pr_number": 7436, "head_sha": "a" * 40}},
            "explain without changing admission",
        ),
    )
    assert result == {
        "ok": False,
        "reason": "progress_subscription_evidence_stale",
        "subscription_diagnostics": diagnostics,
    }


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
    ],
)
def test_learning_reconciliation_requires_standing_operator(monkeypatch, principal):
    from factory import learning_reconciliation

    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized caller reached recovery")

    monkeypatch.setattr(learning_reconciliation, "preview", forbidden)
    assert (
        _as(
            principal,
            lambda: mcp.factory_reconcile_learning_execution(
                "preview", 18004, 1, 15160, "reason"
            ),
        )["error"]
        == mcp.OPERATOR_REQUIRED
    )


def test_learning_reconciliation_adapter_binds_actor_and_requires_digest(monkeypatch):
    from factory import learning_reconciliation

    calls = []

    async def apply(*args):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(learning_reconciliation, "apply", apply)
    p = _principal()
    assert (
        _as(
            p,
            lambda: mcp.factory_reconcile_learning_execution(
                "apply", 18004, 1, 15160, "reason"
            ),
        )["reason"]
        == "learning_preview_required"
    )
    assert not calls
    assert _as(
        p,
        lambda: mcp.factory_reconcile_learning_execution(
            "apply", 18004, 1, 15160, "reason", "stable", "digest"
        ),
    )["ok"]
    assert calls == [(p.subject, 18004, 1, 15160, "reason", "stable", "digest")]
