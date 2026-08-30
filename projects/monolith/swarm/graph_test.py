import json
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import swarm.graph as graph
from swarm.graph import (
    add_node,
    admit_dispatch,
    current_version,
    discard_node,
    load_graph,
    node_runs,
    record_dispatch,
    record_outcome,
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
    engine = create_engine(f"sqlite:///{tmp_path / 'swarm-graph.db'}")
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(graph, "get_engine", lambda: engine)
    try:
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


def make_task(db, task_id="task-1", budget=10.0):
    with Session(db) as session:
        session.add(
            SwarmTask(
                id=task_id,
                task_text="test the graph",
                conductor_model="conductor-model",
                budget_usd=budget,
            )
        )
        session.commit()
    return task_id


def add_work(task_id, node_key, expected_version, **overrides):
    values = {
        "author_kind": "conductor",
        "author": "model",
        "cause_kind": "condition",
        "cause_ref": "turn-1",
        "stated_reason": "needed by the plan",
        "expected_version": expected_version,
        "node_key": node_key,
        "kind": "work",
        "prompt": f"do {node_key}",
        "model": "worker-model",
        "deps": [],
        "max_cost_usd": 1.0,
        "side_effects": False,
        "max_attempts": 2,
        "turn_timeout_seconds": 60,
    }
    values.update(overrides)
    return add_node(task_id, **values)


def discard(task_id, node_key, expected_version, **overrides):
    values = {
        "author_kind": "conductor",
        "author": "model",
        "cause_kind": "condition",
        "cause_ref": "turn-2",
        "stated_reason": "plan changed",
        "expected_version": expected_version,
    }
    values.update(overrides)
    return discard_node(task_id, node_key, **values)


def last_call(db):
    with Session(db) as session:
        return session.exec(
            select(SwarmConductorCall).order_by(SwarmConductorCall.id.desc())
        ).first()


def assert_recorded(db, result, tool, outcome, refusal_code, before, after):
    call = last_call(db)
    assert call is not None
    assert call.tool == tool
    assert call.outcome == outcome
    assert call.refusal_code == refusal_code
    assert call.version_before == before
    assert call.version_after == after
    assert result.version == after
    assert isinstance(json.loads(call.args_json), dict)


def test_version_sequence_stale_refusal_and_duplicate_key(db):
    task_id = make_task(db)
    assert current_version(task_id) == 0

    first = add_work(task_id, "one", 0)
    assert first.ok
    assert current_version(task_id) == 1
    assert_recorded(db, first, "add_node", "applied", None, 0, 1)

    stale = add_work(task_id, "two", 0)
    assert not stale.ok and stale.refusal_code == "stale_version"
    assert_recorded(db, stale, "add_node", "refused", "stale_version", 1, 1)

    duplicate = add_work(task_id, "one", 1)
    assert not duplicate.ok and duplicate.refusal_code == "duplicate_key"
    assert current_version(task_id) == 1
    assert_recorded(db, duplicate, "add_node", "refused", "duplicate_key", 1, 1)


def test_discarded_key_readd_and_historical_load(db):
    task_id = make_task(db)
    assert add_work(task_id, "leaf", 0).ok
    removed = discard(task_id, "leaf", 1)
    assert removed.ok and removed.version == 2
    assert load_graph(task_id) == []
    historical = load_graph(task_id, 1)
    assert [node["node_key"] for node in historical] == ["leaf"]
    assert historical[0]["discarded_in_version"] == 2

    readded = add_work(task_id, "leaf", 2, prompt="replacement")
    assert readded.ok and readded.version == 3
    visible = load_graph(task_id)
    assert visible[0]["prompt"] == "replacement"
    assert visible[0]["created_in_version"] == 3
    assert visible[0]["discarded_in_version"] is None
    assert_recorded(db, readded, "add_node", "applied", None, 2, 3)


def test_unknown_dependency_and_three_node_cycle_are_refused(db):
    task_id = make_task(db)
    unknown = add_work(task_id, "one", 0, deps=["missing"])
    assert not unknown.ok and unknown.refusal_code == "unknown_dep"
    assert unknown.detail == "missing"
    assert_recorded(db, unknown, "add_node", "refused", "unknown_dep", 0, 0)

    with Session(db) as session:
        session.add(
            SwarmPlanVersion(
                task_id=task_id,
                version=1,
                op="bootstrap",
                author_kind="system",
                author="router",
                change_json="{}",
                cause_kind="user_message",
            )
        )
        session.add(
            SwarmPlanNode(
                task_id=task_id,
                node_key="one",
                kind="work",
                prompt="one",
                deps_json='["two"]',
                max_cost_usd=1.0,
                side_effects=False,
                created_in_version=1,
            )
        )
        session.add(
            SwarmPlanNode(
                task_id=task_id,
                node_key="two",
                kind="work",
                prompt="two",
                deps_json='["three"]',
                max_cost_usd=1.0,
                side_effects=False,
                created_in_version=1,
            )
        )
        session.commit()

    cycle = add_work(task_id, "three", 1, deps=["one"])
    assert not cycle.ok and cycle.refusal_code == "cycle"
    assert_recorded(db, cycle, "add_node", "refused", "cycle", 1, 1)


def test_kind_and_budget_admission_rules(db):
    task_id = make_task(db, budget=1.0)
    invalid = add_work(task_id, "bad", 0, kind="conversation")
    assert not invalid.ok and invalid.refusal_code == "invalid_kind"
    assert_recorded(db, invalid, "add_node", "refused", "invalid_kind", 0, 0)

    assert add_work(task_id, "first", 0, max_cost_usd=0.4).ok
    boundary = add_work(task_id, "second", 1, max_cost_usd=0.6)
    assert boundary.ok
    exceeded = add_work(task_id, "third", 2, max_cost_usd=0.01)
    assert not exceeded.ok and exceeded.refusal_code == "budget_exceeded"
    assert_recorded(db, exceeded, "add_node", "refused", "budget_exceeded", 2, 2)

    unbudgeted_id = make_task(db, "unbudgeted", budget=None)
    unbudgeted = add_work(unbudgeted_id, "ordinary", 0, max_cost_usd=100.0)
    assert unbudgeted.ok and unbudgeted.detail == "unbudgeted run"
    fable = add_work(
        unbudgeted_id,
        "fable",
        1,
        kind="fable_escalation",
        max_cost_usd=1.0,
    )
    assert not fable.ok and fable.refusal_code == "fable_requires_budget"
    assert_recorded(db, fable, "add_node", "refused", "fable_requires_budget", 1, 1)


def test_fable_cap_counts_discarded_escalation(db):
    task_id = make_task(db)
    first = add_work(task_id, "fable-one", 0, kind="fable_escalation")
    assert first.ok
    assert discard(task_id, "fable-one", 1).ok
    second = add_work(task_id, "fable-two", 2, kind="fable_escalation")
    assert not second.ok and second.refusal_code == "fable_cap"
    assert_recorded(db, second, "add_node", "refused", "fable_cap", 2, 2)


def test_discard_unknown_stale_armed_and_ledger_refusals(db):
    task_id = make_task(db)
    assert add_work(task_id, "node", 0).ok

    stale = discard(task_id, "node", 0)
    assert not stale.ok and stale.refusal_code == "stale_version"
    assert_recorded(db, stale, "discard_node", "refused", "stale_version", 1, 1)
    unknown = discard(task_id, "missing", 1)
    assert not unknown.ok and unknown.refusal_code == "unknown_node"
    assert_recorded(db, unknown, "discard_node", "refused", "unknown_node", 1, 1)

    with Session(db) as session:
        node = session.exec(
            select(SwarmPlanNode).where(SwarmPlanNode.node_key == "node")
        ).one()
        node.armed_at = datetime.now(timezone.utc)
        session.add(node)
        session.commit()
    armed = discard(task_id, "node", 1)
    assert not armed.ok and armed.refusal_code == "armed"
    assert_recorded(db, armed, "discard_node", "refused", "armed", 1, 1)

    ledger_id = make_task(db, "ledger-armed")
    assert add_work(ledger_id, "node", 0).ok
    with Session(db) as session:
        session.add(
            SwarmNodeRun(
                task_id=ledger_id, node_key="node", attempt=1, status="admitted"
            )
        )
        session.commit()
    ledger_armed = discard(ledger_id, "node", 1)
    assert not ledger_armed.ok and ledger_armed.refusal_code == "armed"
    assert_recorded(db, ledger_armed, "discard_node", "refused", "armed", 1, 1)


def test_discard_branch_write_claim_and_matching_evidence(db):
    task_id = make_task(db)
    assert add_work(task_id, "node", 0).ok
    with Session(db) as session:
        node = session.exec(select(SwarmPlanNode)).one()
        node.base_artifact_sha = "base"
        session.add(node)
        session.commit()

    moved = discard(task_id, "node", 1, observed_branch_head="different")
    assert not moved.ok and moved.refusal_code == "branch_moved"
    assert_recorded(db, moved, "discard_node", "refused", "branch_moved", 1, 1)
    claimed = discard(
        task_id,
        "node",
        1,
        observed_branch_head="base",
        activities_claim_write=True,
    )
    assert not claimed.ok and claimed.refusal_code == "write_claimed"
    assert_recorded(db, claimed, "discard_node", "refused", "write_claimed", 1, 1)
    permitted = discard(
        task_id,
        "node",
        1,
        observed_branch_head="base",
        activities_claim_write=False,
    )
    assert permitted.ok and permitted.version == 2
    assert_recorded(db, permitted, "discard_node", "applied", None, 1, 2)


def test_discard_refuses_live_dependents(db):
    task_id = make_task(db)
    assert add_work(task_id, "parent", 0).ok
    assert add_work(task_id, "child", 1, deps=["parent"]).ok
    result = discard(task_id, "parent", 2)
    assert not result.ok and result.refusal_code == "dependents"
    assert_recorded(db, result, "discard_node", "refused", "dependents", 2, 2)


def test_admit_attempt_numbering_bound_and_armed_stamp(db):
    task_id = make_task(db)
    assert add_work(task_id, "node", 0, max_cost_usd=10.0).ok
    first = admit_dispatch(task_id, "node")
    assert first.ok and first.detail == "1"
    assert_recorded(db, first, "admit_dispatch", "applied", None, 1, 1)
    assert load_graph(task_id)[0]["armed_at"] is not None
    assert record_outcome(task_id, "node", 1, "failed", 1.0, None, "{}").ok

    second = admit_dispatch(task_id, "node")
    assert second.ok and second.detail == "2"
    exhausted = admit_dispatch(task_id, "node")
    assert not exhausted.ok and exhausted.refusal_code == "attempts_exhausted"
    assert_recorded(
        db,
        exhausted,
        "admit_dispatch",
        "refused",
        "attempts_exhausted",
        1,
        1,
    )
    assert [run["attempt"] for run in node_runs(task_id, "node")] == [1, 2]


def test_admit_unknown_and_node_budget_exhaustion(db):
    task_id = make_task(db)
    missing = admit_dispatch(task_id, "missing")
    assert not missing.ok and missing.refusal_code == "unknown_node"
    assert_recorded(db, missing, "admit_dispatch", "refused", "unknown_node", 0, 0)

    assert add_work(task_id, "node", 0, max_cost_usd=1.0).ok
    assert admit_dispatch(task_id, "node").ok
    assert record_outcome(task_id, "node", 1, "failed", 1.0, None, None).ok
    exhausted = admit_dispatch(task_id, "node")
    assert not exhausted.ok and exhausted.refusal_code == "node_budget_exhausted"
    assert_recorded(
        db,
        exhausted,
        "admit_dispatch",
        "refused",
        "node_budget_exhausted",
        1,
        1,
    )


def test_dispatch_and_outcome_round_trip_records_calls(db):
    task_id = make_task(db)
    assert add_work(task_id, "node", 0).ok
    assert admit_dispatch(task_id, "node").ok
    dispatched = record_dispatch(task_id, "node", 1, 42, "base-sha")
    assert dispatched.ok
    assert_recorded(db, dispatched, "record_dispatch", "applied", None, 1, 1)
    finished = record_outcome(
        task_id,
        "node",
        1,
        "succeeded",
        0.25,
        "head-sha",
        '{"summary":"done"}',
    )
    assert finished.ok
    assert_recorded(db, finished, "record_outcome", "applied", None, 1, 1)

    run = node_runs(task_id)[0]
    assert run["status"] == "succeeded"
    assert run["session_id"] == 42
    assert run["base_sha"] == "base-sha"
    assert run["head_sha"] == "head-sha"
    assert run["cost_usd"] == 0.25
    assert run["outcome_json"] == '{"summary":"done"}'
    assert run["finished_at"] is not None
