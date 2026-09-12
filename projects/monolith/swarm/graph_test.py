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


def test_completed_nodes_release_only_unused_plan_budget(db):
    task_id = make_task(db, budget=60.0)
    # Six completed $10 nodes from the factory pause, including three whose
    # unreported usage must still consume their entire immutable reservation.
    costs = [0.853754, None, 1.748051, None, 1.636344, None]
    for index, cost in enumerate(costs):
        key = f"step-{index}"
        assert add_work(task_id, key, index, max_cost_usd=10.0).ok
        assert admit_dispatch(task_id, key, dispatch_key=key).ok
        assert record_outcome(task_id, key, 1, "succeeded", cost, "head", "{}").ok

    history = node_runs(task_id)
    plan = load_graph(task_id)
    assert sum(run["accounted_cost_usd"] for run in history) == pytest.approx(34.238149)
    assert add_work(task_id, "next", 6, max_cost_usd=10.0).ok
    assert load_graph(task_id)[:6] == plan
    assert node_runs(task_id) == history
    with Session(db) as session:
        assert session.get(SwarmTask, task_id).budget_usd == 60.0
    for run in history:
        replay = admit_dispatch(
            task_id, run["node_key"], dispatch_key=run["dispatch_key"]
        )
        assert replay.ok and replay.pin == run["pin"]
        assert admit_dispatch(task_id, run["node_key"]).refusal_code == "node_succeeded"
    assert node_runs(task_id) == history
    assert admit_dispatch(task_id, "next").pin["max_cost_usd"] == 10.0


@pytest.mark.parametrize(
    "status",
    [None, "admitted", "dispatched", "uncertain", "failed", "cancelled", "escalated"],
)
def test_unfinished_or_retryable_nodes_keep_remaining_plan_budget(db, status):
    task_id = make_task(db, budget=5.0)
    assert add_work(task_id, "one", 0, max_cost_usd=4.0).ok
    if status is not None:
        assert admit_dispatch(task_id, "one").ok
        if status == "dispatched":
            assert record_dispatch(task_id, "one", 1, 42, "base").ok
        elif status != "admitted":
            assert record_outcome(task_id, "one", 1, status, 1.0, None, "{}").ok
    history = node_runs(task_id)
    assert (
        add_work(task_id, "too-large", 1, max_cost_usd=1.01).refusal_code
        == "budget_exceeded"
    )
    assert add_work(task_id, "fits", 1).ok
    assert node_runs(task_id) == history
    if status in ("failed", "cancelled", "escalated"):
        assert admit_dispatch(task_id, "one").pin["max_cost_usd"] == 3.0


@pytest.mark.parametrize("final_cost,remaining", [(0.25, 0.25), (None, 0.0)])
def test_successful_retry_keeps_spend_from_every_attempt(db, final_cost, remaining):
    task_id = make_task(db, budget=1.0)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 1, "failed", 0.5, None, "{}").ok
    assert admit_dispatch(task_id, "one").pin["max_cost_usd"] == 0.5
    assert record_outcome(task_id, "one", 2, "succeeded", final_cost, "head", "{}").ok
    history = node_runs(task_id)
    assert (
        add_work(task_id, "too-large", 1, max_cost_usd=remaining + 0.01).refusal_code
        == "budget_exceeded"
    )
    if remaining:
        assert add_work(task_id, "fits", 1, max_cost_usd=remaining).ok
    assert node_runs(task_id) == history


@pytest.mark.parametrize("tombstone", ["discarded_in_version", "cancelled_in_version"])
@pytest.mark.parametrize(
    "status,cost,accounted",
    [("failed", 0.75, 0.75), ("succeeded", None, 1.0), ("uncertain", 0.25, 1.0)],
)
def test_hidden_node_history_remains_in_plan_budget(
    db, tombstone, status, cost, accounted
):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "old", 0).ok
    assert admit_dispatch(task_id, "old").ok
    assert record_outcome(task_id, "old", 1, status, cost, "head", "{}").ok
    # Ordinary discard refuses acted nodes. Model a retained historical row
    # hidden by cancellation or an earlier writer without erasing its ledger.
    with Session(db) as session:
        node = session.exec(select(SwarmPlanNode)).one()
        setattr(node, tombstone, 1)
        session.add(node)
        session.commit()
    assert load_graph(task_id) == []
    history = node_runs(task_id)
    assert (
        add_work(task_id, "too-large", 1, max_cost_usd=2.01 - accounted).refusal_code
        == "budget_exceeded"
    )
    assert add_work(task_id, "fits", 1, max_cost_usd=2.0 - accounted).ok
    assert node_runs(task_id) == history


@pytest.mark.parametrize("status", ["succeeded", "failed", "uncertain"])
def test_plan_budget_retains_observed_overrun(db, status):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 1, status, 1.5, "head", "{}").ok
    assert (
        add_work(task_id, "too-large", 1, max_cost_usd=0.51).refusal_code
        == "budget_exceeded"
    )
    assert add_work(task_id, "fits", 1, max_cost_usd=0.5).ok
    assert node_runs(task_id)[0]["accounted_cost_usd"] == 1.5


@pytest.mark.parametrize("status", ["admitted", "uncertain", "succeeded", "failed"])
def test_hidden_legacy_unknown_reservation_cannot_change_on_readd(db, status):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "old", 0).ok
    assert admit_dispatch(task_id, "old").ok
    if status != "admitted":
        assert record_outcome(task_id, "old", 1, status, None, None, "{}").ok
    with Session(db) as session:
        node = session.exec(select(SwarmPlanNode)).one()
        node.cancelled_in_version = 1
        run = session.exec(select(SwarmNodeRun)).one()
        run.reserved_cost_usd = None
        run.pin_json = None
        session.add(node)
        session.add(run)
        session.commit()
    history = node_runs(task_id)
    assert history[0]["accounted_cost_usd"] == 1.0
    for cap in (0.5, 1.5):
        refused = add_work(task_id, "old", 1, max_cost_usd=cap)
        assert refused.refusal_code == "legacy_reservation_conflict"
        assert_recorded(
            db, refused, "add_node", "refused", "legacy_reservation_conflict", 1, 1
        )
        assert load_graph(task_id) == []
        assert node_runs(task_id) == history
    assert add_work(task_id, "old", 1).ok
    assert node_runs(task_id) == history


@pytest.mark.parametrize("legacy", [True, False])
def test_hidden_known_cost_can_readd_without_changing_history(db, legacy):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "old", 0).ok
    assert admit_dispatch(task_id, "old").ok
    assert record_outcome(task_id, "old", 1, "failed", 0.5, None, "{}").ok
    with Session(db) as session:
        node = session.exec(select(SwarmPlanNode)).one()
        node.cancelled_in_version = 1
        session.add(node)
        if legacy:
            run = session.exec(select(SwarmNodeRun)).one()
            run.reserved_cost_usd = None
            run.pin_json = None
            session.add(run)
        session.commit()
    history = node_runs(task_id)
    assert add_work(task_id, "old", 1, max_cost_usd=0.25).ok
    assert node_runs(task_id) == history
    assert add_work(task_id, "fits", 2, max_cost_usd=1.5).ok
    assert admit_dispatch(task_id, "old").refusal_code == "node_budget_exhausted"


def test_hidden_pinned_unknown_cost_survives_readd_with_lower_ceiling(db):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "old", 0).ok
    assert admit_dispatch(task_id, "old").ok
    assert record_outcome(task_id, "old", 1, "failed", None, None, "{}").ok
    with Session(db) as session:
        node = session.exec(select(SwarmPlanNode)).one()
        node.cancelled_in_version = 1
        session.add(node)
        session.commit()
    history = node_runs(task_id)
    assert add_work(task_id, "old", 1, max_cost_usd=0.25).ok
    assert node_runs(task_id) == history
    assert add_work(task_id, "fits", 2).ok
    assert (
        add_work(task_id, "too-large", 3, max_cost_usd=0.01).refusal_code
        == "budget_exceeded"
    )


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
    assert record_outcome(task_id, "node", 2, "failed", 1.0, None, "{}").ok
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


def test_supplied_session_rolls_back_admission_and_audit(db):
    task_id = make_task(db)
    assert add_work(task_id, "node", 0).ok
    with Session(db) as session:
        result = admit_dispatch(
            task_id, "node", dispatch_key="attempt-1", session=session
        )
        assert result.ok
        session.rollback()
    assert node_runs(task_id) == []
    assert load_graph(task_id)[0]["armed_at"] is None
    assert last_call(db).tool == "add_node"


@pytest.mark.parametrize(
    "field,value", [("max_attempts", 11), ("turn_timeout_seconds", 43201)]
)
def test_node_hard_limit_ceilings(db, field, value):
    task_id = make_task(db)
    assert not add_work(task_id, "node", 0, **{field: value}).ok


def test_uncertain_reported_cost_cannot_release_active_reservation(db):
    task_id = make_task(db, budget=1.0)
    assert add_work(task_id, "one", 0, max_cost_usd=0.5).ok
    assert add_work(task_id, "two", 1, max_cost_usd=0.5).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 1, "uncertain", 0.1, None, "{}").ok
    assert node_runs(task_id, "one")[0]["accounted_cost_usd"] == 0.5
    assert node_runs(task_id, "one")[0]["accounting_basis"] == "active_reservation"
    assert admit_dispatch(task_id, "two").ok


def test_terminal_unknown_usage_allows_dependency_but_consumes_ceiling(db):
    task_id = make_task(db, budget=1.0)
    assert add_work(task_id, "one", 0, max_cost_usd=0.5).ok
    assert add_work(task_id, "two", 1, max_cost_usd=0.5, deps=["one"]).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 1, "succeeded", None, "head", "{}").ok
    row = node_runs(task_id, "one")[0]
    assert row["accounted_cost_usd"] == 0.5
    assert row["accounting_basis"] == "reserved_unknown_cost"
    assert admit_dispatch(task_id, "two").ok


def test_list_priced_completion_settles_at_its_cost_with_a_visible_basis(db):
    task_id = make_task(db, budget=1.0)
    assert add_work(task_id, "one", 0, max_cost_usd=0.5).ok
    assert add_work(task_id, "two", 1, max_cost_usd=0.5, deps=["one"]).ok
    assert admit_dispatch(task_id, "one").ok
    outcome = json.dumps({"status": "succeeded", "cost_basis": "list"})
    assert record_outcome(task_id, "one", 1, "succeeded", 0.08, "head", outcome).ok
    row = node_runs(task_id, "one")[0]
    assert row["accounted_cost_usd"] == 0.08
    assert row["accounting_basis"] == "list_priced"
    assert admit_dispatch(task_id, "two").ok


def test_provider_priced_completion_keeps_the_reported_basis(db):
    task_id = make_task(db, budget=1.0)
    assert add_work(task_id, "one", 0, max_cost_usd=0.5).ok
    assert admit_dispatch(task_id, "one").ok
    outcome = json.dumps({"status": "succeeded", "cost_basis": "provider"})
    assert record_outcome(task_id, "one", 1, "succeeded", 0.08, "head", outcome).ok
    assert node_runs(task_id, "one")[0]["accounting_basis"] == "reported"


def test_uncertain_run_keeps_its_reservation_even_with_a_list_price(db):
    task_id = make_task(db, budget=1.0)
    assert add_work(task_id, "one", 0, max_cost_usd=0.5).ok
    assert admit_dispatch(task_id, "one").ok
    outcome = json.dumps({"status": "uncertain", "cost_basis": "list"})
    assert record_outcome(task_id, "one", 1, "uncertain", 0.08, None, outcome).ok
    row = node_runs(task_id, "one")[0]
    assert row["accounted_cost_usd"] == 0.5
    assert row["accounting_basis"] == "active_reservation"


def test_dispatch_key_cannot_alias_another_node(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert add_work(task_id, "two", 1).ok
    first = admit_dispatch(task_id, "one", dispatch_key="stable-key")
    assert first.ok
    conflict = admit_dispatch(task_id, "two", dispatch_key="stable-key")
    assert not conflict.ok
    assert len(node_runs(task_id)) == 1


def test_replayed_admission_keeps_original_pin_after_graph_change(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    context = {
        "repo": "org/repo",
        "branch": "factory/work",
        "workflow_id": "flow",
        "artifact_path": "result-1.json",
        "artifact_schema": {"type": "object"},
    }
    first = admit_dispatch(
        task_id, "one", dispatch_key="one-1", execution_context=context
    )
    assert first.ok and first.attempt == 1
    assert first.pin["prompt"] == "do one"
    first.pin["artifact_schema"]["type"] = "string"
    assert add_work(task_id, "two", 1).ok
    replay = admit_dispatch(
        task_id, "one", dispatch_key="one-1", execution_context=context
    )
    assert replay.ok and replay.pin["artifact_schema"] == {"type": "object"}
    assert replay.pin["max_cost_usd"] == 1.0
    assert len(node_runs(task_id)) == 1
    conflict = admit_dispatch(
        task_id,
        "one",
        dispatch_key="one-1",
        execution_context={**context, "branch": "another"},
    )
    assert conflict.refusal_code == "dispatch_key_conflict"
    assert record_outcome(task_id, "one", 1, "succeeded", 0.2, "sha", "{}").ok
    assert admit_dispatch(
        task_id, "one", dispatch_key="one-1", execution_context=context
    ).ok
    assert (
        admit_dispatch(task_id, "one", dispatch_key="one-2").refusal_code
        == "node_succeeded"
    )


def test_unknown_execution_blocks_retries_until_reconciled(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    assert admit_dispatch(task_id, "one").refusal_code == "active_attempt"
    assert record_outcome(task_id, "one", 1, "uncertain", None, None, "{}").ok
    assert node_runs(task_id)[0]["finished_at"] is None
    assert admit_dispatch(task_id, "one").refusal_code == "active_attempt"
    assert discard(task_id, "one", 1).refusal_code == "armed"
    assert record_outcome(task_id, "one", 1, "failed", 0.25, None, "{}").ok
    next_attempt = admit_dispatch(task_id, "one")
    assert next_attempt.ok and next_attempt.pin["max_cost_usd"] == 0.75


def test_dependencies_require_success(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert add_work(task_id, "two", 1, deps=["one"]).ok
    assert admit_dispatch(task_id, "two").refusal_code == "dependency_not_succeeded"
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 1, "failed", 0.1, None, "{}").ok
    assert admit_dispatch(task_id, "two").refusal_code == "dependency_not_succeeded"
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 2, "succeeded", 0.1, "sha", "{}").ok
    assert admit_dispatch(task_id, "two").ok


def test_dispatch_outcome_replays_cannot_change_identity_or_terminal_evidence(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_dispatch(task_id, "one", 1, 42, "base").ok
    assert record_dispatch(task_id, "one", 1, 42, "base").ok
    assert (
        record_dispatch(task_id, "one", 1, 43, "base").refusal_code
        == "dispatch_conflict"
    )
    assert record_outcome(task_id, "one", 1, "succeeded", 0.25, "head", "{}").ok
    before = node_runs(task_id)[0]
    assert record_dispatch(task_id, "one", 1, 42, "base").ok
    assert record_outcome(task_id, "one", 1, "succeeded", 0.25, "head", "{}").ok
    assert (
        record_outcome(task_id, "one", 1, "failed", 0.25, "head", "{}").refusal_code
        == "outcome_conflict"
    )
    assert (
        record_outcome(task_id, "one", 1, "uncertain", None, None, "{}").refusal_code
        == "outcome_conflict"
    )
    assert node_runs(task_id)[0] == before


def test_task_budget_accounts_observed_overrun_and_other_reservations(db):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "one", 0).ok
    assert add_work(task_id, "two", 1).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 1, "failed", 1.25, None, "{}").ok
    assert admit_dispatch(task_id, "one").refusal_code == "node_budget_exhausted"
    assert admit_dispatch(task_id, "two").refusal_code == "task_budget_exhausted"
    assert node_runs(task_id)[0]["accounted_cost_usd"] == 1.25


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True, 10**1000])
def test_invalid_node_cost_is_refused_and_audited(db, value):
    task_id = make_task(db)
    result = add_work(task_id, "one", 0, max_cost_usd=value)
    assert result.refusal_code == "invalid_node_budget"
    assert json.loads(last_call(db).args_json)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_attempts", None),
        ("max_attempts", 0),
        ("max_attempts", -1),
        ("max_attempts", True),
        ("max_attempts", 1.5),
        ("turn_timeout_seconds", None),
        ("turn_timeout_seconds", 0),
        ("turn_timeout_seconds", True),
        ("turn_timeout_seconds", 1.5),
    ],
)
def test_missing_or_unbounded_node_limits_are_refused(db, field, value):
    task_id = make_task(db)
    assert not add_work(task_id, "one", 0, **{field: value}).ok


def test_dispatch_rejects_legacy_unbounded_node(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    with Session(db) as session:
        node = session.exec(select(SwarmPlanNode)).one()
        node.max_attempts = None
        session.add(node)
        session.commit()
    assert admit_dispatch(task_id, "one").refusal_code == "invalid_max_attempts"


def test_dispatch_requires_bounded_task_budget(db):
    task_id = make_task(db, budget=None)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").refusal_code == "invalid_task_budget"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_outcome_cost_does_not_release_reservation(db, value):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    assert (
        record_outcome(task_id, "one", 1, "failed", value, None, "{}").refusal_code
        == "invalid_cost"
    )
    assert node_runs(task_id)[0]["status"] == "admitted"
    assert node_runs(task_id)[0]["accounted_cost_usd"] == 1.0


def test_old_revision_survives_readd_and_bootstrap_node_discard(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert discard(task_id, "one", 1).ok
    assert add_work(task_id, "one", 2, prompt="replacement").ok
    assert load_graph(task_id, 1)[0]["prompt"] == "do one"
    assert load_graph(task_id, 1)[0]["discarded_in_version"] == 2
    assert load_graph(task_id, 2) == []
    assert load_graph(task_id, 3)[0]["prompt"] == "replacement"


def test_supplied_session_rolls_back_semantic_graph_changes(db):
    task_id = make_task(db)
    with Session(db) as session:
        assert add_work(task_id, "one", 0, session=session).ok
        session.rollback()
    assert current_version(task_id) == 0
    assert load_graph(task_id) == []


def test_bootstrap_fields_survive_discard_and_key_reuse(db):
    task_id = make_task(db)
    with Session(db) as session:
        session.add(
            SwarmPlanVersion(
                task_id=task_id,
                version=1,
                op="bootstrap",
                author_kind="system",
                author="router",
                change_json="{}",
                cause_kind="classification",
            )
        )
        session.add(
            SwarmPlanNode(
                task_id=task_id,
                node_key="one",
                kind="work",
                prompt="bootstrap prompt",
                model="worker",
                deps_json="[]",
                max_cost_usd=1.0,
                max_attempts=2,
                turn_timeout_seconds=60,
                side_effects=False,
                created_in_version=1,
            )
        )
        session.commit()
    assert discard(task_id, "one", 1).ok
    assert add_work(task_id, "one", 2, prompt="replacement").ok
    assert load_graph(task_id, 1)[0]["prompt"] == "bootstrap prompt"


def test_completed_unknown_cost_consumes_node_budget(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(task_id, "one", 1, "failed", None, "sha", "{}").ok
    assert admit_dispatch(task_id, "one").refusal_code == "node_budget_exhausted"


@pytest.mark.parametrize(
    "phase",
    ["never_dispatched", "not_invoked", "lost_before_guest"],
)
def test_terminal_null_cost_before_model_post_releases_reservation(db, phase):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    outcome = json.dumps({"invocation_phase": phase})
    assert record_outcome(task_id, "one", 1, "failed", None, None, outcome).ok
    row = node_runs(task_id, "one")[0]
    assert row["accounted_cost_usd"] == 0.0
    assert row["accounting_basis"] == "no_model_post"
    retry = admit_dispatch(task_id, "one")
    assert retry.ok and retry.attempt == 2
    assert retry.pin["max_cost_usd"] == 1.0


def test_typed_not_invoked_proof_releases_reservation(db):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    outcome = json.dumps(
        {"not_invoked": {"invocation_phase": "not_invoked", "session_id": 7}}
    )
    assert record_outcome(task_id, "one", 1, "failed", None, None, outcome).ok
    assert node_runs(task_id, "one")[0]["accounted_cost_usd"] == 0.0
    assert admit_dispatch(task_id, "one").ok


def test_legacy_no_post_attempt_does_not_pin_an_old_node_ceiling(db):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "old", 0).ok
    assert admit_dispatch(task_id, "old").ok
    outcome = json.dumps({"invocation_phase": "never_dispatched"})
    assert record_outcome(task_id, "old", 1, "failed", None, None, outcome).ok
    with Session(db) as session:
        node = session.exec(select(SwarmPlanNode)).one()
        node.cancelled_in_version = 1
        run = session.exec(select(SwarmNodeRun)).one()
        run.reserved_cost_usd = None
        run.pin_json = None
        session.add_all([node, run])
        session.commit()
    assert add_work(task_id, "old", 1, max_cost_usd=0.5).ok
    assert node_runs(task_id, "old")[0]["accounted_cost_usd"] == 0.0


@pytest.mark.parametrize(
    "outcome",
    [
        {},
        {"invocation_phase": "guest_cessation_confirmed"},
        {"invocation_phase": "response_lost"},
        {"previous_outcome": {"invocation_phase": "not_invoked"}},
        {"not_invoked": {"invocation_phase": "guest_cessation_confirmed"}},
    ],
)
def test_terminal_null_cost_without_no_post_proof_keeps_reservation(db, outcome):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert admit_dispatch(task_id, "one").ok
    assert record_outcome(
        task_id, "one", 1, "failed", None, None, json.dumps(outcome)
    ).ok
    row = node_runs(task_id, "one")[0]
    assert row["accounted_cost_usd"] == 1.0
    assert row["accounting_basis"] == "reserved_unknown_cost"
    assert admit_dispatch(task_id, "one").refusal_code == "node_budget_exhausted"


def test_no_post_marker_does_not_release_an_active_or_measured_attempt(db):
    task_id = make_task(db, budget=2.0)
    assert add_work(task_id, "active", 0).ok
    assert add_work(task_id, "measured", 1).ok
    assert admit_dispatch(task_id, "active").ok
    outcome = json.dumps({"invocation_phase": "not_invoked"})
    assert record_outcome(task_id, "active", 1, "uncertain", None, None, outcome).ok
    active = node_runs(task_id, "active")[0]
    assert active["accounted_cost_usd"] == 1.0
    assert active["accounting_basis"] == "active_reservation"
    assert admit_dispatch(task_id, "active").refusal_code == "active_attempt"

    assert admit_dispatch(task_id, "measured").ok
    assert record_outcome(task_id, "measured", 1, "failed", 0.25, None, outcome).ok
    measured = node_runs(task_id, "measured")[0]
    assert measured["accounted_cost_usd"] == 0.25
    assert measured["accounting_basis"] == "reported"


@pytest.mark.parametrize(
    "context",
    [
        {"max_cost_usd": 100.0},
        {"artifact_schema": {"value": float("nan")}},
        {1: True},
        {"artifact_schema": {1, 2}},
    ],
)
def test_invalid_context_cannot_override_pinned_node_fields(db, context):
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    assert (
        admit_dispatch(task_id, "one", execution_context=context).refusal_code
        == "invalid_execution_context"
    )
    assert node_runs(task_id) == []


def test_hour_scale_turn_timeout_is_admitted_and_pinned(db):
    task_id = make_task(db)
    result = add_work(task_id, "long", 0, turn_timeout_seconds=43200)
    assert result.ok
    assert load_graph(task_id)[0]["turn_timeout_seconds"] == 43200
    admitted = admit_dispatch(task_id, "long", dispatch_key="long-1")
    assert admitted.ok
    assert admitted.pin["turn_timeout_seconds"] == 43200
    replay = admit_dispatch(task_id, "long", dispatch_key="long-1")
    assert replay.ok and replay.pin == admitted.pin
    assert len(node_runs(task_id)) == 1


def plan_add(node_key, deps=(), **overrides):
    values = {
        "op": "add_node",
        "node_key": node_key,
        "kind": "work",
        "prompt": f"do {node_key}",
        "model": "worker-model",
        "deps": list(deps),
        "max_cost_usd": 1.0,
        "side_effects": False,
        "max_attempts": 2,
        "turn_timeout_seconds": 60,
        "stated_reason": f"the plan needs {node_key}",
    }
    values.update(overrides)
    return values


def apply_plan(task_id, edits, expected_version=0, **overrides):
    values = {
        "author_kind": "conductor",
        "author": "model",
        "cause_kind": "factory_conductor",
        "cause_ref": "plan-1",
        "expected_version": expected_version,
    }
    values.update(overrides)
    return graph.apply_edits(task_id, edits=edits, **values)


def test_plan_applies_every_edit_as_its_own_chained_version(db):
    task_id = make_task(db)
    result = apply_plan(
        task_id,
        [
            plan_add("scope"),
            plan_add("fix", deps=["scope"]),
            plan_add("check", deps=["fix"]),
        ],
    )
    assert result.ok and result.version == 3
    assert current_version(task_id) == 3
    nodes = {node["node_key"]: node for node in load_graph(task_id)}
    assert set(nodes) == {"scope", "fix", "check"}
    assert [nodes[key]["created_in_version"] for key in ("scope", "fix", "check")] == [
        1,
        2,
        3,
    ]
    assert nodes["check"]["deps"] == ["fix"]
    with Session(db) as session:
        versions = session.exec(
            select(SwarmPlanVersion).order_by(SwarmPlanVersion.version)
        ).all()
        assert [v.op for v in versions] == ["add_node"] * 3
        assert {v.cause_ref for v in versions} == {"plan-1"}
        call = session.exec(
            select(SwarmConductorCall).order_by(SwarmConductorCall.id.desc())
        ).first()
        assert call.tool == "apply_edits" and call.outcome == "applied"
        assert (call.version_before, call.version_after) == (0, 3)
        # The unbounded prompt body is summarised rather than copied per edit.
        recorded = json.loads(call.args_json)["edits"]
        assert [edit["prompt_chars"] for edit in recorded] == [8, 6, 8]
        assert all("prompt" not in edit for edit in recorded)


def test_one_refused_edit_leaves_no_part_of_the_plan_applied(db):
    task_id = make_task(db)
    result = apply_plan(
        task_id,
        [
            plan_add("scope"),
            plan_add("fix", deps=["absent"]),
            plan_add("check", deps=["fix"]),
        ],
    )
    assert not result.ok and result.refusal_code == "unknown_dep"
    assert "edit 1 (fix)" in result.detail and "absent" in result.detail
    assert current_version(task_id) == 0
    assert load_graph(task_id) == []
    with Session(db) as session:
        # The rollback discards the staged edits, and the refusal survives it.
        assert session.exec(select(SwarmPlanVersion)).all() == []
        calls = session.exec(
            select(SwarmConductorCall).order_by(SwarmConductorCall.id)
        ).all()
        assert [call.outcome for call in calls] == ["refused"]
        assert calls[0].tool == "apply_edits"


@pytest.mark.parametrize(
    "edits, code",
    [
        ([plan_add("cycle", deps=["cycle"])], "unknown_dep"),
        ([plan_add("a"), plan_add("a")], "duplicate_key"),
        ([plan_add("a", kind="nonsense")], "invalid_kind"),
        ([plan_add("a", max_attempts=99)], "invalid_max_attempts"),
        ([plan_add("a", max_cost_usd=100.0)], "budget_exceeded"),
        ([{"op": "reshape", "node_key": "a"}], "invalid_edits"),
        ([], "invalid_edits"),
        ([plan_add("a")] * (graph.MAX_PLAN_EDITS + 1), "invalid_edits"),
    ],
)
def test_plan_preserves_every_single_edit_refusal(db, edits, code):
    task_id = make_task(db)
    result = apply_plan(task_id, edits)
    assert not result.ok and result.refusal_code == code
    assert current_version(task_id) == 0 and load_graph(task_id) == []


def test_plan_refuses_a_stale_expected_version_without_writing(db):
    task_id = make_task(db)
    assert add_work(task_id, "existing", 0).ok
    result = apply_plan(task_id, [plan_add("late")], expected_version=0)
    assert not result.ok and result.refusal_code == "stale_version"
    assert current_version(task_id) == 1
    assert [node["node_key"] for node in load_graph(task_id)] == ["existing"]


def test_plan_discard_and_re_add_share_one_transaction(db):
    task_id = make_task(db)
    assert add_work(task_id, "old", 0).ok
    result = apply_plan(
        task_id,
        [
            {
                "op": "discard_node",
                "node_key": "old",
                "stated_reason": "superseded by the new plan",
            },
            plan_add("new"),
        ],
        expected_version=1,
    )
    assert result.ok and result.version == 3
    assert [node["node_key"] for node in load_graph(task_id)] == ["new"]
    assert [node["node_key"] for node in load_graph(task_id, 1)] == ["old"]


def test_plan_discard_refusal_rolls_back_an_already_staged_add(db):
    task_id = make_task(db)
    assert add_work(task_id, "armed_node", 0).ok
    assert admit_dispatch(task_id, "armed_node", dispatch_key="k").ok
    result = apply_plan(
        task_id,
        [
            plan_add("new"),
            {"op": "discard_node", "node_key": "armed_node", "stated_reason": "no"},
        ],
        expected_version=1,
    )
    assert not result.ok and result.refusal_code == "armed"
    assert [node["node_key"] for node in load_graph(task_id)] == ["armed_node"]
    assert current_version(task_id) == 1


def test_a_plan_edit_may_precede_the_edits_it_depends_on(db):
    task_id = make_task(db)
    result = apply_plan(
        task_id,
        [
            plan_add("check", deps=["fix"]),
            plan_add("fix", deps=["scope"]),
            plan_add("scope"),
        ],
    )
    assert result.ok and result.version == 3
    nodes = {node["node_key"]: node for node in load_graph(task_id)}
    # The batch applies in dependency order, not in the order it was written.
    assert [nodes[key]["created_in_version"] for key in ("scope", "fix", "check")] == [
        1,
        2,
        3,
    ]
    assert nodes["check"]["deps"] == ["fix"]


def test_a_plan_may_name_a_dep_by_the_key_its_author_wrote(db):
    task_id = make_task(db)
    result = apply_plan(
        task_id,
        [
            plan_add("implement_fix", raw_node_key="fix"),
            plan_add("review_check", raw_node_key="check", deps=["fix"]),
        ],
    )
    assert result.ok
    nodes = {node["node_key"]: node for node in load_graph(task_id)}
    assert nodes["review_check"]["deps"] == ["implement_fix"]


def test_the_authors_key_never_shadows_a_real_node(db):
    task_id = make_task(db)
    assert add_work(task_id, "fix", 0).ok
    result = apply_plan(
        task_id,
        [plan_add("implement_fix", raw_node_key="fix", deps=["fix"])],
        expected_version=1,
    )
    assert result.ok
    nodes = {node["node_key"]: node for node in load_graph(task_id)}
    # A live node answers to that exact name, so the dep is left alone.
    assert nodes["implement_fix"]["deps"] == ["fix"]


def test_an_ambiguous_authors_key_resolves_to_nothing(db):
    task_id = make_task(db)
    result = apply_plan(
        task_id,
        [
            plan_add("investigate_look", raw_node_key="look"),
            plan_add("implement_look", raw_node_key="look"),
            plan_add("review_check", raw_node_key="check", deps=["look"]),
        ],
    )
    assert not result.ok and result.refusal_code == "unknown_dep"
    assert load_graph(task_id) == []


def test_a_circular_batch_refuses_rather_than_reordering(db):
    task_id = make_task(db)
    result = apply_plan(task_id, [plan_add("a", deps=["b"]), plan_add("b", deps=["a"])])
    assert not result.ok and result.refusal_code == "unknown_dep"
    assert current_version(task_id) == 0 and load_graph(task_id) == []


def test_reordering_never_moves_an_add_across_a_discard_of_its_key(db):
    task_id = make_task(db)
    assert add_work(task_id, "fix", 0).ok
    result = apply_plan(
        task_id,
        [
            {"op": "discard_node", "node_key": "fix", "stated_reason": "replan"},
            plan_add("fix", max_cost_usd=2.0),
        ],
        expected_version=1,
    )
    assert result.ok and result.version == 3
    nodes = {node["node_key"]: node for node in load_graph(task_id)}
    assert nodes["fix"]["max_cost_usd"] == 2.0
    assert nodes["fix"]["created_in_version"] == 3


def test_a_plan_repoints_a_dependency_by_discarding_and_re_adding_it(db):
    """The fan-in edit the factory builds: insert a node, then move a dep onto it."""
    task_id = make_task(db)
    assert apply_plan(
        task_id,
        [
            plan_add("alpha"),
            plan_add("beta"),
            plan_add("check", deps=["alpha", "beta"]),
        ],
    ).ok
    result = apply_plan(
        task_id,
        [
            plan_add("merge", deps=["alpha", "beta"]),
            {
                "op": "discard_node",
                "node_key": "check",
                "stated_reason": "repointing check at merge",
            },
            plan_add("check", deps=["merge"]),
        ],
        expected_version=3,
        cause_ref="factory-loop:integrate_1",
    )
    assert result.ok and result.version == 6
    nodes = {node["node_key"]: node for node in load_graph(task_id)}
    assert set(nodes) == {"alpha", "beta", "merge", "check"}
    assert nodes["merge"]["deps"] == ["alpha", "beta"]
    assert nodes["check"]["deps"] == ["merge"]
    # The discard and its re-add keep their written order, and merge is added
    # before the re-add that depends on it.
    with Session(db) as session:
        versions = session.exec(
            select(SwarmPlanVersion)
            .where(SwarmPlanVersion.cause_ref == "factory-loop:integrate_1")
            .order_by(SwarmPlanVersion.version)
        ).all()
    assert [v.op for v in versions] == ["add_node", "discard_node", "add_node"]
    assert [json.loads(v.change_json)["node_key"] for v in versions] == [
        "merge",
        "check",
        "check",
    ]


def test_repointing_an_armed_dependency_refuses_the_whole_plan(db):
    task_id = make_task(db)
    assert apply_plan(
        task_id, [plan_add("alpha"), plan_add("check", deps=["alpha"])]
    ).ok
    assert record_outcome(
        task_id,
        "alpha",
        admit_dispatch(task_id, "alpha").attempt,
        "succeeded",
        0.1,
        None,
        "{}",
    ).ok
    assert admit_dispatch(task_id, "check").ok
    result = apply_plan(
        task_id,
        [
            plan_add("merge", deps=["alpha"]),
            {
                "op": "discard_node",
                "node_key": "check",
                "stated_reason": "repointing check at merge",
            },
            plan_add("check", deps=["merge"]),
        ],
        expected_version=2,
    )
    assert not result.ok and result.refusal_code == "armed"
    nodes = {node["node_key"]: node for node in load_graph(task_id)}
    assert set(nodes) == {"alpha", "check"} and nodes["check"]["deps"] == ["alpha"]


def test_a_dispatched_model_pins_the_attempt_without_touching_the_node(db):
    """The plan keeps the model it asked for; the pin records what ran."""
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    context = {"repo": "org/repo", "branch": "factory/work", "workflow_id": "flow"}
    first = admit_dispatch(
        task_id, "one", dispatch_key="one-1", execution_context=context, model="astra"
    )
    assert first.ok and first.pin["model"] == "astra"
    node = next(n for n in load_graph(task_id) if n["node_key"] == "one")
    assert node["model"] == "worker-model"


def test_a_replay_keeps_its_original_pinned_model(db):
    """A replay is the same attempt, so the dispatcher's current choice cannot
    change what it already ran on."""
    task_id = make_task(db)
    assert add_work(task_id, "one", 0).ok
    context = {"repo": "org/repo", "branch": "factory/work", "workflow_id": "flow"}
    first = admit_dispatch(
        task_id, "one", dispatch_key="one-1", execution_context=context
    )
    assert first.ok and first.pin["model"] == "worker-model"
    replay = admit_dispatch(
        task_id, "one", dispatch_key="one-1", execution_context=context, model="astra"
    )
    assert replay.ok and replay.pin["model"] == "worker-model"
    assert len(node_runs(task_id)) == 1
