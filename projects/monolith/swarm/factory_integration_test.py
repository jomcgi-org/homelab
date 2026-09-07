"""Factory composition against real durable ledgers and simulated node receipts.

The DBOS transport below supplies already completed typed node results. These
checks prove server admission/reconciliation, not guest execution or live CI.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from swarm import factory_conductor as conductor
from swarm import factory_controls as controls
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

HEAD = "a" * 40


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'factory-integration.db'}",
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
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (conductor, controls, graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def policy():
    return {
        "repo": "owner/repo",
        "issue_numbers": [7],
        "generation": 0,
        "max_tasks": 1,
        "max_turns_per_task": 12,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 2,
    }


def admit(policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        "owner/repo",
        7,
        "Deliver the bounded fix",
        "Issue text is input, not authority.",
        "https://github.com/owner/repo/issues/7",
        "poller",
    )
    result = admit_next("scheduler")
    assert result["ok"] and result["task_id"].startswith("t-")
    return result["task_id"]


class CompletedNodes:
    """A fake durable workflow lookup with one completion per exact workflow ID."""

    def __init__(self):
        self.results = {}
        self.started_pins = []

    def retrieve_workflow(self, key):
        assert key in self.results, "DBOS retrieve_workflow rejects unknown IDs"
        return SimpleNamespace(
            get_status=lambda: (
                SimpleNamespace(status="SUCCESS") if key in self.results else None
            ),
            get_result=lambda: copy.deepcopy(self.results[key]),
        )

    def get_workflow_status(self, key):
        return SimpleNamespace(status="SUCCESS") if key in self.results else None

    def start_workflow(self, _function, pin):
        # Exercise the real pure node pin validator, including string task IDs.
        from swarm.node_workflows import _validate_pin

        _validate_pin(pin)
        key = pin["workflow_id"]
        assert key not in self.results, (
            "reconciliation dispatched an already completed workflow"
        )
        self.started_pins.append(copy.deepcopy(pin))
        node = pin["node_key"]
        if node == "conductor_1":
            value = {
                "action": "add_node",
                "reason": "Implement the issue",
                "node_key": "fix",
                "role": "implement",
                "prompt": "Implement the bounded fix and deliver PR 21",
                "deps": [],
            }
        elif node == "implement_fix":
            value = {
                "status": "complete",
                "summary": "Fix delivered",
                "pr_number": 21,
                "head_sha": HEAD,
            }
        elif node == "conductor_2":
            value = {
                "action": "add_node",
                "reason": "Review current head",
                "node_key": "check",
                "role": "review",
                "prompt": "Independently review PR 21 at its current head",
                "deps": ["implement_fix"],
            }
        elif node == "review_check":
            value = {
                "verdict": "approve",
                "summary": "Reviewed the exact fix",
                "pr_number": 21,
                "head_sha": HEAD,
            }
        elif node == "conductor_3":
            value = {
                "action": "finish",
                "reason": "Independent review and required CI passed",
                "pr_number": 21,
            }
        else:
            raise AssertionError(f"unexpected workflow node {node}")
        self.results[key] = {
            "status": "succeeded",
            "session_id": 100 + len(self.started_pins),
            "attempt": pin["attempt"],
            "cost_usd": 0.25,
            "head_sha": HEAD,
            "artifact": {"status": "ok", "value": value, "errors": []},
            "value": value,
            "reason": None,
            "cleanup": {"status": "completed"},
        }


def delivery_api(monkeypatch, task_id):
    def get(repo, suffix):
        assert repo == "owner/repo"
        if suffix.startswith("git/ref/heads/"):
            return {"object": {"sha": HEAD}}
        if suffix == "pulls/21":
            return {
                "state": "open",
                "draft": False,
                "head": {
                    "sha": HEAD,
                    "ref": f"factory/{task_id}",
                    "repo": {"full_name": repo},
                },
                "base": {"ref": "main"},
                "html_url": "https://github.com/owner/repo/pull/21",
            }
        if suffix == f"commits/{HEAD}/status":
            return {
                "state": "success",
                "statuses": [{"context": "pr-checks", "state": "success"}],
            }
        raise AssertionError(f"unexpected GitHub read {suffix}")

    monkeypatch.setattr(conductor, "github_get", get)


def reconcile_until(task_id, policy, dbos, condition, *, max_ticks=40):
    for _ in range(max_ticks):
        if condition():
            return
        conductor.reconcile_task(task_id, policy, dbos)
    assert condition(), (
        "bounded reconciliation did not reach the expected durable state"
    )


def test_real_receipt_graph_controls_complete_string_task_plan_work_review_pr(
    db, policy, monkeypatch
):
    task_id = admit(policy)
    dbos = CompletedNodes()
    delivery_api(monkeypatch, task_id)
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: controls.task_snapshot(task_id)["state"] == "succeeded",
    )
    assert [p["node_key"] for p in dbos.started_pins] == [
        "conductor_1",
        "implement_fix",
        "conductor_2",
        "review_check",
        "conductor_3",
    ]
    assert all(p["task_id"] == task_id for p in dbos.started_pins)
    assert {
        p["model"]
        for p in dbos.started_pins
        if p["node_key"].startswith("conductor_") or p["node_key"].startswith("review_")
    } == {"opus"}
    assert (
        next(p for p in dbos.started_pins if p["node_key"] == "implement_fix")["model"]
        == "luna"
    )
    assert len({r["session_id"] for r in dbos.results.values()}) == 5
    snapshot = controls.task_snapshot(task_id)
    assert snapshot["evidence"] == {
        "pr_url": "https://github.com/owner/repo/pull/21",
        "head_sha": HEAD,
        "review_session_id": 104,
        "state": "ready_for_review",
    }
    assert snapshot["turns_used"] == 5 and snapshot["unresolved_starts"] == 0
    assert snapshot["committed_cost_usd"] == 1.25
    assert all(r["status"] == "succeeded" for r in graph.node_runs(task_id))
    db.dispose()
    assert controls.status()["active_tasks"] == []
    duplicate = receive_issue(
        "owner/repo",
        7,
        "changed",
        "replace task",
        "https://github.com/owner/repo/issues/7",
        "poller",
    )
    assert not duplicate["created"] and duplicate["receipt"]["task_id"] == task_id
    assert len(dbos.started_pins) == 5


def test_restart_retries_atomic_outcome_settlement_without_redispatch(
    db, policy, monkeypatch
):
    task_id = admit(policy)
    dbos = CompletedNodes()
    reconcile_until(task_id, policy, dbos, lambda: len(dbos.started_pins) == 1)
    original = controls.record_start_outcome

    def crash(*_args, **_kwargs):
        raise RuntimeError("simulated loss during atomic graph settlement")

    monkeypatch.setattr(controls, "record_start_outcome", crash)
    with pytest.raises(RuntimeError, match="simulated loss"):
        conductor.reconcile_task(task_id, policy, dbos)
    assert graph.node_runs(task_id)[0]["status"] == "admitted"
    assert controls.task_snapshot(task_id)["starts"][0]["status"] == "reserved"
    monkeypatch.setattr(controls, "record_start_outcome", original)
    db.dispose()
    conductor.reconcile_task(task_id, policy, dbos)
    assert controls.task_snapshot(task_id)["starts"][0]["status"] == "succeeded"
    assert controls.task_snapshot(task_id)["committed_cost_usd"] == 0.25
    assert len(dbos.started_pins) == 1


def test_real_factory_turn_rejection_rolls_back_graph_dispatch_and_arming(db, policy):
    policy["max_turns_per_task"] = 1
    task_id = admit(policy)
    dbos = CompletedNodes()
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: any(
            n["node_key"] == "implement_fix" for n in graph.load_graph(task_id)
        ),
    )
    assert controls.task_snapshot(task_id)["turns_used"] == 1

    # The graph has cost/attempt room. The distinct factory turn cap must reject
    # the composed reservation and roll back the graph's inserted/armed run.
    before = graph.node_runs(task_id)
    conductor.reconcile_task(task_id, policy, dbos)
    assert graph.node_runs(task_id) == before
    work = next(
        n for n in graph.load_graph(task_id) if n["node_key"] == "implement_fix"
    )
    assert work["armed_at"] is None
    snapshot = controls.task_snapshot(task_id)
    assert snapshot["turns_used"] == 1 and snapshot["task_paused"]
    assert len(dbos.started_pins) == 1


def test_late_confirmed_result_settles_unknown_identity_without_new_vm(
    db, policy, monkeypatch
):
    from swarm import node_workflows

    task_id = admit(policy)
    dbos = CompletedNodes()
    reconcile_until(task_id, policy, dbos, lambda: len(dbos.started_pins) == 1)
    key = dbos.started_pins[0]["workflow_id"]
    completed = copy.deepcopy(dbos.results[key])
    dbos.results[key] = {
        "status": "uncertain",
        "session_id": None,
        "cost_usd": None,
        "reason": "submit observation lost",
    }
    observed = {"result": None}
    monkeypatch.setattr(
        node_workflows,
        "reconcile_completed_node",
        lambda pin, identity: observed["result"],
    )
    conductor.reconcile_task(task_id, policy, dbos)
    assert graph.node_runs(task_id)[0]["status"] == "uncertain"
    assert controls.task_snapshot(task_id)["state"] == "uncertain"
    observed["result"] = completed
    conductor.reconcile_task(task_id, policy, dbos)
    run = graph.node_runs(task_id)[0]
    assert run["status"] == "succeeded" and run["session_id"] == completed["session_id"]
    assert controls.task_snapshot(task_id)["unresolved_starts"] == 0
    assert len(dbos.started_pins) == 1
