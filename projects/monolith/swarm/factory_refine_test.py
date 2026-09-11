"""Single-node refine admission and server-side settlement regressions."""

from __future__ import annotations

from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from swarm import factory_conductor as conductor
from swarm import factory_controls as controls
from swarm import factory_refine as refine
from swarm import graph
from swarm.factory_intake import admit_next, receive_issue
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'refine.db'}",
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
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        FactoryAudit,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (conductor, controls, graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def make_task(task_class="refine"):
    policy = {
        "repo": "owner/repo",
        "issue_numbers": [7],
        "generation": 0,
        "max_tasks": 1,
        "max_turns_per_task": 20,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 4,
    }
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        "owner/repo",
        7,
        "Clarify delivery",
        "Untrusted issue body.",
        "https://github.com/owner/repo/issues/7",
        "factory:intake",
        task_class=task_class,
    )
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    return conductor._task(admitted["task_id"]), admitted["policy"]


def add_refine_node(task, policy):
    refine.reconcile(task, policy, [], [], graph.current_version(task["id"]))
    return graph.load_graph(task["id"])[0]


def settle_attempt(task, status, value=None):
    runs = graph.node_runs(task["id"], refine.NODE_KEY)
    attempt = len(runs) + 1
    key = f"factory-node:{task['id']}:{refine.NODE_KEY}:{attempt}"
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task['id']}",
        "workflow_id": key,
        "artifact_path": f".factory/{refine.NODE_KEY}.json",
        "artifact_schema": refine.REFINE_SCHEMA,
        "hydration_branch": "main",
        "retry_context": "[]",
    }
    assert conductor.reserve_node(task["id"], refine.NODE_KEY, key, context)
    assert graph.record_dispatch(
        task["id"], refine.NODE_KEY, attempt, 100 + attempt, None
    ).ok
    outcome = {"value": value or {}}
    assert graph.record_outcome(
        task["id"],
        refine.NODE_KEY,
        attempt,
        status,
        0.1,
        None,
        json.dumps(outcome),
    ).ok
    assert controls.record_start_outcome(
        task["id"],
        key,
        status,
        "worker",
        cost_usd=0.1,
        session_id=100 + attempt,
    )["ok"]
    return graph.node_runs(task["id"], refine.NODE_KEY)[-1]


def verified_github(monkeypatch, task, outcome="agent-ready", **comment_overrides):
    admitted = controls.task_snapshot(task["id"])["admitted_at"]
    comment = {
        "body": "## Agent brief\n### Outcome\nReady",
        "created_at": (
            datetime.fromisoformat(admitted) + timedelta(seconds=1)
        ).isoformat(),
        "html_url": "https://github.com/owner/repo/issues/7#issuecomment-1",
        "user": {"login": "factory-bot"},
    }
    comment.update(comment_overrides)
    monkeypatch.setattr(
        refine, "github_get", lambda *_args: {"labels": [{"name": outcome}]}
    )
    monkeypatch.setattr(refine, "github_list", lambda *_args: [comment])
    return comment


def audit_actions(db):
    with Session(db) as session:
        return [row.action for row in session.exec(select(FactoryAudit)).all()]


def test_empty_refine_graph_adds_one_conductor_pool_node(db):
    task, policy = make_task()
    node = add_refine_node(task, policy)
    assert node["node_key"] == "refine_1"
    assert node["model"] == "opus"
    assert node["max_attempts"] == 2
    assert node["kind"] == "work" and node["side_effects"] is True
    assert "Factory refine task" in node["prompt"]
    refine.reconcile(task, policy, [node], [], graph.current_version(task["id"]))
    assert len(graph.load_graph(task["id"])) == 1


def test_advisory_diagnosis_pauses_and_audits_without_a_node(db):
    task, policy = make_task("advisory-diagnosis")
    conductor.reconcile_task(task["id"], policy, object())
    assert graph.load_graph(task["id"]) == []
    assert controls.task_snapshot(task["id"])["task_paused"] is True
    with Session(db) as session:
        audit = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "advisory_class_unimplemented"
            )
        ).one()
    assert json.loads(audit.detail_json)["task_class"] == "advisory-diagnosis"


def test_verified_agent_ready_settles_succeeded(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task)
    run = settle_attempt(
        task,
        "succeeded",
        {"outcome": "agent-ready", "comment_url": comment["html_url"]},
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "succeeded"
    assert snapshot["evidence"] == {
        "state": "refine_agent_ready",
        "reason": comment["html_url"],
    }


def test_missing_claimed_label_is_terminal_mismatch(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task)
    monkeypatch.setattr(refine, "github_get", lambda *_args: {"labels": []})
    run = settle_attempt(
        task,
        "succeeded",
        {"outcome": "agent-ready", "comment_url": comment["html_url"]},
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "failed"
    assert snapshot["evidence"]["state"] == "refine_unverified"
    assert "refine_mismatch" in audit_actions(db)


@pytest.mark.parametrize("case", ["missing", "before", "author"])
def test_unverified_comment_is_refused(db, monkeypatch, case):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task)
    if case == "missing":
        monkeypatch.setattr(refine, "github_list", lambda *_args: [])
    elif case == "before":
        comment["created_at"] = "2000-01-01T00:00:00Z"
    else:
        monkeypatch.setenv("FACTORY_EXECUTOR_LOGIN", "expected-bot")
        comment["user"] = {"login": "other-bot"}
    run = settle_attempt(
        task,
        "succeeded",
        {"outcome": "agent-ready", "comment_url": comment["html_url"]},
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_unverified"
    )


def test_needs_human_notifies_once(db, monkeypatch):
    from agent import notify as notify_module

    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    sent = []

    async def notify(text, *, level):
        sent.append((text, level))

    monkeypatch.setattr(notify_module, "notify", notify)
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "needs-human",
            "comment_url": comment["html_url"],
            "question": "Which compatibility target is required?",
        },
    )
    nodes = graph.load_graph(task["id"])
    refine.reconcile(task, policy, nodes, [run], 1)
    refine.reconcile(task, policy, nodes, [run], 1)
    assert len(sent) == 1 and sent[0][1] == "warn"
    assert controls.task_snapshot(task["id"])["state"] == "succeeded"


def test_notify_failure_still_settles(db, monkeypatch):
    from agent import notify as notify_module

    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")

    async def notify(*_args, **_kwargs):
        raise RuntimeError("Discord unavailable")

    monkeypatch.setattr(notify_module, "notify", notify)
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "needs-human",
            "comment_url": comment["html_url"],
            "question": "Choose one target",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["state"] == "succeeded"
    assert "refine_notify_failed" in audit_actions(db)


def test_two_failed_attempts_fail_without_touching_github(db, monkeypatch):
    task, policy = make_task()
    node = add_refine_node(task, policy)
    monkeypatch.setattr(
        refine, "github_get", lambda *_args: pytest.fail("unexpected issue write")
    )
    monkeypatch.setattr(
        refine, "github_list", lambda *_args: pytest.fail("unexpected issue write")
    )
    settle_attempt(task, "failed")
    settle_attempt(task, "failed")
    runs = graph.node_runs(task["id"])
    refine.reconcile(task, policy, [node], runs, graph.current_version(task["id"]))
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "failed"
    assert snapshot["evidence"]["state"] == "refine_failed"
    assert "refine_failed" in audit_actions(db)
