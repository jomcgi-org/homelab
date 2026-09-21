"""File-backed regressions for aged factory starts that made no session."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from threading import Event, Thread

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_conductor as conductor


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path, monkeypatch):
    import core.db
    from factory.execution import admission, reconciliation
    from factory.execution.models import (
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from factory.orchestration import factory_controls as controls
    from factory.orchestration import work_items
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

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sessionless-start.db'}",
        connect_args={"timeout": 5},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
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
        AgentSession,
        AgentTurn,
        PendingMessage,
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentResultReceipt,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as db:
        db.add(FactoryControl(id="factory", actor="migration"))
        db.commit()
    modules = (conductor, conductor.graph, controls, admission, core.db, work_items)
    for module in modules:
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(reconciliation, "_utcnow", lambda: NOW)
    monkeypatch.setattr(conductor, "github_list", lambda *_args: [])
    monkeypatch.setattr(conductor, "hydration_branch", lambda _task: "main")
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setenv("FACTORY_BACKGROUND_RESERVE", "0")
    yield engine
    engine.dispose()


def _policy():
    return {
        "repo": "owner/repo",
        "issue_numbers": [6285],
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 0},
        "max_turns_per_task": 4,
        "task_budget_usd": 10.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 2,
    }


def _sessionless_attempt(engine, *, age_seconds=61):
    from factory.orchestration import factory_controls as controls
    from factory.orchestration.factory_intake import admit_next, receive_issue
    from factory.orchestration.factory_models import FactoryStart

    policy = _policy()
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        "owner/repo",
        6285,
        "Recover a sessionless start",
        "test issue",
        "https://github.com/owner/repo/issues/6285",
        "poller",
    )
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    task = conductor._task(admitted["task_id"])
    node_key = "implement_fix"
    assert conductor._add(
        task,
        policy,
        node_key,
        "Implement the bounded fix",
        [],
        "luna",
        "test:add",
        "test fixture",
    ).ok
    workflow = f"factory-node:{task['id']}:{node_key}:1"
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task['id']}",
        "hydration_branch": "main",
        "workflow_id": workflow,
        "artifact_path": ".factory/implement_fix.json",
        "artifact_schema": conductor._schema(node_key),
        "retry_context": "[]",
    }
    assert conductor.reserve_node(task["id"], node_key, workflow, context).ok
    run = conductor.graph.node_runs(task["id"])[0]
    with Session(engine) as db:
        start = db.exec(
            select(FactoryStart).where(FactoryStart.start_key == workflow)
        ).one()
        start.created_at = NOW - timedelta(seconds=age_seconds)
        db.add(start)
        db.commit()
        start_id = start.id
    return SimpleNamespace(
        task=task,
        policy=policy,
        run=run,
        workflow=workflow,
        start_id=start_id,
        engine=engine,
    )


def _rows(state):
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmNodeRun

    with Session(state.engine) as db:
        return SimpleNamespace(
            **db.get(SwarmNodeRun, state.run["id"]).model_dump()
        ), SimpleNamespace(**db.get(FactoryStart, state.start_id).model_dump())


def test_real_authorize_without_session_settles_both_ledgers_and_replays(database):
    from factory.execution.models import AgentSession
    from factory.orchestration import factory_controls as controls
    from factory.orchestration.factory_models import FactoryAudit

    state = _sessionless_attempt(database)
    with Session(database) as db:
        assert db.exec(select(AgentSession)).first() is None

    assert conductor._sweep_sessionless_starts({"task_id": state.task["id"]}) == 1
    run, start = _rows(state)
    outcome = json.loads(run.outcome_json)
    assert run.status == "failed" and run.cost_usd == 0.0
    assert outcome["reason"] == "never_dispatched"
    assert outcome["never_dispatched"]["invocation_phase"] == "never_dispatched"
    assert start.status == "failed" and start.cost_usd == 0.0
    assert start.accounting_basis == "no_model_post"
    accounting = controls.task_snapshot(state.task["id"])
    assert accounting["committed_cost_usd"] == 0.0
    assert accounting["unresolved_starts"] == 0

    assert conductor._sweep_sessionless_starts({"task_id": state.task["id"]}) == 0
    with Session(database) as db:
        audits = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "sessionless_start_settled"
            )
        ).all()
    assert len(audits) == 1


@pytest.mark.parametrize("age_seconds,settled", [(59, 0), (60, 0), (61, 1)])
def test_timeout_is_strictly_beyond_the_pinned_boundary(database, age_seconds, settled):
    state = _sessionless_attempt(database, age_seconds=age_seconds)
    assert conductor._sweep_sessionless_starts({"task_id": state.task["id"]}) == settled
    run, start = _rows(state)
    assert (run.status, start.status) == (
        ("failed", "failed") if settled else ("admitted", "reserved")
    )


def test_deterministic_session_without_ledger_bindings_refuses_sweep(database):
    from factory.execution.models import AgentSession

    state = _sessionless_attempt(database)
    pin = state.run["pin"]
    with Session(database) as db:
        db.add(
            AgentSession(
                local_session_id=(
                    f"factory:{state.task['id']}:{state.run['node_key']}:1"
                ),
                workspace="guest",
                branch=pin["hydration_branch"],
                repo=pin["repo"],
                model="luna",
                workflow_id=pin["workflow_id"],
                node_key=pin["node_key"],
                node_attempt=pin["attempt"],
                admission_tier="project",
            )
        )
        db.commit()
    assert conductor._sweep_sessionless_starts({"task_id": state.task["id"]}) == 0
    run, start = _rows(state)
    assert (run.status, start.status) == ("admitted", "reserved")


@pytest.mark.parametrize(
    "conflict", ["ownership", "dispatch", "start_session", "permit", "receipt"]
)
def test_contradictory_attempt_evidence_refuses_sweep(database, conflict):
    from factory.execution.models import AgentCapacityReservation, AgentResultReceipt
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmNodeRun

    state = _sessionless_attempt(database)
    with Session(database) as db:
        run = db.get(SwarmNodeRun, state.run["id"])
        start = db.get(FactoryStart, state.start_id)
        if conflict == "ownership":
            start.model = "opus"
            db.add(start)
        elif conflict == "dispatch":
            run.dispatch_key = "factory-node:wrong:owner:1"
            db.add(run)
        elif conflict == "start_session":
            start.session_id = 999
            db.add(start)
        elif conflict == "permit":
            db.add(
                AgentCapacityReservation(
                    local_session_id=(
                        f"factory:{state.task['id']}:{state.run['node_key']}:1"
                    ),
                    pending_seq=1,
                    tier="project",
                    model="luna",
                )
            )
        else:
            db.add(
                AgentResultReceipt(
                    id="receipt-1",
                    token_sha256="a" * 64,
                    session_id=999,
                    local_session_id=(
                        f"factory:{state.task['id']}:{state.run['node_key']}:1"
                    ),
                    seq=1,
                    dispatch_count=1,
                    claim_owner="replica:attempt",
                    guest_id="guest-1",
                    request_sha256="b" * 64,
                    created_at=NOW,
                    accept_until=NOW + timedelta(minutes=1),
                    retain_until=NOW + timedelta(days=1),
                )
            )
        db.commit()
    assert conductor._sweep_sessionless_starts({"task_id": state.task["id"]}) == 0
    run, start = _rows(state)
    assert (run.status, start.status) == ("admitted", "reserved")


def test_failed_deterministic_lookup_rolls_back_without_settlement(
    database, monkeypatch
):
    from factory.orchestration import node_workflows

    state = _sessionless_attempt(database)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("lookup unavailable")

    monkeypatch.setattr(node_workflows, "resolve_node_session_id", unavailable)
    with pytest.raises(RuntimeError, match="lookup unavailable"):
        conductor._sweep_sessionless_starts({"task_id": state.task["id"]})
    run, start = _rows(state)
    assert (run.status, start.status) == ("admitted", "reserved")


def test_atomic_rollback_when_graph_settlement_refuses(database, monkeypatch):
    state = _sessionless_attempt(database)
    monkeypatch.setattr(
        conductor.graph,
        "record_outcome",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False, refusal_code="changed_attempt"
        ),
    )
    with pytest.raises(ValueError, match="outcome_refused: changed_attempt"):
        conductor._sweep_sessionless_starts({"task_id": state.task["id"]})
    run, start = _rows(state)
    assert (run.status, start.status, start.accounting_basis) == (
        "admitted",
        "reserved",
        None,
    )


def test_late_session_binding_cannot_cross_atomic_settlement(database, monkeypatch):
    from factory.orchestration import factory_controls as controls

    state = _sessionless_attempt(database)
    reached_settlement = Event()
    release_settlement = Event()
    creator_started = Event()
    creator_done = Event()
    original = controls.record_start_outcome
    results = {}

    def paused_outcome(*args, **kwargs):
        reached_settlement.set()
        assert release_settlement.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(controls, "record_start_outcome", paused_outcome)

    def sweep():
        results["sweep"] = conductor._sweep_sessionless_starts(
            {"task_id": state.task["id"]}
        )

    def create_late():
        creator_started.set()
        try:
            with controls.start_guard(
                state.task["id"], start_key=state.workflow
            ) as admission:
                results["admission"] = admission
                conductor.graph.lock_node_session_binding(
                    state.task["id"],
                    state.run["node_key"],
                    1,
                    workflow_id=state.workflow,
                    session=controls.active_start_session(),
                )
        except Exception as exc:  # the terminal graph fence is the assertion
            results["creator_error"] = exc
        finally:
            creator_done.set()

    sweeper = Thread(target=sweep)
    sweeper.start()
    assert reached_settlement.wait(5)
    creator = Thread(target=create_late)
    creator.start()
    assert creator_started.wait(5)
    assert not creator_done.wait(0.1)
    release_settlement.set()
    sweeper.join(5)
    creator.join(5)
    assert not sweeper.is_alive() and not creator.is_alive()
    assert results["sweep"] == 1
    assert isinstance(results.get("creator_error"), ValueError)
    assert "binding conflict" in str(results["creator_error"])


def test_sweep_releases_ceiling_then_normal_reconciliation_retries_once(database):
    from factory.orchestration import factory_controls as controls

    state = _sessionless_attempt(database)
    assert conductor._sweep_sessionless_starts({"task_id": state.task["id"]}) == 1
    before = controls.task_snapshot(state.task["id"])
    assert before["committed_cost_usd"] == 0.0

    conductor.reconcile_task(state.task["id"], state.policy, SimpleNamespace())
    runs = conductor.graph.node_runs(state.task["id"], state.run["node_key"])
    assert [(run["attempt"], run["status"]) for run in runs] == [
        (1, "failed"),
        (2, "admitted"),
    ]
    after = controls.task_snapshot(state.task["id"])
    assert after["committed_cost_usd"] == pytest.approx(2.0)
    assert after["unresolved_starts"] == 1
