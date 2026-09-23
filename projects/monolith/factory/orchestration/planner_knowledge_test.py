"""Receipt-scoped staged knowledge inputs for Astra planning."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import conductor_context
from factory.orchestration import factory_conductor as conductor
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)
from factory.orchestration.turn_artifact import schema_errors


@pytest.fixture
def planner_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'planner-knowledge.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = (
        SwarmTask,
        SwarmPlanVersion,
        SwarmPlanNode,
        SwarmNodeRun,
        SwarmConductorCall,
        FactoryClassTier,
        FactoryControl,
        FactoryReceipt,
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
        FactoryReviewVerdict,
        FactoryStart,
        FactoryAudit,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    policy = {
        "repo": "owner/repo",
        "issue_numbers": [7],
        "generation": 3,
        "max_tasks": {"delivery": 1, "advisory": 1},
        "max_turns_per_task": 18,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["astra", "luna"],
        "conductor_model": "astra",
        "reviewer_model": "astra",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 2,
    }
    task_id = "t-planner-knowledge"
    with Session(engine) as db:
        db.add(
            FactoryControl(
                id="factory",
                actor="operator",
                state="enabled",
                policy_json=json.dumps(policy),
            )
        )
        db.add(
            SwarmTask(
                id=task_id,
                task_text="STALE ADMISSION ACCEPTANCE",
                repo="owner/repo",
                base_branch="main",
                conductor_model="astra",
                budget_usd=30.0,
                workflow_id=f"factory:{task_id}",
                start_state="factory",
            )
        )
        db.flush()
        db.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=7,
                generation=3,
                title="Current receipt title",
                body="Current receipt acceptance and operator constraint.",
                url="https://github.com/owner/repo/issues/7",
                actor="operator",
                task_class="bug-fix",
                routing_tier="delivery",
                state="admitted",
                task_id=task_id,
                policy_json=json.dumps(policy),
                direction_json=json.dumps(
                    {
                        "option_key": "stage",
                        "label": "Stage repository delivery",
                        "effect": "agent-ready",
                        "detail": {"scope": "repository only"},
                        "note": "Keep live acceptance open.",
                        "actor": "operator",
                        "decided_at": "2026-09-22T22:00:00+00:00",
                    }
                ),
            )
        )
        db.commit()
    for module in (conductor, conductor.graph, controls):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    yield engine, task_id, policy
    engine.dispose()


def _knowledge(note_id="authorized-current", raw_id="raw-authorized"):
    return {
        "status": "available",
        "scope": "repo:owner/repo",
        "authority": "receipt_authorized_untrusted_evidence",
        "observed_at": "2026-09-22T22:30:00+00:00",
        "retrieved_at": "2026-09-22T22:30:00+00:00",
        "notes": [
            {
                "note_id": note_id,
                "title": "Corrected evidence",
                "snippet": "The current bounded fact.",
                "scope": "repo:owner/repo",
                "verification_state": "disputed",
                "disputed": True,
                "confidence": 0.7,
                "valid_from": "2026-09-22T20:00:00+00:00",
                "valid_until": None,
                "observed_at": "2026-09-22T21:00:00+00:00",
                "evidence_raw_ids": [raw_id],
            }
        ],
        "omitted": {"unauthorized_candidates": 2, "prompt_budget": 0},
    }


def _factory_context(task_id, *, knowledge=None, body=None):
    return {
        "ok": True,
        "authorization": {
            "status": "authorized",
            "task_id": task_id,
            "receipt_id": 1,
            "generation": 3,
            "repo": "owner/repo",
        },
        "observed_at": "2026-09-22T22:30:00+00:00",
        "receipt_updated_at": "2026-09-22T22:20:00+00:00",
        "acceptance": {
            "source": "factory_receipt",
            "title": "Current receipt title",
            "body": body or "Current receipt acceptance and operator constraint.",
            "url": "https://github.com/owner/repo/issues/7",
        },
        "operator_constraints": {"generation": 3, "max_attempts": 2},
        "operator_direction": {
            "option_key": "stage",
            "label": "Stage repository delivery",
            "effect": "agent-ready",
            "detail": {"scope": "repository only"},
            "note": "Keep live acceptance open.",
            "actor": "operator",
            "decided_at": "2026-09-22T22:00:00+00:00",
        },
        "control": {
            "state": "admitted",
            "task_paused": False,
            "cancellation_requested": False,
            "turns_used": 1,
        },
        "decision": None,
        "recent_exchanges": [],
        "followups": [],
        "knowledge": knowledge or _knowledge(),
    }


def _task(task_id):
    return {
        "id": task_id,
        "task_text": "STALE ADMISSION ACCEPTANCE",
        "repo": "owner/repo",
        "base_branch": "main",
        "conductor_model": "astra",
        "budget_usd": 30.0,
    }


def _context(prompt):
    return json.loads(prompt.rsplit("\n", 1)[1])


def test_flag_off_keeps_legacy_planner_input(planner_db, monkeypatch):
    _engine, task_id, _policy = planner_db
    monkeypatch.delenv("FACTORY_PLANNER_KNOWLEDGE_ENABLED", raising=False)
    task = _task(task_id)
    assert conductor._load_planner_factory_context(task["id"]) is None
    context = _context(conductor.planner_prompt(task, [], []))
    assert context["task"] == "STALE ADMISSION ACCEPTANCE"
    assert "factory" not in context
    assert "knowledge" not in context


def test_enabled_prompt_uses_current_receipt_and_preserves_citation_metadata(
    planner_db,
):
    _engine, task_id, _policy = planner_db
    task = _task(task_id)
    context = _context(
        conductor.planner_prompt(
            task,
            [],
            [],
            deviation={
                "code": "node_failed",
                "node_key": "implement_fix",
                "evidence": "current failure",
                "text": "repair it",
            },
            factory_context=_factory_context(task["id"]),
        )
    )
    assert "STALE ADMISSION ACCEPTANCE" not in json.dumps(context)
    assert context["factory"]["acceptance"]["body"].startswith("Current receipt")
    assert context["factory"]["operator_constraints"]["generation"] == 3
    assert context["operator_direction"]["option_key"] == "stage"
    note = context["knowledge"]["notes"][0]
    assert note == _knowledge()["notes"][0]
    assert context["knowledge"]["retrieved_at"] == "2026-09-22T22:30:00+00:00"
    assert context["deviation"]["evidence"] == "current failure"


def test_initial_and_amendment_manifests_are_append_only(planner_db):
    engine, task_id, policy = planner_db
    task = _task(task_id)
    first_factory = _factory_context(task_id)
    first_prompt = conductor.planner_prompt(
        task, [], [], decision_revision=1, factory_context=first_factory
    )
    first_context = _context(first_prompt)
    choice = {"model": "astra"}
    assert conductor._add_planner_with_manifest(
        task, policy, "conductor_1", first_prompt, choice, "initial", 0, first_context
    ).ok

    corrected = _factory_context(
        task_id, knowledge=_knowledge("authorized-correction", "raw-correction")
    )
    corrected["receipt_updated_at"] = "2026-09-22T23:00:00+00:00"
    second_prompt = conductor.planner_prompt(
        task,
        conductor.graph.load_graph(task_id),
        [],
        decision_revision=2,
        factory_context=corrected,
    )
    second_context = _context(second_prompt)
    assert conductor._add_planner_with_manifest(
        task,
        policy,
        "conductor_2",
        second_prompt,
        choice,
        "amendment",
        1,
        second_context,
    ).ok

    with Session(engine) as db:
        rows = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "planner_context_manifest")
            .order_by(FactoryAudit.id)
        ).all()
        manifests = [json.loads(row.detail_json) for row in rows]
        assert [item["planner_node_key"] for item in manifests] == [
            "conductor_1",
            "conductor_2",
        ]
        assert manifests[0]["knowledge"]["notes"][0]["note_id"] == (
            "authorized-current"
        )
        assert manifests[1]["knowledge"]["notes"][0]["note_id"] == (
            "authorized-correction"
        )
        assert manifests[0]["authoritative"]["receipt_updated_at"] != (
            manifests[1]["authoritative"]["receipt_updated_at"]
        )


def test_receipt_authorization_excludes_same_repo_and_session_history(monkeypatch):
    async def embed(text):
        assert text == "current query"
        return [0.1]

    rows = [
        {
            **_knowledge()["notes"][0],
            "provenance": [{"raw_id": "raw-authorized"}],
        },
        {
            **_knowledge("other-receipt", "raw-other")["notes"][0],
            "provenance": [{"raw_id": "raw-other"}],
        },
        {
            **_knowledge("invalidated-authorized", "raw-invalidated")["notes"][0],
            "verification_state": "invalidated",
            "provenance": [{"raw_id": "raw-invalidated"}],
        },
        {
            **_knowledge("private-session", "raw-session")["notes"][0],
            "scope": "session:private",
            "provenance": [{"raw_id": "raw-session"}],
        },
    ]

    def search(vector, **kwargs):
        assert vector == [0.1]
        assert kwargs == {
            "limit": 30,
            "scope_filter": "repo:owner/repo",
            "exclude_invalidated": False,
        }
        return rows

    monkeypatch.setattr(
        "shared.embedding.EmbeddingClient", lambda: SimpleNamespace(embed=embed)
    )
    monkeypatch.setattr("core.db.get_engine", lambda: object())
    monkeypatch.setattr(conductor_context, "Session", lambda _: nullcontext(object()))
    monkeypatch.setattr(
        "knowledge.api.KnowledgeStore",
        lambda _: SimpleNamespace(search_notes_with_context=search),
    )
    monkeypatch.setattr(
        conductor_context,
        "_receipt_raw_ids",
        lambda *_args: {"raw-authorized", "raw-invalidated"},
    )
    result = asyncio.run(
        conductor_context.retrieve_knowledge(
            "current query",
            "repo:owner/repo",
            3,
            receipt_id=9,
            generation=3,
        )
    )
    assert [note["note_id"] for note in result["notes"]] == [
        "authorized-current",
        "invalidated-authorized",
    ]
    assert result["notes"][1]["verification_state"] == "invalidated"
    serialized = json.dumps(result)
    assert "other-receipt" not in serialized
    assert "private-session" not in serialized
    assert "raw-other" not in serialized
    assert result["omitted"]["unauthorized_candidates"] == 2


def test_kg_outage_is_visible_with_authoritative_factory_evidence(planner_db):
    _engine, task_id, _policy = planner_db
    task = _task(task_id)
    unavailable = {
        "status": "unavailable",
        "scope": "repo:owner/repo",
        "retrieved_at": "2026-09-22T23:10:00+00:00",
        "notes": [],
        "omitted": {"unauthorized_candidates": 0, "prompt_budget": 0},
    }
    context = _context(
        conductor.planner_prompt(
            task,
            [],
            [],
            factory_context=_factory_context(task["id"], knowledge=unavailable),
        )
    )
    assert context["knowledge"]["status"] == "unavailable"
    assert context["knowledge"]["notes"] == []
    assert context["factory"]["acceptance"]["body"].startswith("Current receipt")
    manifest = conductor._planner_input_manifest("conductor_1", context)
    assert manifest["knowledge"]["status"] == "unavailable"


def test_prompt_pressure_drops_knowledge_before_required_evidence(
    planner_db, monkeypatch
):
    _engine, task_id, _policy = planner_db
    task = _task(task_id)
    required = _factory_context(task["id"], knowledge={**_knowledge(), "notes": []})
    deviation = {
        "code": "node_failed",
        "node_key": "implement_fix",
        "evidence": "required deviation evidence",
        "text": "required deviation text",
    }
    baseline = _context(
        conductor.planner_prompt(
            task, [], [], deviation=deviation, factory_context=required
        )
    )
    monkeypatch.setattr(
        conductor,
        "PLANNER_CONTEXT_CHARS",
        len(conductor._planner_json(baseline)) + 100,
    )
    large = _factory_context(task["id"])
    large["knowledge"]["notes"][0]["snippet"] = "optional" * 2000
    context = _context(
        conductor.planner_prompt(
            task, [], [], deviation=deviation, factory_context=large
        )
    )
    assert context["knowledge"]["notes"] == []
    assert context["knowledge"]["omitted"]["prompt_budget"] == 1
    assert context["factory"]["acceptance"] == baseline["factory"]["acceptance"]
    assert context["factory"]["control"] == baseline["factory"]["control"]
    assert context["operator_direction"] == baseline["operator_direction"]
    assert context["deviation"] == baseline["deviation"]


def test_request_context_is_schema_bounded_and_durably_linked(
    planner_db, monkeypatch
):
    engine, task_id, _policy = planner_db
    monkeypatch.setenv("FACTORY_PLANNER_KNOWLEDGE_ENABLED", "true")

    async def current_context(selected_task_id, query=None, limit=5):
        assert selected_task_id == task_id
        assert query == "Which correction is current?"
        assert limit == 5
        return _factory_context(task_id)

    monkeypatch.setattr(conductor_context, "planner_context", current_context)
    decision = {
        "action": "request_context",
        "reason": "Need corrected evidence",
        "query": "Which correction is current?",
    }
    assert not schema_errors(decision, conductor.DECISION_SCHEMA)
    assert schema_errors(
        {**decision, "receipt_id": 1}, conductor.DECISION_SCHEMA
    )
    assert schema_errors(
        {"action": "request_context", "reason": "missing query"},
        conductor.DECISION_SCHEMA,
    )
    code, _reason = conductor._request_planner_context(
        _task(task_id), decision, "factory-decision:conductor_1:1"
    )
    assert code == "context_provided"
    with Session(engine) as db:
        request = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "planner_context_request"
            )
        ).one()
        result = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "planner_context_result")
        ).one()
        request_detail = json.loads(request.detail_json)
        result_detail = json.loads(result.detail_json)
        assert result_detail["request_audit_id"] == request.id
        assert result_detail["request_id"] == request_detail["request_id"]
        assert request_detail["bounds"] == {
            "iterations": 1,
            "queries": 1,
            "results": 5,
            "timeout_seconds": 8,
        }
        assert result_detail["authorization"]["receipt_id"] == 1
        assert result_detail["knowledge"]["notes"][0]["evidence_raw_ids"] == [
            "raw-authorized"
        ]
    second, _reason = conductor._request_planner_context(
        _task(task_id), decision, "factory-decision:conductor_2:1"
    )
    assert second == "context_request_limit"


def test_request_context_records_authorization_failure(planner_db, monkeypatch):
    engine, task_id, _policy = planner_db
    monkeypatch.setenv("FACTORY_PLANNER_KNOWLEDGE_ENABLED", "true")

    async def refused(_task_id, query=None, limit=5):
        return {"ok": False, "reason": "invalid_receipt_authorization"}

    monkeypatch.setattr(conductor_context, "planner_context", refused)
    code, _reason = conductor._request_planner_context(
        _task(task_id),
        {
            "action": "request_context",
            "reason": "Need evidence",
            "query": "current evidence",
        },
        "factory-decision:conductor_1:1",
    )
    assert code == "context_authorization_failed"
    with Session(engine) as db:
        result = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "planner_context_result")
        ).one()
        assert json.loads(result.detail_json)["error"] == (
            "invalid_receipt_authorization"
        )


def test_request_context_records_bounded_retrieval_failure(planner_db, monkeypatch):
    engine, task_id, _policy = planner_db
    monkeypatch.setenv("FACTORY_PLANNER_KNOWLEDGE_ENABLED", "true")

    async def failed(_task_id, query=None, limit=5):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(conductor_context, "planner_context", failed)
    code, _reason = conductor._request_planner_context(
        _task(task_id),
        {
            "action": "request_context",
            "reason": "Need evidence",
            "query": "current evidence",
        },
        "factory-decision:conductor_1:1",
    )
    assert code == "context_unavailable"
    with Session(engine) as db:
        result = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "planner_context_result")
        ).one()
        assert json.loads(result.detail_json)["error"] == "context_retrieval_failed"


def test_request_context_enforces_and_records_timeout(planner_db, monkeypatch):
    engine, task_id, _policy = planner_db
    monkeypatch.setenv("FACTORY_PLANNER_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setattr(conductor, "PLANNER_CONTEXT_REQUEST_TIMEOUT_SECONDS", 0.001)

    async def slow(_task_id, query=None, limit=5):
        await asyncio.sleep(0.05)
        return _factory_context(task_id)

    monkeypatch.setattr(conductor_context, "planner_context", slow)
    code, _reason = conductor._request_planner_context(
        _task(task_id),
        {
            "action": "request_context",
            "reason": "Need evidence",
            "query": "current evidence",
        },
        "factory-decision:conductor_1:1",
    )
    assert code == "context_unavailable"
    with Session(engine) as db:
        result = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "planner_context_result")
        ).one()
        assert json.loads(result.detail_json)["error"] == "context_timeout"


def test_server_derives_receipt_identity_and_fails_closed(planner_db):
    engine, task_id, _policy = planner_db
    current = conductor_context._planner_factory_context(task_id)
    assert current["authorization"] == {
        "status": "authorized",
        "task_id": task_id,
        "receipt_id": 1,
        "generation": 3,
        "repo": "owner/repo",
    }
    with Session(engine) as db:
        task = db.get(SwarmTask, task_id)
        task.workflow_id = "factory:another-task"
        db.add(task)
        db.commit()
    assert conductor_context._planner_factory_context(task_id) == {
        "ok": False,
        "reason": "invalid_receipt_authorization",
    }
