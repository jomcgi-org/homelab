"""Factory composition against real durable ledgers and simulated node receipts.

The DBOS transport below supplies already completed typed node results. These
checks prove server admission/reconciliation, not guest execution or live CI.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
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
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = (
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
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

    def github_branch(repo, suffix):
        with Session(engine) as session:
            expected = {
                (task.repo, f"git/ref/heads/{quote('factory/' + task.id, safe='')}")
                for task in session.exec(select(SwarmTask)).all()
            }
        assert (repo, suffix) in expected, f"unexpected GitHub read {repo}/{suffix}"
        return {"object": {"sha": HEAD}}

    monkeypatch.setattr(conductor, "github_get", github_branch)
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


def test_autonomous_intake_receipt_flows_through_admission(db, policy, monkeypatch):
    from swarm import factory_intake_loop

    policy["intake"] = {"enabled": True}
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    opened = {
        "number": 8,
        "title": "Ready issue",
        "body": "bounded",
        "html_url": "https://github.com/owner/repo/issues/8",
        "state": "open",
        "assignees": [],
        "labels": [{"name": "agent-ready"}],
        "created_at": "2026-09-10T00:00:00Z",
    }
    monkeypatch.setattr(
        factory_intake_loop,
        "github_list",
        lambda _repo, suffix: [] if suffix.startswith("pulls?") else [opened],
    )
    received = factory_intake_loop.intake_tick(policy, generation=0)
    assert received["receipt"]["kind"] == "deliver"
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    assert admitted["receipt"]["issue_number"] == 8


def test_refine_intake_reconciles_one_node_and_verifies_settlement(
    db, policy, monkeypatch
):
    from datetime import datetime, timedelta
    import json
    from swarm import factory_intake_loop, factory_refine

    policy["intake"] = {"enabled": True, "refine_enabled": True}
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    opened = {
        "number": 8,
        "title": "Needs a brief",
        "body": "bounded",
        "html_url": "https://github.com/owner/repo/issues/8",
        "state": "open",
        "assignees": [],
        "labels": [],
        "created_at": "2026-09-10T00:00:00Z",
    }
    monkeypatch.setattr(
        factory_intake_loop,
        "github_list",
        lambda _repo, suffix: [] if suffix.startswith("pulls?") else [opened],
    )
    assert (
        factory_intake_loop.intake_tick(policy, generation=0)["receipt"]["kind"]
        == "refine"
    )
    admitted = admit_next("scheduler")
    task_id = admitted["task_id"]
    task = conductor._task(task_id)
    conductor.reconcile_task(task_id, admitted["policy"], SimpleNamespace())
    nodes = graph.load_graph(task_id)
    assert [node["node_key"] for node in nodes] == ["refine_1"]
    key = f"factory-node:{task_id}:refine_1:1"
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task_id}",
        "workflow_id": key,
        "artifact_path": ".factory/refine_1.json",
        "artifact_schema": factory_refine.REFINE_SCHEMA,
        "hydration_branch": "main",
        "retry_context": "[]",
    }
    assert conductor.reserve_node(task_id, "refine_1", key, context)
    assert graph.record_dispatch(task_id, "refine_1", 1, 101, None).ok
    comment_url = "https://github.com/owner/repo/issues/8#issuecomment-1"
    value = {"outcome": "agent-ready", "comment_url": comment_url}
    assert graph.record_outcome(
        task_id,
        "refine_1",
        1,
        "succeeded",
        0.1,
        None,
        json.dumps({"value": value, "cost_usd": 0.1, "session_id": 101}),
    ).ok
    assert controls.record_start_outcome(
        task_id, key, "succeeded", "worker", cost_usd=0.1, session_id=101
    )["ok"]
    admitted_at = datetime.fromisoformat(controls.task_snapshot(task_id)["admitted_at"])
    monkeypatch.setattr(
        factory_refine,
        "github_get",
        lambda *_args: {"labels": [{"name": "agent-ready"}]},
    )
    monkeypatch.setattr(
        factory_refine,
        "github_list",
        lambda *_args: [
            {
                "body": "## Agent brief\n### Outcome\nReady",
                "created_at": (admitted_at + timedelta(seconds=1)).isoformat(),
                "html_url": comment_url,
                "user": {"login": "factory-bot"},
            }
        ],
    )
    conductor.reconcile_task(task_id, admitted["policy"], SimpleNamespace())
    assert controls.task_snapshot(task_id)["evidence"]["state"] == (
        "refine_agent_ready"
    )


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


def delivery_api(monkeypatch, task_id, *, branches=None):
    """GitHub reads for the task branch, its PR and its checks.

    ``branches`` names the extra refs that exist, so a first fan-out attempt
    reads a 404 for its own branch exactly as it would in the repository.
    """

    def get(repo, suffix):
        assert repo == "owner/repo"
        if suffix.startswith("git/ref/heads/"):
            branch = suffix.removeprefix("git/ref/heads/").replace("%2F", "/")
            if branch == f"factory/{task_id}" or branch in (branches or ()):
                return {"object": {"sha": HEAD}}
            response = httpx.Response(
                404, request=httpx.Request("GET", "https://example.test/ref")
            )
            response.raise_for_status()
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
    # Three of the five starts were conductor planning rounds. Only the two
    # work starts count against max_turns_per_task; all five cost money.
    assert snapshot["turns_used"] == 2 and snapshot["planner_turns_used"] == 3
    assert snapshot["unresolved_starts"] == 0
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
    policy["max_planner_turns"] = 4
    task_id = admit(policy)
    dbos = CompletedNodes()
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: any(n["node_key"] == "review_check" for n in graph.load_graph(task_id)),
    )
    used = controls.task_snapshot(task_id)
    assert used["turns_used"] == 1 and used["planner_turns_used"] == 2

    # A crash between authorizing a start and launching its workflow leaves the
    # reservation held. The graph has cost and attempt room, so the distinct
    # factory start fence must reject the composed reservation and roll back the
    # graph's inserted and armed run.
    with Session(db) as session:
        session.add(
            FactoryStart(
                task_id=task_id,
                start_key=f"factory-node:{task_id}:conductor_9:1",
                actor="crashed-reconciler",
                model="luna",
                max_cost_usd=1.0,
            )
        )
        session.commit()
    before = graph.node_runs(task_id)
    conductor.reconcile_task(task_id, policy, dbos)
    assert graph.node_runs(task_id) == before
    work = next(n for n in graph.load_graph(task_id) if n["node_key"] == "review_check")
    assert work["armed_at"] is None
    snapshot = controls.task_snapshot(task_id)
    assert snapshot["turns_used"] == 1 and snapshot["task_paused"]
    assert len(dbos.started_pins) == 3


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
    with Session(db) as session:
        # The real typed-proof reader acquired its pool lock and found no
        # session evidence. It must not fabricate a turn to resolve the hold.
        assert session.get(AgentCapacityPool, 1) is not None
        assert session.exec(select(AgentTurn)).first() is None
    observed["result"] = completed
    conductor.reconcile_task(task_id, policy, dbos)
    run = graph.node_runs(task_id)[0]
    assert run["status"] == "succeeded" and run["session_id"] == completed["session_id"]
    assert controls.task_snapshot(task_id)["unresolved_starts"] == 0
    assert len(dbos.started_pins) == 1


class PlannedThenCorrected(CompletedNodes):
    """One batched plan, one review that asks for changes, one engine round."""

    def start_workflow(self, _function, pin):
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
                "action": "plan",
                "reason": "Implement then independently review the issue",
                "edits": [
                    {
                        "action": "add_node",
                        "reason": "Deliver the bounded fix",
                        "node_key": "fix",
                        "role": "implement",
                        "prompt": "Implement the bounded fix and deliver PR 21",
                        "deps": [],
                    },
                    {
                        "action": "add_node",
                        "reason": "Independent review at the exact head",
                        "node_key": "check",
                        "role": "review",
                        "prompt": "Independently review PR 21 at its current head",
                        "deps": ["implement_fix"],
                    },
                ],
            }
        elif node in ("implement_fix", "correct_1"):
            value = {
                "status": "complete",
                "summary": f"{node} delivered",
                "pr_number": 21,
                "head_sha": HEAD,
            }
        elif node == "review_check":
            value = {
                "verdict": "changes_requested",
                "summary": "Bound the retry and add the regression test.",
                "pr_number": 21,
                "head_sha": HEAD,
            }
        elif node == "review_1":
            value = {
                "verdict": "approve",
                "summary": "The correction covers every finding.",
                "pr_number": 21,
                "head_sha": HEAD,
            }
        elif node == "conductor_2":
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


def test_a_plan_time_dag_runs_its_review_loop_without_a_planner_turn(
    db, policy, monkeypatch
):
    task_id = admit(policy)
    dbos = PlannedThenCorrected()
    delivery_api(monkeypatch, task_id)
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: controls.task_snapshot(task_id)["state"] == "succeeded",
    )
    # Two planner turns for five work nodes: one plan and one finish. The
    # correction round between them is the engine's own graph edit.
    assert [p["node_key"] for p in dbos.started_pins] == [
        "conductor_1",
        "implement_fix",
        "review_check",
        "correct_1",
        "review_1",
        "conductor_2",
    ]
    assert (
        next(p for p in dbos.started_pins if p["node_key"] == "correct_1")["model"]
        == "luna"
    )
    assert (
        next(p for p in dbos.started_pins if p["node_key"] == "review_1")["model"]
        == "opus"
    )
    correction = next(p for p in dbos.started_pins if p["node_key"] == "correct_1")
    assert "Bound the retry and add the regression test." in correction["prompt"]
    assert HEAD in correction["prompt"]
    nodes = {n["node_key"]: n for n in graph.load_graph(task_id)}
    assert nodes["correct_1"]["deps"] == ["review_check"]
    assert nodes["review_1"]["deps"] == ["correct_1"]
    with Session(db) as session:
        loop = session.exec(
            select(SwarmPlanVersion).where(
                SwarmPlanVersion.cause_kind == "factory_loop"
            )
        ).all()
        assert [v.cause_ref for v in loop] == ["factory-loop:review_1"] * 2
        assert {v.author_kind for v in loop} == {"engine"}
        # The plan's two edits are one atomic conductor call, chained in order.
        planned = session.exec(
            select(SwarmPlanVersion).where(
                SwarmPlanVersion.cause_ref == "factory-decision:conductor_1:1"
            )
        ).all()
        assert [v.version for v in planned] == [2, 3]
    snapshot = controls.task_snapshot(task_id)
    # Four work turns and two planner turns: the plan and the finish. The
    # correction round between them is a graph edit, not a start.
    assert snapshot["turns_used"] == 4 and snapshot["planner_turns_used"] == 2
    assert snapshot["unresolved_starts"] == 0
    assert snapshot["evidence"]["head_sha"] == HEAD
    assert snapshot["evidence"]["state"] == "ready_for_review"


def test_the_planner_is_not_called_while_a_review_round_is_open(
    db, policy, monkeypatch
):
    task_id = admit(policy)
    dbos = PlannedThenCorrected()
    delivery_api(monkeypatch, task_id)
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: any(
            node["node_key"] == "correct_1" for node in graph.load_graph(task_id)
        ),
    )
    assert [p["node_key"] for p in dbos.started_pins] == [
        "conductor_1",
        "implement_fix",
        "review_check",
    ]
    assert not any(
        node["node_key"] == "conductor_2" for node in graph.load_graph(task_id)
    )


def test_exhausted_review_rounds_return_the_task_to_the_planner(
    db, policy, monkeypatch
):
    policy["max_review_rounds"] = 0
    task_id = admit(policy)

    class NeverSatisfied(PlannedThenCorrected):
        def start_workflow(self, function, pin):
            if pin["node_key"] == "conductor_2":
                self.started_pins.append(copy.deepcopy(pin))
                self.results[pin["workflow_id"]] = {
                    "status": "succeeded",
                    "session_id": 900,
                    "attempt": pin["attempt"],
                    "cost_usd": 0.25,
                    "head_sha": HEAD,
                    "artifact": {"status": "ok", "value": {}, "errors": []},
                    "value": {"action": "pause", "reason": "review is unresolved"},
                    "reason": None,
                    "cleanup": {"status": "completed"},
                }
                return
            super().start_workflow(function, pin)

    dbos = NeverSatisfied()
    delivery_api(monkeypatch, task_id)
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: controls.task_snapshot(task_id)["task_paused"],
    )
    assert [p["node_key"] for p in dbos.started_pins] == [
        "conductor_1",
        "implement_fix",
        "review_check",
        "conductor_2",
    ]
    assert not any(
        node["node_key"].startswith("correct_") for node in graph.load_graph(task_id)
    )
    planner = next(
        node for node in graph.load_graph(task_id) if node["node_key"] == "conductor_2"
    )
    import json

    deviation = json.loads(planner["prompt"].rsplit("\n", 1)[1])["deviation"]
    assert deviation["code"] == "review_rounds_exhausted"


class PlannedInParallel(CompletedNodes):
    """A two-branch plan, fanned in by the engine before an independent review."""

    def start_workflow(self, _function, pin):
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
                "action": "plan",
                "reason": "Two independent pieces, then one review",
                "edits": [
                    {
                        "action": "add_node",
                        "reason": "Deliver the server half",
                        "node_key": "alpha",
                        "role": "implement",
                        "prompt": "Implement the server half",
                        "deps": [],
                    },
                    {
                        "action": "add_node",
                        "reason": "Deliver the client half",
                        "node_key": "beta",
                        "role": "implement",
                        "prompt": "Implement the client half",
                        "deps": [],
                    },
                    {
                        "action": "add_node",
                        "reason": "Independent review at the exact head",
                        "node_key": "check",
                        "role": "review",
                        "prompt": "Independently review PR 21 at its current head",
                        "deps": ["implement_alpha", "implement_beta"],
                    },
                ],
            }
        elif node in ("implement_alpha", "implement_beta", "integrate_1"):
            value = {
                "status": "complete",
                "summary": f"{node} delivered",
                "pr_number": 21,
                "head_sha": HEAD,
            }
        elif node == "review_check":
            value = {
                "verdict": "approve",
                "summary": "The integrated head covers both halves.",
                "pr_number": 21,
                "head_sha": HEAD,
            }
        elif node == "conductor_2":
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


def test_parallel_halves_fan_out_and_are_integrated_before_review(
    db, policy, monkeypatch
):
    policy["max_parallel_nodes"] = 2
    task_id = admit(policy)
    dbos = PlannedInParallel()
    delivery_api(monkeypatch, task_id)
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: controls.task_snapshot(task_id)["state"] == "succeeded",
    )
    started = [pin["node_key"] for pin in dbos.started_pins]
    assert started[0] == "conductor_1"
    # Both halves are in flight before either settles, and the engine's fan-in
    # runs before the review the planner wrote.
    assert set(started[1:3]) == {"implement_alpha", "implement_beta"}
    assert started[3:] == ["integrate_1", "review_check", "conductor_2"]
    branches = {pin["node_key"]: pin["branch"] for pin in dbos.started_pins}
    assert branches["implement_alpha"] == f"factory/{task_id}-implement_alpha"
    assert branches["implement_beta"] == f"factory/{task_id}-implement_beta"
    assert branches["integrate_1"] == f"factory/{task_id}"
    assert branches["review_check"] == f"factory/{task_id}"
    nodes = {n["node_key"]: n for n in graph.load_graph(task_id)}
    assert nodes["integrate_1"]["deps"] == ["implement_alpha", "implement_beta"]
    assert nodes["review_check"]["deps"] == ["integrate_1"]
    with Session(db) as session:
        fan_in = session.exec(
            select(SwarmPlanVersion).where(
                SwarmPlanVersion.cause_ref == "factory-fanin:integrate_1"
            )
        ).all()
        # One add for the fan-in node, then the review's discard and re-add.
        assert [v.op for v in fan_in] == ["add_node", "discard_node", "add_node"]
        assert {v.author_kind for v in fan_in} == {"engine"}
        # Its own cause kind, so the fan-in never spends a review round.
        assert {v.cause_kind for v in fan_in} == {"factory_fanin"}
    snapshot = controls.task_snapshot(task_id)
    assert snapshot["turns_used"] == 4 and snapshot["planner_turns_used"] == 2
    assert snapshot["allowance"]["derived"] is True
    assert snapshot["evidence"]["state"] == "ready_for_review"


def test_the_serial_lane_keeps_the_task_branch_and_needs_no_fan_in(
    db, policy, monkeypatch
):
    assert "max_parallel_nodes" not in policy
    task_id = admit(policy)
    dbos = PlannedThenCorrected()
    delivery_api(monkeypatch, task_id)
    monkeypatch.setattr(
        conductor,
        "_free_background_slots",
        lambda: pytest.fail("the serial lane must not read the shared pool"),
    )
    reconcile_until(
        task_id,
        policy,
        dbos,
        lambda: controls.task_snapshot(task_id)["state"] == "succeeded",
    )
    assert all(pin["branch"] == f"factory/{task_id}" for pin in dbos.started_pins)
    assert not any(
        node["node_key"].startswith("integrate_") for node in graph.load_graph(task_id)
    )
