from types import SimpleNamespace

import pytest

from swarm import factory_conductor as conductor
from swarm.turn_artifact import schema_errors


@pytest.fixture(autouse=True)
def factory_ceiling(monkeypatch):
    """Two lanes need a ceiling that holds both; it bounds their sum.

    A test that cares about the ceiling itself still sets or clears the
    variable for its own case, and that assignment wins over this one. The
    background reserve is pinned to zero for the same reason: these tests fake
    the free-slot count directly, and the reserve has its own cases below.
    """
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setenv("FACTORY_BACKGROUND_RESERVE", "0")


def test_conductor_contract_rejects_missing_action_fields_and_authority_changes():
    assert schema_errors(
        {"action": "add_node", "reason": "do work"}, conductor.DECISION_SCHEMA
    )
    assert schema_errors(
        {"action": "finish", "reason": "done", "pr_number": 12, "budget_usd": 100000},
        conductor.DECISION_SCHEMA,
    )
    assert not schema_errors(
        {
            "action": "add_node",
            "reason": "fix defect",
            "node_key": "implement_fix",
            "role": "implement",
            "prompt": "Fix the reported defect",
            "deps": [],
        },
        conductor.DECISION_SCHEMA,
    )
    assert not schema_errors(
        {
            "status": "escalate",
            "reason": "needs conductor review",
            "summary": "bounded escalation",
            "pr_number": None,
            "head_sha": None,
            "requested_model": "opus",
        },
        conductor.RESULT_SCHEMA,
    )


def test_missing_delivery_branch_hydrates_base_without_hiding_outages(monkeypatch):
    import httpx

    task = {"id": "t-1", "repo": "owner/repo", "base_branch": "main"}
    code = 404

    def missing(_repo, _path):
        response = httpx.Response(
            code, request=httpx.Request("GET", "https://example.test/ref")
        )
        response.raise_for_status()

    monkeypatch.setattr(conductor, "github_get", missing)
    assert conductor.hydration_branch(task) == "main"
    code = 503
    with pytest.raises(httpx.HTTPStatusError):
        conductor.hydration_branch(task)


def test_github_object_and_list_readers_enforce_response_shapes(monkeypatch):
    monkeypatch.setattr(conductor, "_github_read", lambda *_args: [1, 2])
    assert conductor.github_list("owner/repo", "issues") == [1, 2]
    with pytest.raises(ValueError, match="non-object"):
        conductor.github_get("owner/repo", "issues")
    monkeypatch.setattr(conductor, "_github_read", lambda *_args: {"id": 1})
    assert conductor.github_get("owner/repo", "issues") == {"id": 1}
    with pytest.raises(ValueError, match="non-array"):
        conductor.github_list("owner/repo", "issues")


def test_refine_schema_and_boundary_are_separate_from_delivery():
    from swarm.factory_refine import REFINE_SCHEMA

    task = {"id": "t-1", "repo": "owner/repo", "base_branch": "main"}
    assert conductor._schema("refine_1") is REFINE_SCHEMA
    boundary = conductor._boundary(task, refine=True)
    assert "Factory refine task t-1" in boundary
    assert "Do not create a branch" in boundary
    assert "do not open a pull request" in boundary
    delivery = conductor._boundary(task)
    assert "dedicated branch factory/t-1" in delivery
    assert "Factory refine task" not in delivery
    # An explicit raise, not an assert: a python -O run strips asserts and
    # would hand a reviewer node the refine boundary instead of refusing.
    with pytest.raises(ValueError, match="never both"):
        conductor._boundary(task, review=True, refine=True)


def delivery(
    monkeypatch,
    *,
    review_head=None,
    draft=False,
    check_state="success",
    reviewer="opus",
    review_session=11,
    body="Delivers the fix.\n\nCloses #77",
):
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
    head = "a" * 40
    task = {
        "id": "t-1",
        "repo": "owner/repo",
        "base_branch": "main",
        "conductor_model": "opus",
        "issue_number": 77,
    }
    pr = {
        "state": "open",
        "draft": draft,
        "body": body,
        "head": {
            "sha": head,
            "ref": "factory/t-1",
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main"},
        "html_url": "https://github.com/owner/repo/pull/3",
    }
    checks = {
        "state": check_state,
        "statuses": [{"context": "pr-checks", "state": check_state}],
    }
    monkeypatch.setattr(
        conductor,
        "github_get",
        lambda _repo, path: pr if path.startswith("pulls/") else checks,
    )
    import json

    runs = [
        {
            "id": 1,
            "node_key": "implement_fix",
            "status": "succeeded",
            "session_id": 10,
            "outcome_json": json.dumps(
                {
                    "value": {
                        "status": "complete",
                        "summary": "Implemented the requested fix.",
                        "pr_number": 3,
                        "head_sha": head,
                    }
                }
            ),
        },
        {
            "id": 2,
            "node_key": "review_fix",
            "status": "succeeded",
            "session_id": review_session,
            "head_sha": head,
            "pin": {"model": reviewer},
            "outcome_json": json.dumps(
                {
                    "value": {
                        "pr_number": 3,
                        "head_sha": review_head or head,
                        "verdict": "approve",
                        "summary": "Reviewed the exact head.",
                    }
                }
            ),
        },
    ]
    for run in runs:
        outcome = json.loads(run["outcome_json"])
        outcome["artifact"] = {"status": "ok", "value": outcome["value"], "errors": []}
        run["outcome_json"] = json.dumps(outcome)
    return task, runs


def test_delivery_requires_review_of_current_head(monkeypatch):
    task, runs = delivery(monkeypatch, review_head="b" * 40)
    with pytest.raises(ValueError, match="exact-head"):
        conductor.verify_delivery(task, 3, runs)


@pytest.mark.parametrize(
    "kwargs", [{"draft": True}, {"check_state": "pending"}, {"check_state": "failure"}]
)
def test_delivery_does_not_complete_with_draft_or_unpassed_ci(monkeypatch, kwargs):
    task, runs = delivery(monkeypatch, **kwargs)
    with pytest.raises(ValueError):
        conductor.verify_delivery(task, 3, runs)


def test_delivery_requires_independent_session(monkeypatch):
    task, runs = delivery(monkeypatch)
    runs[1]["session_id"] = 10
    with pytest.raises(ValueError, match="independent"):
        conductor.verify_delivery(task, 3, runs)


def test_later_changes_requested_supersedes_earlier_approval(monkeypatch):
    import copy
    import json

    task, runs = delivery(monkeypatch)
    later = copy.deepcopy(runs[-1])
    later["id"] = 3
    result = json.loads(later["outcome_json"])
    result["value"]["verdict"] = "changes_requested"
    later["outcome_json"] = json.dumps(result)
    runs.append(later)
    with pytest.raises(ValueError, match="exact-head"):
        conductor.verify_delivery(task, 3, runs)


def test_verified_delivery_returns_evidence_without_merging(monkeypatch):
    task, runs = delivery(monkeypatch)
    result = conductor.verify_delivery(task, 3, runs)
    assert result == {
        "pr_url": "https://github.com/owner/repo/pull/3",
        "head_sha": "a" * 40,
        "review_session_id": 11,
        # The approval records which model gave it.
        "reviewer_model": "opus",
        "state": "ready_for_review",
    }


def test_a_fallback_reviewer_still_delivers_and_is_named_in_the_evidence(monkeypatch):
    """Independence is the session, not the model: Astra reviewing is a review."""
    task, runs = delivery(monkeypatch, reviewer="astra")
    result = conductor.verify_delivery(task, 3, runs, ["opus", "astra"])
    assert result["reviewer_model"] == "astra"
    assert result["review_session_id"] == 11


def test_a_reviewer_outside_the_pool_is_not_evidence(monkeypatch):
    task, runs = delivery(monkeypatch, reviewer="luna")
    with pytest.raises(ValueError, match="exact-head"):
        conductor.verify_delivery(task, 3, runs, ["opus", "astra"])


def test_judgment_delivery_refuses_a_below_floor_approver(monkeypatch):
    """Dispatch makes judgment review wait rather than fall back, so this is
    unreachable. The completion gate refuses it anyway."""
    task, runs = delivery(monkeypatch, reviewer="astra")
    with pytest.raises(ValueError, match="exact-head"):
        conductor.verify_delivery(task, 3, runs, ["opus", "astra"], judgment=True)


def test_judgment_delivery_accepts_an_opus_approver(monkeypatch):
    task, runs = delivery(monkeypatch, reviewer="opus")
    result = conductor.verify_delivery(task, 3, runs, ["opus", "astra"], judgment=True)
    assert result["reviewer_model"] == "opus"


def test_a_fallback_reviewer_in_the_implementer_session_is_refused(monkeypatch):
    """The session check is the independence check, and it does not relax."""
    task, runs = delivery(monkeypatch, reviewer="astra", review_session=10)
    with pytest.raises(ValueError, match="exact-head"):
        conductor.verify_delivery(task, 3, runs, ["opus", "astra"])


def test_durable_active_node_reconciles_before_new_planning(monkeypatch):
    import swarm.factory_controls as controls

    calls = []
    active = {"node_key": "implement_fix", "status": "admitted", "attempt": 1}
    monkeypatch.setattr(conductor, "_task", lambda _id: {"id": "t-1"})
    monkeypatch.setattr(conductor.graph, "node_runs", lambda _id: [active])
    monkeypatch.setattr(
        conductor, "_submit_or_reconcile", lambda *args: calls.append(args)
    )
    monkeypatch.setattr(
        conductor.graph,
        "load_graph",
        lambda _id: pytest.fail("must reconcile existing attempt first"),
    )
    monkeypatch.setattr(
        controls,
        "can_start",
        lambda _id: pytest.fail("an active attempt is not new admission"),
    )
    dbos = object()
    conductor.reconcile_task("t-1", {}, dbos)
    assert calls == [({"id": "t-1"}, active, dbos)]


def test_running_workflow_does_not_start_another_session(monkeypatch):
    run = {"pin": {"workflow_id": "factory-node:t-1:work:1"}}
    dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="PENDING")
    )
    conductor._submit_or_reconcile({"id": "t-1"}, run, dbos)


def test_tick_admits_up_to_the_concurrency_limit(
    monkeypatch,
):
    import swarm.factory_controls as controls
    import swarm.factory_intake as intake

    policy = {"max_tasks": 3}
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setattr(
        controls,
        "status",
        lambda: {"state": "enabled", "policy": policy, "active_tasks": []},
    )
    monkeypatch.setattr(conductor.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(conductor.runtime, "init_dbos", lambda: object())
    ingested = []
    monkeypatch.setattr(conductor, "ingest_eligible", lambda p: ingested.append(p))
    admitted = iter(
        [
            {"ok": True, "task_id": "t-1", "policy": policy},
            {"ok": True, "task_id": "t-2", "policy": policy},
            {"ok": True, "task_id": "t-3", "policy": policy},
        ]
    )
    monkeypatch.setattr(intake, "admit_next", lambda _actor, **_kwargs: next(admitted))
    reconciled = []
    monkeypatch.setattr(
        conductor,
        "reconcile_task",
        lambda task_id, p, _dbos: reconciled.append(task_id),
    )
    conductor.tick()
    assert ingested == [policy]
    # Tasks admitted this tick are reconciled on the next one.
    assert reconciled == []


def test_tick_at_the_limit_reconciles_without_ingesting_or_admitting(monkeypatch):
    import swarm.factory_controls as controls
    import swarm.factory_intake as intake

    policy = {"max_tasks": 1}
    monkeypatch.delenv("FACTORY_MAX_CONCURRENT_TASKS", raising=False)
    monkeypatch.setattr(
        controls,
        "status",
        lambda: {
            "state": "enabled",
            "policy": policy,
            "active_tasks": [{"task_id": "t-1", "policy": policy}],
        },
    )
    monkeypatch.setattr(conductor.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(conductor.runtime, "init_dbos", lambda: object())
    monkeypatch.setattr(
        conductor, "ingest_eligible", lambda _p: pytest.fail("at the limit")
    )
    monkeypatch.setattr(
        intake,
        "admit_next",
        lambda _a, **_kwargs: pytest.fail("at the limit"),
    )
    reconciled = []
    monkeypatch.setattr(
        conductor,
        "reconcile_task",
        lambda task_id, p, _dbos: reconciled.append(task_id),
    )
    conductor.tick()
    assert reconciled == ["t-1"]


def test_tick_isolates_a_failing_task_and_a_failing_ingest(monkeypatch):
    import swarm.factory_controls as controls
    import swarm.factory_intake as intake

    policy = {"max_tasks": 3}
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "3")
    monkeypatch.setattr(
        controls,
        "status",
        lambda: {
            "state": "enabled",
            "policy": policy,
            "active_tasks": [
                {"task_id": "t-poisoned", "policy": policy},
                {"task_id": "t-healthy", "policy": policy},
            ],
        },
    )
    monkeypatch.setattr(conductor.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(conductor.runtime, "init_dbos", lambda: object())
    reconciled = []

    def reconcile(task_id, _policy, _dbos):
        reconciled.append(task_id)
        if task_id == "t-poisoned":
            raise ValueError("factory outcome refused: conflicting_outcome")

    monkeypatch.setattr(conductor, "reconcile_task", reconcile)

    def ingest(_policy):
        raise RuntimeError("404 on a transferred issue")

    monkeypatch.setattr(conductor, "ingest_eligible", ingest)
    monkeypatch.setattr(
        intake,
        "admit_next",
        lambda _a, **_kwargs: pytest.fail("ingest raised"),
    )
    conductor.tick()
    assert reconciled == ["t-poisoned", "t-healthy"]


def test_stopped_factory_never_polls_or_admits(monkeypatch):
    import swarm.factory_controls as controls

    monkeypatch.setattr(
        controls,
        "status",
        lambda: {
            "state": "stopped",
            "active_tasks": [{"task_id": "t-1"}, {"task_id": "t-2"}],
        },
    )
    monkeypatch.setattr(conductor.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(conductor.runtime, "init_dbos", lambda: object())
    monkeypatch.setattr(
        conductor, "ingest_eligible", lambda _: pytest.fail("stopped admission")
    )
    cancelled = []

    def cancel(task_id, _dbos):
        cancelled.append(task_id)
        if task_id == "t-1":
            raise RuntimeError("reap failed")

    monkeypatch.setattr(conductor, "cancel_owned", cancel)
    conductor.tick()
    assert cancelled == ["t-1", "t-2"]


@pytest.fixture
def feedback_db(tmp_path, monkeypatch):
    from sqlalchemy import event
    from sqlmodel import Session, SQLModel, create_engine
    from swarm import factory_controls as controls
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

    engine = create_engine(
        f"sqlite:///{tmp_path / 'decision-feedback.db'}",
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
    with Session(engine) as db:
        db.add(FactoryControl(id="factory", actor="migration"))
        db.commit()
    for module in (conductor, conductor.graph, controls):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: pytest.fail("unexpected GitHub read")
    )
    # Dispatch reads the shared background pool now, the serial lane included,
    # and this fixture holds no admission tables. A test that cares about the
    # pool sets its own count afterwards, which wins over this one.
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    yield engine
    engine.dispose()


def feedback_task(
    *,
    max_turns=18,
    body="Untrusted issue text",
    task_class="bug-fix",
    **overrides,
):
    from swarm import factory_controls as controls
    from swarm.factory_intake import admit_next, receive_issue

    policy = {
        "repo": "owner/repo",
        "issue_numbers": [7],
        "generation": 0,
        # Both lanes open: this helper admits advisory classes too, and the
        # advisory lane is opt-in.
        "max_tasks": {"delivery": 1, "advisory": 1},
        "max_turns_per_task": max_turns,
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
    # Fields the receipt must pin, such as max_parallel_nodes, have to be in
    # the policy before it is configured and the task admitted.
    policy.update(overrides)
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        "owner/repo",
        7,
        "Fix issue",
        body,
        "https://github.com/owner/repo/issues/7",
        "poller",
        task_class=task_class,
    )
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    return conductor._task(admitted["task_id"]), policy


def complete_feedback_node(
    task, policy, node_key, value, *, head=None, deps=None, model=None, **kwargs
):
    model = model or ("luna" if node_key.startswith("implement_") else "opus")
    assert conductor._add(
        task,
        policy,
        node_key,
        "bounded work",
        list(deps or []),
        model,
        f"test:{node_key}",
        "test fixture",
        review=node_key.startswith("review_"),
    ).ok
    return run_feedback_node(task, node_key, value, head=head, **kwargs)


def run_feedback_node(
    task, node_key, value, *, head=None, status="succeeded", reason=None
):
    """Reserve and settle one attempt of a node that is already in the graph."""
    import json
    from swarm import factory_controls as controls

    workflow = f"factory-node:{task['id']}:{node_key}:1"
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task['id']}",
        "workflow_id": workflow,
        "artifact_path": f".factory/{node_key}.json",
        "artifact_schema": conductor._schema(node_key),
        "hydration_branch": "main",
        "retry_context": "[]",
    }
    assert conductor.reserve_node(task["id"], node_key, workflow, context)
    session_id = 100 + len(controls.task_snapshot(task["id"])["starts"])
    result = {
        "status": status,
        "session_id": session_id,
        "cost_usd": 0.25,
        "head_sha": head,
        "value": value,
        "artifact": {"status": "ok", "value": value},
        "cleanup": {"status": "completed"},
    }
    if reason is not None:
        result["reason"] = reason
    assert conductor.graph.record_dispatch(task["id"], node_key, 1, session_id, None).ok
    assert conductor.graph.record_outcome(
        task["id"], node_key, 1, status, 0.25, head, json.dumps(result)
    ).ok
    assert controls.record_start_outcome(
        task["id"],
        workflow,
        status,
        "worker",
        cost_usd=0.25,
        session_id=session_id,
    )["ok"]
    return next(
        r for r in conductor.graph.node_runs(task["id"]) if r["node_key"] == node_key
    )


def settle_admitted_node(task, node_key, value, *, head=None, status="succeeded"):
    """Settle an attempt the reconciler already admitted, without re-reserving it.

    The reconciler pins its own execution context, including the branch a
    fanned-out node works on, so a test cannot rebuild that context by hand.
    """
    import json
    from swarm import factory_controls as controls

    run = next(
        r
        for r in conductor.graph.node_runs(task["id"])
        if r["node_key"] == node_key and r["status"] == "admitted"
    )
    session_id = 200 + run["attempt"]
    result = {
        "status": status,
        "session_id": session_id,
        "cost_usd": 0.25,
        "head_sha": head,
        "value": value,
        "artifact": {"status": "ok", "value": value},
    }
    assert conductor.graph.record_dispatch(
        task["id"], node_key, run["attempt"], session_id, None
    ).ok
    assert conductor.graph.record_outcome(
        task["id"], node_key, run["attempt"], status, 0.25, head, json.dumps(result)
    ).ok
    assert controls.record_start_outcome(
        task["id"],
        run["pin"]["workflow_id"],
        status,
        "worker",
        cost_usd=0.25,
        session_id=session_id,
    )["ok"]


def feedback_audits(engine, task_id):
    import json
    from sqlmodel import Session, select
    from swarm.factory_models import FactoryAudit

    with Session(engine) as db:
        return [
            json.loads(row.detail_json)
            for row in db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.task_id == task_id,
                    FactoryAudit.action == "conductor_rejected",
                )
            ).all()
        ]


def test_discard_before_task_branch_exists_records_absent_head_and_advances(
    feedback_db, monkeypatch
):
    import json
    import httpx
    from sqlmodel import Session, select
    from swarm.models import SwarmConductorCall

    task, policy = feedback_task()
    assert conductor._add(
        task, policy, "implement_spare", "unused", [], "luna", "spare", "not needed"
    ).ok
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "discard_node",
            "node_key": "implement_spare",
            "reason": "unneeded work",
        },
    )
    reads = []

    def missing(repo, path):
        assert repo == task["repo"]
        assert path == f"git/ref/heads/factory%2F{task['id']}"
        reads.append(path)
        httpx.Response(
            404, request=httpx.Request("GET", "https://example.test/ref")
        ).raise_for_status()

    monkeypatch.setattr(conductor, "github_get", missing)
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    feedback_db.dispose()
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    assert len(reads) == 1
    assert "implement_spare" not in {
        n["node_key"] for n in conductor.graph.load_graph(task["id"])
    }
    with Session(feedback_db) as db:
        call = db.exec(
            select(SwarmConductorCall).where(SwarmConductorCall.tool == "discard_node")
        ).one()
        assert call.outcome == "applied"
        assert json.loads(call.args_json)["observed_branch_head"] is None
    conductor.reconcile_task(task["id"], policy, object())
    assert "conductor_2" in {
        n["node_key"] for n in conductor.graph.load_graph(task["id"])
    }


def test_discard_outage_is_rejection_evidence_not_absent_branch(
    feedback_db, monkeypatch
):
    import httpx

    task, policy = feedback_task()
    assert conductor._add(
        task, policy, "implement_spare", "unused", [], "luna", "spare", "not needed"
    ).ok
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "discard_node",
            "node_key": "implement_spare",
            "reason": "unneeded work",
        },
    )
    reads = []

    def outage(*args):
        reads.append(args)
        httpx.Response(
            503, request=httpx.Request("GET", "https://example.test/ref")
        ).raise_for_status()

    monkeypatch.setattr(conductor, "github_get", outage)
    before = conductor.graph.current_version(task["id"])
    for _ in range(2):
        conductor.apply_decision(
            task, policy, run, conductor.graph.node_runs(task["id"])
        )
    assert len(reads) == 1
    assert conductor.graph.current_version(task["id"]) == before
    assert (
        feedback_audits(feedback_db, task["id"])[0]["refusal_code"] == "github_http_503"
    )
    assert "implement_spare" in {
        n["node_key"] for n in conductor.graph.load_graph(task["id"])
    }


@pytest.mark.parametrize(
    "failure", ["missing_review", "wrong_branch", "pending_ci", "failed_ci"]
)
def test_premature_finish_is_processed_once_and_next_planner_gets_reason(
    feedback_db, monkeypatch, failure
):
    from swarm import factory_controls as controls

    task, policy = feedback_task()
    head = "a" * 40
    if failure in ("pending_ci", "failed_ci"):
        complete_feedback_node(
            task,
            policy,
            "implement_fix",
            {
                "status": "complete",
                "summary": "Implemented the change; CI still needs acceptance.",
                "pr_number": 3,
                "head_sha": head,
            },
            head=head,
        )
        complete_feedback_node(
            task,
            policy,
            "review_fix",
            {
                "verdict": "approve",
                "summary": "Reviewed the exact head; required CI remains separate.",
                "pr_number": 3,
                "head_sha": head,
            },
            head=head,
        )
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {"action": "finish", "reason": "done", "pr_number": 3},
    )
    reads = []

    def github(repo, path):
        assert repo == task["repo"]
        reads.append(path)
        if path == "pulls/3":
            return {
                "state": "open",
                "draft": False,
                "head": {
                    "sha": head,
                    "ref": "other"
                    if failure == "wrong_branch"
                    else f"factory/{task['id']}",
                    "repo": {"full_name": task["repo"]},
                },
                "base": {"ref": "main"},
            }
        assert path == f"commits/{head}/status"
        state = "pending" if failure == "pending_ci" else "failure"
        return {"state": state, "statuses": [{"context": "pr-checks", "state": state}]}

    monkeypatch.setattr(conductor, "github_get", github)
    before = controls.task_snapshot(task["id"])
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    first_read_count = len(reads)
    feedback_db.dispose()
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    assert len(reads) == first_read_count
    audit = feedback_audits(feedback_db, task["id"])
    assert len(audit) == 1 and audit[0]["refusal_code"] == "validation_failed"
    assert audit[0]["decision_action"] == "finish"
    assert conductor._decision_processed(task["id"], audit[0]["cause"])
    conductor.reconcile_task(task["id"], policy, object())
    next_planner = next(
        n
        for n in conductor.graph.load_graph(task["id"])
        if n["node_key"] == "conductor_2"
    )
    assert audit[0]["reason"] in next_planner["prompt"]
    after = controls.task_snapshot(task["id"])
    assert after["state"] == "admitted" and after["policy"] == before["policy"]
    assert after["turns_used"] == before["turns_used"]
    assert after["committed_cost_usd"] == before["committed_cost_usd"]


def test_graph_refusal_survives_audit_gap_and_reaches_next_planner(
    feedback_db, monkeypatch
):
    from sqlmodel import Session, select
    from swarm.models import SwarmConductorCall

    task, policy = feedback_task()
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "repair",
            "role": "implement",
            "prompt": "fix",
            "deps": ["missing_dependency"],
            "reason": "needed",
        },
    )
    reject = conductor._reject_decision

    def crash(*_args):
        raise RuntimeError("process lost after committed graph refusal")

    monkeypatch.setattr(conductor, "_reject_decision", crash)
    with pytest.raises(RuntimeError, match="committed graph refusal"):
        conductor.apply_decision(
            task, policy, run, conductor.graph.node_runs(task["id"])
        )
    monkeypatch.setattr(conductor, "_reject_decision", reject)
    feedback_db.dispose()
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    assert feedback_audits(feedback_db, task["id"]) == []
    with Session(feedback_db) as db:
        refused = db.exec(
            select(SwarmConductorCall).where(SwarmConductorCall.outcome == "refused")
        ).all()
        assert len(refused) == 1 and refused[0].refusal_code == "unknown_dep"
    conductor.reconcile_task(task["id"], policy, object())
    next_planner = next(
        n
        for n in conductor.graph.load_graph(task["id"])
        if n["node_key"] == "conductor_2"
    )
    assert "unknown_dep" in next_planner["prompt"]
    assert "missing_dependency" in next_planner["prompt"]


def test_graph_refusal_audit_is_idempotent_and_feedback_survives_large_context(
    feedback_db,
):
    task, policy = feedback_task()
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "repair",
            "role": "implement",
            "prompt": "fix",
            "deps": ["missing_dependency"],
            "reason": "needed",
        },
    )
    for _ in range(2):
        conductor.apply_decision(
            task, policy, run, conductor.graph.node_runs(task["id"])
        )
    audit = feedback_audits(feedback_db, task["id"])
    assert len(audit) == 1 and audit[0]["refusal_code"] == "unknown_dep"
    task["task_text"] = "x" * 100000
    prompt = conductor.planner_prompt(task, [], [])
    assert "unknown_dep" in prompt and "missing_dependency" in prompt


def test_repair_does_not_replenish_factory_turn_budget(feedback_db, monkeypatch):
    from swarm import factory_controls as controls

    task, policy = feedback_task(max_turns=1)
    # One work start spends the entire work cap. The planner round that follows
    # answers to its own cap, and it must not hand the repair a work turn back.
    complete_feedback_node(
        task,
        policy,
        "implement_once",
        {"status": "complete", "summary": "done", "pr_number": None, "head_sha": None},
    )
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "repair",
            "role": "implement",
            "prompt": "fix",
            "deps": [],
            "reason": "needed",
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": "a" * 40}}
    )
    conductor.reconcile_task(task["id"], policy, object())
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["turns_used"] == 1 and snapshot["planner_turns_used"] == 1
    assert snapshot["committed_cost_usd"] == 0.5
    assert snapshot["policy"]["max_turns_per_task"] == 1
    assert len(conductor.graph.node_runs(task["id"])) == 2
    # The repair never reaches the graph: sizing it against the envelope
    # refuses it with the excess, which is what the planner needs in order to
    # shrink the work rather than a silently pinned task.
    assert not any(
        node["node_key"] == "implement_repair"
        for node in conductor.graph.load_graph(task["id"])
    )
    audits = feedback_audits(feedback_db, task["id"])
    assert [audit["refusal_code"] for audit in audits] == ["envelope_exceeded"]
    assert '"allowed": 1' in audits[0]["reason"]


@pytest.mark.parametrize("later_planner", [False, True])
@pytest.mark.parametrize("later_graph_edit", [False, True])
def test_inserted_planner_decision_uses_its_own_committed_revision(
    feedback_db, monkeypatch, later_planner, later_graph_edit
):
    import json
    from swarm import factory_controls as controls

    task, policy = feedback_task()
    if later_planner:
        prior = complete_feedback_node(
            task,
            policy,
            "conductor_1",
            {
                "action": "add_node",
                "node_key": "conductor_forbidden",
                "role": "implement",
                "prompt": "not authorized",
                "deps": [],
                "reason": "needs a corrected decision",
            },
        )
        conductor.apply_decision(
            task, policy, prior, conductor.graph.node_runs(task["id"])
        )
    before_budget = conductor._budget_evidence(task["id"])
    before_receipt = controls.task_snapshot(task["id"])
    insertion_revision = conductor.graph.current_version(task["id"])
    planner_key = "conductor_2" if later_planner else "conductor_1"

    conductor.reconcile_task(task["id"], policy, object())
    planner = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == planner_key
    )
    context = json.loads(planner["prompt"].rsplit("\n", 1)[1])
    decision_revision = context["graph_revision"]
    assert decision_revision == insertion_revision + 1
    assert decision_revision == planner["created_in_version"]
    assert decision_revision == conductor.graph.current_version(task["id"])
    assert context["budget_evidence"] == before_budget
    assert context["budget_evidence"]["graph_revision"] == insertion_revision
    assert (
        context["budget_evidence"]["snapshot_phase"]
        == "before_this_planner_node_is_added_or_admitted"
    )
    assert controls.task_snapshot(task["id"])["starts"] == before_receipt["starts"]

    # The ordinary reconciliation owner reserves this exact frozen prompt.
    monkeypatch.setattr(conductor, "github_get", lambda *_args: {})
    conductor.reconcile_task(task["id"], policy, object())
    run = conductor.graph.node_runs(task["id"])[-1]
    assert run["node_key"] == planner_key and run["pin"]["prompt"] == planner["prompt"]
    armed_planner = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == planner_key
    )
    assert armed_planner["armed_at"] is not None
    assert {**armed_planner, "armed_at": None} == planner
    decision = {
        "action": "add_node",
        "node_key": "implement_revision_fix",
        "role": "implement",
        "prompt": "Fix the reported issue",
        "deps": [],
        "expected_version": decision_revision,
        "reason": "Use the revision advertised by the admitted planner",
    }
    result = {
        "status": "succeeded",
        "session_id": 202,
        "cost_usd": 0.25,
        "value": decision,
        "artifact": {"status": "ok", "value": decision, "errors": []},
        "cleanup": {"status": "completed"},
    }
    dbos = SimpleNamespace(
        get_workflow_status=lambda _key: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _key: SimpleNamespace(get_result=lambda: result),
    )
    conductor.reconcile_task(task["id"], policy, dbos)
    assert conductor.graph.node_runs(task["id"])[-1]["status"] == "succeeded"
    if later_graph_edit:
        assert conductor._add(
            task,
            policy,
            "implement_competing",
            "A separately authorized graph edit",
            [],
            "luna",
            "test:competing",
            "Competing edit after planner insertion",
            expected_version=decision_revision,
        ).ok
    conductor.reconcile_task(task["id"], policy, object())
    nodes = conductor.graph.load_graph(task["id"])
    applied = any(node["node_key"] == decision["node_key"] for node in nodes)
    assert applied is not later_graph_edit
    assert conductor.graph.current_version(task["id"]) == decision_revision + 1
    assert (
        next(node for node in nodes if node["node_key"] == planner_key) == armed_planner
    )
    feedback = feedback_audits(feedback_db, task["id"])
    if later_graph_edit:
        assert feedback[-1]["refusal_code"] == "stale_version"
    else:
        assert all(item["refusal_code"] != "stale_version" for item in feedback)
    after = controls.task_snapshot(task["id"])
    assert after["policy"] == before_receipt["policy"]
    assert after["deadline_at"] == before_receipt["deadline_at"]
    assert after["turns_used"] == before_receipt["turns_used"]
    assert after["planner_turns_used"] == before_receipt["planner_turns_used"] + 1
    assert after["committed_cost_usd"] == before_receipt["committed_cost_usd"] + 0.25


@pytest.mark.parametrize("edit_during", ["graph_read", "prompt_construction"])
def test_planner_insertion_refuses_edit_that_races_with_its_snapshot(
    feedback_db, monkeypatch, edit_during
):
    import json
    from sqlmodel import Session, select
    from swarm import factory_controls as controls
    from swarm.models import SwarmConductorCall

    task, policy = feedback_task()
    before = controls.task_snapshot(task["id"])
    load_graph = conductor.graph.load_graph
    prompt = conductor.planner_prompt

    def competing_edit():
        assert conductor._add(
            task,
            policy,
            "implement_competing",
            "A separately authorized graph edit",
            [],
            "luna",
            "test:competing",
            "Competing edit before planner insertion",
            expected_version=0,
        ).ok

    def changed_graph(*args, **kwargs):
        result = load_graph(*args, **kwargs)
        competing_edit()
        return result

    def changed_prompt(*args, **kwargs):
        result = prompt(*args, **kwargs)
        competing_edit()
        return result

    with monkeypatch.context() as race:
        if edit_during == "graph_read":
            race.setattr(conductor.graph, "load_graph", changed_graph)
        else:
            race.setattr(conductor, "planner_prompt", changed_prompt)
        conductor.reconcile_task(task["id"], policy, object())
    assert [node["node_key"] for node in load_graph(task["id"])] == [
        "implement_competing"
    ]
    assert conductor.graph.current_version(task["id"]) == 1
    after = controls.task_snapshot(task["id"])
    assert after["task_paused"]
    for field in (
        "policy",
        "deadline_at",
        "starts",
        "turns_used",
        "committed_cost_usd",
    ):
        assert after[field] == before[field]
    with Session(feedback_db) as db:
        calls = db.exec(
            select(SwarmConductorCall).order_by(SwarmConductorCall.id)
        ).all()
        assert len(calls) == 2
        refused = calls[-1]
        assert refused.outcome == "refused" and refused.refusal_code == "stale_version"
        assert refused.version_before == refused.version_after == 1
        assert json.loads(refused.args_json)["expected_version"] == 0
    # A paused reconciliation does not retry against the now-current revision.
    conductor.reconcile_task(task["id"], policy, object())
    with Session(feedback_db) as db:
        assert len(db.exec(select(SwarmConductorCall)).all()) == 2


@pytest.mark.parametrize("overflow", [True, False])
def test_planner_context_overflow_pauses_without_inserting_or_starting(
    feedback_db, monkeypatch, overflow
):
    from sqlmodel import Session, select
    from swarm import factory_controls as controls
    from swarm.models import SwarmConductorCall

    task, policy = feedback_task()
    before = controls.task_snapshot(task["id"])

    def context_failure(*args):
        error = conductor.PlannerContextOverflow if overflow else ValueError
        raise error("required planner evidence cannot fit")

    monkeypatch.setattr(conductor, "_planner_context", context_failure)
    if overflow:
        conductor.reconcile_task(task["id"], policy, object())
    else:
        with pytest.raises(ValueError, match="required planner evidence"):
            conductor.reconcile_task(task["id"], policy, object())
    assert conductor.graph.current_version(task["id"]) == 0
    assert conductor.graph.load_graph(task["id"]) == []
    assert conductor.graph.node_runs(task["id"]) == []
    after = controls.task_snapshot(task["id"])
    assert after["task_paused"] is overflow
    for field in (
        "policy",
        "deadline_at",
        "starts",
        "turns_used",
        "committed_cost_usd",
    ):
        assert after[field] == before[field]
    with Session(feedback_db) as db:
        assert db.exec(select(SwarmConductorCall)).all() == []


def test_new_graph_refusal_is_not_hidden_by_older_audit_feedback(feedback_db):
    import json
    from datetime import datetime, timedelta, timezone
    from sqlmodel import Session
    from swarm.factory_models import FactoryAudit

    task, policy = feedback_task()
    older = datetime.now(timezone.utc) - timedelta(hours=1)
    with Session(feedback_db) as db:
        for number in range(conductor.DECISION_EVIDENCE_LIMIT):
            db.add(
                FactoryAudit(
                    actor="factory:reconciler",
                    action="conductor_rejected",
                    task_id=task["id"],
                    created_at=older,
                    detail_json=json.dumps(
                        {
                            "cause": f"old:{number}",
                            "decision_action": "finish",
                            "refusal_code": "validation_failed",
                            "reason": "old rejection",
                        }
                    ),
                )
            )
        db.commit()
    result = conductor._add(
        task,
        policy,
        "implement_fix",
        "fix",
        ["missing"],
        "luna",
        "new-refusal",
        "needed",
    )
    assert not result.ok and result.refusal_code == "unknown_dep"
    evidence = conductor._decision_evidence(task["id"])
    assert len(evidence) == conductor.DECISION_EVIDENCE_LIMIT
    assert (
        evidence[0]["cause"] == "new-refusal"
        and evidence[0]["refusal_code"] == "unknown_dep"
    )


def planner_context(prompt):
    import json

    return json.loads(prompt.split("\n", 1)[1])


@pytest.mark.parametrize(
    "selected_profile",
    [pytest.param("absent", id="absent"), None, "explicit"],
)
def test_planner_run_falls_back_for_absent_or_null_selected_profile(selected_profile):
    import json

    pin = {"model": "immutable-profile"}
    if selected_profile != "absent":
        pin["selected_profile"] = selected_profile
    run = {
        "pin": pin,
        "outcome_json": json.dumps({"artifact": {"status": "invalid", "errors": []}}),
    }

    result = conductor._planner_run(run)

    expected_profile = (
        "immutable-profile" if selected_profile in ("absent", None) else "explicit"
    )
    assert result["selected_profile"] == expected_profile
    assert result["provider_model"] == "unavailable"


def test_planner_keeps_completed_review_after_recursive_historical_prompts(monkeypatch):
    import copy
    import json

    task, delivery_runs = delivery(monkeypatch)
    task["task_text"] = "Fix the reported cap and independently review the exact PR."
    nested = "historical-prompt-must-not-return" * 100
    runs = []
    nodes = []
    for ordinal, key in enumerate(
        ("conductor_1", "implement_fix", "conductor_2", "review_fix", "conductor_3"),
        1,
    ):
        value = {"action": "add_node", "prompt": nested, "reason": "next bounded step"}
        run = {
            "id": ordinal,
            "node_key": key,
            "attempt": 1,
            "status": "succeeded",
            "session_id": 2797 + ordinal,
            "cost_usd": 0.25,
            "reserved_cost_usd": 5,
            "accounted_cost_usd": 0.25,
            "accounting_basis": "observed",
            "head_sha": None,
        }
        if key in ("implement_fix", "review_fix"):
            original = delivery_runs[0 if key == "implement_fix" else 1]
            value = json.loads(original["outcome_json"])["value"]
            value["summary"] = "Fix checked against the task's acceptance criteria."
            run["head_sha"] = "a" * 40
        run["pin"] = {
            "prompt": nested,
            "artifact_schema": {"description": nested},
            "retry_context": nested,
            "model": "opus" if key != "implement_fix" else "luna",
            "workflow_id": f"factory-node:t-1:{key}:1",
        }
        run["outcome_json"] = json.dumps(
            {
                "value": value,
                "artifact": {"status": "ok", "value": value, "errors": []},
                "raw_detail": nested,
            }
        )
        runs.append(run)
        nodes.append(
            {
                "node_key": key,
                "prompt": nested,
                "kind": "work",
                "deps": ["implement_fix"] if key == "review_fix" else [],
                "max_attempts": 2,
                "max_cost_usd": 5,
                "turn_timeout_seconds": 900,
            }
        )
        nested = json.dumps({"graph": nodes, "runs": runs})[:200_000]
    before = copy.deepcopy((task, nodes, runs))
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])
    prompt = conductor.planner_prompt(task, nodes, runs)
    context = planner_context(prompt)
    review = context["delivery_evidence"]["latest_review"]
    assert review["id"] == 4 and review["session_id"] == 2801
    assert review["model"] == "opus" and review["status"] == "succeeded"
    assert review["head_sha"] == review["artifact"]["head_sha"] == "a" * 40
    assert review["artifact"]["verdict"] == "approve"
    assert review["artifact"]["pr_number"] == 3
    assert context["delivery_evidence"]["latest_implement"]["model"] == "luna"
    assert context["graph"][3]["deps"] == ["implement_fix"]
    assert context["graph"][3]["max_attempts"] == 2
    assert context["graph"][3]["latest_status"] == "succeeded"
    assert context["runs"][1]["accounted_cost_usd"] == 0.25
    assert context["runs"][1]["selected_profile"] == "luna"
    assert context["runs"][1]["provider_model"] == "unavailable"
    for excluded in (
        "historical-prompt-must-not-return",
        '"pin"',
        '"prompt"',
        '"artifact_schema"',
        '"outcome_json"',
        '"raw_detail"',
    ):
        assert excluded not in prompt
    assert len(prompt.split("\n", 1)[1]) <= conductor.PLANNER_CONTEXT_CHARS
    # The shrink loop bounds the JSON context; the instruction preamble rides
    # on top of it and nothing bounds that, so this is the guard on preamble
    # growth. The preamble is about 9,300 characters and this case's context
    # about 6,700. It moved once, from 16,000, when pause gained its option
    # contract (#6041): roughly 1,000 characters, and cheap against the
    # planner round a refused pause costs. Move it again only for a rule the
    # planner cannot follow without being told, and say which rule.
    assert len(prompt) < 17_000
    assert (task, nodes, runs) == before


def test_planner_limits_complete_json_without_losing_older_delivery_evidence(
    monkeypatch,
):
    import copy
    import json

    task, runs = delivery(monkeypatch)
    task["task_text"] = '"\\\n\u2603' * 20_000
    long_text = '"\\\n\u2603' * 1000
    for run in runs:
        outcome = json.loads(run["outcome_json"])
        outcome["value"]["summary"] = long_text
        run["outcome_json"] = json.dumps(outcome)
    # Completed review is older than the recent-run window. Its exact evidence
    # must survive even when later planner history forces collection trimming.
    for ordinal in range(3, 100):
        runs.append(
            {
                "id": ordinal,
                "node_key": f"conductor_{ordinal}",
                "attempt": 1,
                "status": "failed",
                "session_id": 100 + ordinal,
                "pin": {"model": "opus", "prompt": long_text},
                "outcome_json": json.dumps({"reason": long_text}),
            }
        )
    nodes = [
        {
            "node_key": f"conductor_{i}",
            "deps": [f"dependency_{n}" for n in range(20)],
            "prompt": long_text,
            "max_attempts": 2,
            "max_cost_usd": 5,
        }
        for i in range(100)
    ]
    feedback = [
        {
            "cause": f"decision:{i}",
            "refusal_code": "validation_failed",
            "reason": long_text,
        }
        for i in range(20)
    ]
    before = copy.deepcopy((task, nodes, runs, feedback))
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: feedback)
    prompt = conductor.planner_prompt(task, nodes, runs)
    context = planner_context(prompt)
    assert len(prompt.split("\n", 1)[1].encode()) <= conductor.PLANNER_CONTEXT_CHARS
    assert (
        context["delivery_evidence"]["latest_review"]["artifact"]["verdict"]
        == "approve"
    )
    assert context["delivery_evidence"]["latest_review"]["session_id"] == 11
    assert (
        context["delivery_evidence"]["latest_implement"]["artifact"]["pr_number"] == 3
    )
    assert context["runs"][-1]["id"] == 99
    assert context["decision_feedback"][0]["cause"] == "decision:0"
    assert context["omitted"]["task_characters"] > 0
    assert context["omitted"]["run_records"] == len(runs) - len(context["runs"])
    assert context["omitted"]["graph_records"] == len(nodes) - len(context["graph"])
    for role in ("latest_implement", "latest_review"):
        assert context["delivery_evidence"][role]["summary_complete"] is True
        assert context["delivery_evidence"][role]["artifact"]["summary"] == long_text
    assert (task, nodes, runs, feedback) == before


def test_planner_preserves_later_review_rejection_and_recorded_head_mismatch(
    monkeypatch,
):
    import copy
    import json

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Deliver a reviewed correction."
    later = copy.deepcopy(runs[-1])
    later["id"] = 3
    later["session_id"] = 12
    outcome = json.loads(later["outcome_json"])
    outcome["value"].update(verdict="changes_requested", head_sha="b" * 40)
    later["outcome_json"] = json.dumps(outcome)
    runs.append(later)
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])
    review = planner_context(conductor.planner_prompt(task, [], runs))[
        "delivery_evidence"
    ]["latest_review"]
    assert review["id"] == 3 and review["session_id"] == 12
    assert review["artifact"]["verdict"] == "changes_requested"
    assert review["artifact"]["head_sha"] == "b" * 40
    assert review["head_sha"] == "a" * 40


def test_planner_keeps_unknown_execution_and_missing_cost_distinct(monkeypatch):
    import json

    task = {"id": "t-1", "task_text": "Complete the task."}
    runs = [
        {
            "id": 1,
            "node_key": "implement_fix",
            "attempt": 1,
            "status": "uncertain",
            "session_id": None,
            "cost_usd": None,
            "reserved_cost_usd": 5,
            "accounted_cost_usd": 5,
            "accounting_basis": "reserved",
            "pin": {"model": "luna"},
            "outcome_json": json.dumps(
                {"reason": "observer lost; execution may continue"}
            ),
        }
    ]
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])
    context = planner_context(conductor.planner_prompt(task, [], runs))
    run = context["runs"][0]
    assert context["delivery_evidence"]["latest_implement"] is None
    assert run["status"] == "uncertain" and run["session_id"] is None
    assert run["cost_usd"] is None and run["accounted_cost_usd"] == 5
    assert run["reason"] == "observer lost; execution may continue"


@pytest.mark.parametrize("text", ["\U0001f600" * 1000, '"\\\n' * 1000])
def test_planner_bounds_encoded_protected_text_without_losing_review(monkeypatch, text):
    import json

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Fix and review this issue."
    for run in runs:
        outcome = json.loads(run["outcome_json"])
        outcome["value"]["summary"] = text
        run["outcome_json"] = json.dumps(outcome)
    monkeypatch.setattr(
        conductor,
        "_decision_evidence",
        lambda _task: [
            {
                "cause": "factory-decision:conductor_3:1",
                "refusal_code": "validation_failed",
                "reason": text,
            }
        ],
    )
    prompt = conductor.planner_prompt(task, [], runs)
    context = planner_context(prompt)
    review = context["delivery_evidence"]["latest_review"]
    assert len(prompt.split("\n", 1)[1].encode()) <= conductor.PLANNER_CONTEXT_CHARS
    assert review["session_id"] == 11 and review["model"] == "opus"
    assert review["head_sha"] == review["artifact"]["head_sha"] == "a" * 40
    assert review["artifact"]["verdict"] == "approve"
    assert review["artifact"]["pr_number"] == 3
    assert context["decision_feedback"][0]["refusal_code"] == "validation_failed"
    assert context["omitted"]["task_characters"] == 0
    assert context["omitted"]["run_records"] == 0
    assert context["task"] == task["task_text"]
    assert review["summary_complete"] is True
    assert review["artifact"]["summary"] == text
    for value in (
        context["runs"][-1]["artifact"]["summary"],
        context["decision_feedback"][0]["reason"],
    ):
        assert value.endswith(" [text omitted]")
        assert len(json.dumps(value)) <= conductor.PLANNER_TEXT_CHARS


def test_planner_preserves_review_findings_after_long_preamble(monkeypatch):
    import copy
    import json

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Complete every requested review correction."
    findings = "BLOCKER: handle terminal readback. BLOCKER: test retained ownership."
    summary = "Design observations. " * 350 + findings
    outcome = json.loads(runs[-1]["outcome_json"])
    outcome["value"].update(verdict="changes_requested", summary=summary)
    assert not schema_errors(outcome["value"], conductor.REVIEW_SCHEMA)
    runs[-1]["outcome_json"] = json.dumps(outcome)
    before = copy.deepcopy(runs)
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])

    context = planner_context(conductor.planner_prompt(task, [], runs))

    review = context["delivery_evidence"]["latest_review"]
    assert review["id"] == 2 and review["session_id"] == 11
    assert review["summary_complete"] is True
    assert review["artifact"]["verdict"] == "changes_requested"
    assert review["artifact"]["summary"] == summary
    assert review["artifact"]["summary"].endswith(findings)
    assert context["runs"][-1]["artifact"]["summary"].endswith(" [text omitted]")
    assert "summary_complete" not in context["runs"][-1]
    assert runs == before


@pytest.mark.parametrize(
    "summary",
    [
        "\U0001f600" * 7980 + "FINAL BLOCKER",
        "\u2603" * 1500 + "\ud800" + r"\ud800" + "FINAL BLOCKER",
        '"\\\n' * 2000 + "FINAL BLOCKER",
    ],
)
def test_planner_keeps_complete_unicode_review_as_valid_bounded_json(
    monkeypatch, summary
):
    import json

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Resolve the final review finding."
    outcome = json.loads(runs[-1]["outcome_json"])
    outcome["value"].update(verdict="changes_requested", summary=summary)
    assert not schema_errors(outcome["value"], conductor.REVIEW_SCHEMA)
    runs[-1]["outcome_json"] = json.dumps(outcome)
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])

    prompt = conductor.planner_prompt(task, [], runs, decision_revision=7)
    context = planner_context(prompt)

    assert (
        len(prompt.split("\n", 1)[1].encode("utf-8")) <= conductor.PLANNER_CONTEXT_CHARS
    )
    assert (
        context["delivery_evidence"]["latest_review"]["artifact"]["summary"] == summary
    )
    assert context["delivery_evidence"]["latest_review"]["summary_complete"] is True
    assert context["graph_revision"] == 7


def test_planner_refuses_overflow_instead_of_clipping_required_summaries(monkeypatch):
    import copy
    import json

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Preserve review findings."
    for run in runs:
        outcome = json.loads(run["outcome_json"])
        outcome["value"]["summary"] = "\U0001f600" * 8000
        runs_schema = (
            conductor.REVIEW_SCHEMA
            if run["node_key"].startswith("review_")
            else conductor.RESULT_SCHEMA
        )
        assert not schema_errors(outcome["value"], runs_schema)
        run["outcome_json"] = json.dumps(outcome)
    before = copy.deepcopy((task, runs))
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])

    with pytest.raises(conductor.PlannerContextOverflow, match="context limit"):
        conductor.planner_prompt(task, [], runs)

    assert (task, runs) == before


@pytest.mark.parametrize("status", ["failed", "uncertain"])
@pytest.mark.parametrize(
    "value",
    [
        {
            "action": "add_node",
            "reason": "repair",
            "node_key": "fix",
            "role": "implement",
            "prompt": "Fix the issue",
            "deps": None,
        },
        ["not-an-artifact-object"],
        None,
    ],
)
def test_planner_reports_actual_invalid_evaluator_output(monkeypatch, status, value):
    import copy
    import json
    from dataclasses import asdict
    from swarm.turn_artifact import evaluate_content

    evaluated = evaluate_content(
        json.dumps(value), ".factory/decision.json", conductor.DECISION_SCHEMA
    )
    assert evaluated.status == "invalid" and evaluated.value == value
    reason = "artifact_invalid: " + "; ".join(evaluated.errors)
    run = {
        "id": 1,
        "node_key": "conductor_1",
        "attempt": 1,
        "status": status,
        "session_id": 2804,
        "pin": {"model": "opus"},
        "reserved_cost_usd": 5,
        "accounted_cost_usd": 5,
        "cost_usd": None,
        "outcome_json": json.dumps(
            {
                "status": status,
                "artifact": asdict(evaluated),
                "value": evaluated.value,
                "reason": reason,
            }
        ),
    }
    before = copy.deepcopy(run)
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])
    prompt = conductor.planner_prompt(
        {"id": "t-1", "task_text": "Fix the reported issue."}, [], [run]
    )
    context = planner_context(prompt)
    projected = context["runs"][0]
    assert projected["status"] == status
    assert projected["reason"] == reason
    assert projected["session_id"] == 2804
    assert projected["artifact"] == {}
    assert projected["artifact_validation"]["status"] == "invalid"
    assert projected["artifact_validation"]["errors"] == evaluated.errors
    assert context["delivery_evidence"] == {
        "latest_implement": None,
        "latest_review": None,
    }
    assert len(prompt.split("\n", 1)[1].encode()) <= conductor.PLANNER_CONTEXT_CHARS
    assert run == before


@pytest.mark.parametrize("status", ["failed", "succeeded"])
def test_planner_does_not_promote_invalid_review_or_crash_on_structured_reason(
    monkeypatch,
    status,
):
    import json
    from dataclasses import asdict
    from swarm.turn_artifact import evaluate_content

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Deliver an independently reviewed change."
    invalid = {
        "verdict": "approve",
        "pr_number": 3,
        "head_sha": "a" * 40,
        "summary": "Looks good",
        "deps": None,
    }
    evaluated = evaluate_content(
        json.dumps(invalid), ".factory/review.json", conductor.REVIEW_SCHEMA
    )
    assert evaluated.status == "invalid"
    runs[-1]["status"] = status
    runs[-1]["outcome_json"] = json.dumps(
        {
            "artifact": asdict(evaluated),
            "value": invalid,
            "reason": {"legacy": "structured reason"},
        }
    )
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])
    context = planner_context(conductor.planner_prompt(task, [], runs))
    assert context["delivery_evidence"]["latest_review"] is None
    review = context["runs"][-1]
    assert (
        review["artifact"] == {}
        and review["artifact_validation"]["status"] == "invalid"
    )
    assert review["reason"] == "invalid structured reason"


def test_planner_uses_captured_validation_without_reinterpreting_pinned_schema(
    monkeypatch,
):
    import json
    from dataclasses import asdict
    from swarm.turn_artifact import evaluate_content

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Deliver the reviewed change."
    value = json.loads(runs[-1]["outcome_json"])["value"]
    value["historical_note"] = "accepted by the pinned schema"
    pinned_schema = {**conductor.REVIEW_SCHEMA, "additionalProperties": True}
    evaluated = evaluate_content(
        json.dumps(value), ".factory/review.json", pinned_schema
    )
    assert evaluated.status == "ok"
    assert schema_errors(value, conductor.REVIEW_SCHEMA)
    runs[-1]["pin"]["artifact_schema"] = pinned_schema
    runs[-1]["outcome_json"] = json.dumps(
        {"value": value, "artifact": asdict(evaluated)}
    )
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task: [])
    context = planner_context(conductor.planner_prompt(task, [], runs))
    review = context["delivery_evidence"]["latest_review"]
    assert review["artifact_validation"]["status"] == "ok"
    assert review["artifact"]["verdict"] == "approve"
    assert review["artifact"]["head_sha"] == "a" * 40
    assert "historical_note" not in review["artifact"]


def test_refused_356_then_applied_357_remains_timestamped_history(feedback_db):
    import json
    from datetime import datetime
    from sqlmodel import Session, select
    from swarm.models import SwarmConductorCall

    task, policy = feedback_task()
    refused_at = datetime(2026, 9, 7, 14, 20, 16)
    cause = "factory-plan:conductor_4"
    with Session(feedback_db) as db:
        db.add(
            SwarmConductorCall(
                id=356,
                task_id=task["id"],
                conductor_model="opus",
                tool="add_node",
                args_json=json.dumps({"cause_ref": cause, "node_key": "conductor_4"}),
                outcome="refused",
                refusal_code="budget_exceeded",
                created_at=refused_at,
                version_before=0,
                version_after=0,
            )
        )
        db.commit()
    # The real graph owner creates the successful call and plan version.
    assert conductor._add(
        task, policy, "conductor_4", "next", [], "opus", cause, "retry after fix"
    ).ok
    with Session(feedback_db) as db:
        applied = db.get(SwarmConductorCall, 357)
        applied.created_at = datetime(2026, 9, 7, 15, 12, 27)
        db.add(applied)
        db.commit()
        before = [r.model_dump() for r in db.exec(select(SwarmConductorCall)).all()]
    evidence = conductor._decision_evidence(task["id"])
    assert len(evidence) == 1
    item = evidence[0]
    assert item["graph_call_id"] == 356 and item["refusal_code"] == "budget_exceeded"
    assert (
        item["evidence_state"] == "superseded" and item["superseded_by_call_id"] == 357
    )
    assert item["recorded_at"] == refused_at.isoformat()
    assert item["applied_at"] == "2026-09-07T15:12:27" and item["applied_version"] == 1
    context = planner_context(
        conductor.planner_prompt(task, conductor.graph.load_graph(task["id"]), [])
    )
    assert {key: context["decision_feedback"][0][key] for key in item} == item
    with Session(feedback_db) as db:
        assert [
            r.model_dump() for r in db.exec(select(SwarmConductorCall)).all()
        ] == before


@pytest.mark.parametrize(
    "difference",
    [
        "cause",
        "operation",
        "node_key",
        "earlier",
        "missing_identity",
        "no_version_change",
    ],
)
def test_refusal_requires_later_success_of_exact_edit(feedback_db, difference):
    import json
    from datetime import datetime, timedelta
    from sqlmodel import Session
    from swarm.models import SwarmConductorCall

    task, _policy = feedback_task()
    args = {"cause_ref": "decision:4", "node_key": "implement_fix"}
    later_args = dict(args)
    tool = "add_node"
    stamp = datetime(2026, 9, 7, 14, 20)
    later_stamp = stamp + timedelta(minutes=1)
    if difference == "cause":
        later_args["cause_ref"] = "decision:5"
    elif difference == "operation":
        tool = "discard_node"
    elif difference == "node_key":
        later_args["node_key"] = "implement_other"
    elif difference == "earlier":
        later_stamp = stamp - timedelta(minutes=1)
    elif difference == "missing_identity":
        args["cause_ref"] = later_args["cause_ref"] = None
    with Session(feedback_db) as db:
        db.add(
            SwarmConductorCall(
                id=356,
                task_id=task["id"],
                conductor_model="opus",
                tool="add_node",
                args_json=json.dumps(args),
                outcome="refused",
                refusal_code="budget_exceeded",
                created_at=stamp,
                version_before=6,
                version_after=6,
            )
        )
        db.add(
            SwarmConductorCall(
                id=357,
                task_id=task["id"],
                conductor_model="opus",
                tool=tool,
                args_json=json.dumps(later_args),
                outcome="applied",
                created_at=later_stamp,
                version_before=6,
                version_after=6 if difference == "no_version_change" else 7,
            )
        )
        db.commit()
    item = conductor._decision_evidence(task["id"])[0]
    assert item["evidence_state"] == "refused" and "superseded_by_call_id" not in item


def test_newer_refusal_survives_old_success_and_audit(feedback_db):
    import json
    from datetime import datetime, timedelta
    from sqlmodel import Session
    from swarm.models import SwarmConductorCall
    from swarm.factory_models import FactoryAudit

    task, _policy = feedback_task()
    stamp = datetime(2026, 9, 7, 14, 20)
    args = json.dumps({"cause_ref": "decision:4", "node_key": "implement_fix"})
    with Session(feedback_db) as db:
        for number, outcome in ((356, "refused"), (357, "applied"), (358, "refused")):
            db.add(
                SwarmConductorCall(
                    id=number,
                    task_id=task["id"],
                    conductor_model="opus",
                    tool="add_node",
                    args_json=args,
                    outcome=outcome,
                    refusal_code="budget_exceeded" if outcome == "refused" else None,
                    created_at=stamp + timedelta(seconds=number - 356),
                    version_before=6,
                    version_after=7 if outcome == "applied" else 6,
                )
            )
        db.add(
            FactoryAudit(
                actor="factory:reconciler",
                action="conductor_rejected",
                task_id=task["id"],
                created_at=stamp + timedelta(microseconds=1),
                detail_json=json.dumps(
                    {
                        "cause": "decision:4",
                        "decision_action": "add_node",
                        "refusal_code": "budget_exceeded",
                        "reason": "old refusal detail",
                    }
                ),
            )
        )
        db.commit()
    evidence = conductor._decision_evidence(task["id"])
    assert len(evidence) == 2
    assert (
        evidence[0]["graph_call_id"] == 358
        and evidence[0]["evidence_state"] == "refused"
    )
    assert (
        evidence[1]["graph_call_id"] == 356
        and evidence[1]["evidence_state"] == "superseded"
    )
    assert evidence[1]["reason"] == "old refusal detail"


def test_budget_projection_shares_admission_accounting_and_retry_ceiling(feedback_db):
    from swarm import factory_controls as controls

    task, policy = feedback_task()
    known = complete_feedback_node(
        task,
        policy,
        "implement_done",
        {"status": "complete", "summary": "done", "pr_number": None, "head_sha": None},
    )
    for key in ("implement_retry", "implement_unknown", "implement_active"):
        assert conductor._add(
            task, policy, key, "bounded", [], "luna", "test:" + key, "test"
        ).ok
        admitted = conductor.graph.admit_dispatch(task["id"], key)
        assert admitted.ok and admitted.pin["max_cost_usd"] == 2
        if key == "implement_retry":
            assert conductor.graph.record_outcome(
                task["id"], key, 1, "failed", 0.5, None, "{}"
            ).ok
            retry = conductor.graph.admit_dispatch(task["id"], key)
            assert retry.ok and retry.pin["max_cost_usd"] == 1.5
            assert conductor.graph.record_outcome(
                task["id"], key, 2, "uncertain", None, None, "{}"
            ).ok
        elif key == "implement_unknown":
            assert conductor.graph.record_outcome(
                task["id"], key, 1, "succeeded", None, None, "{}"
            ).ok
    before = conductor.graph.node_runs(task["id"])
    projection = conductor._budget_evidence(task["id"])
    # Known success: .25. Failed .5 + uncertain retry1.5 share one2 ceiling.
    # Unknown-cost success charges2. Active attempt reserves2. Never2*attempts.
    assert projection["accounted_cost_usd"] == 6.25
    assert projection["planned_cost_usd"] == 6.25
    assert projection["active_accounted_cost_usd"] == 3.5
    assert projection["settled_accounted_cost_usd"] == 2.75
    assert projection["active_attempts"] == 2 and projection["uncertain_attempts"] == 1
    assert projection["unallocated_cost_usd"] == 23.75
    assert projection["new_node_max_cost_usd"] == 2 and projection["max_attempts"] == 2
    assert projection["pending_planner_max_cost_usd"] == 2
    assert (
        projection["turns_used"] == 1
        and projection["planner_turns_used"] == 0
        and projection["max_task_turns_hard"] == policy["max_turns_per_task"]
        and projection["task_turn_allowance"] == policy["max_turns_per_task"]
        and projection["allowance_derived_from_plan"] is False
        and projection["max_parallel_nodes"] == 1
    )
    assert (
        projection["deadline_at"] == controls.task_snapshot(task["id"])["deadline_at"]
    )
    context = planner_context(conductor.planner_prompt(task, [], []))
    assert context["budget_evidence"] == projection
    assert conductor.graph.node_runs(task["id"]) == before
    assert known["cost_usd"] == 0.25
    assert (
        "never multiply the ceiling by the attempt count"
        in conductor.planner_prompt(task, [], [])
    )


@pytest.fixture
def queued_factory(feedback_db, monkeypatch):
    from sqlmodel import Session, SQLModel
    import core.db
    from agent_sessions.models import (
        AgentSession,
        AgentTurn,
        PendingMessage,
        AgentCapacityPool,
        AgentCapacityReservation,
    )

    engine = feedback_db.execution_options(
        schema_translate_map={"swarm": None, "agent_sessions": None}
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                AgentSession,
                AgentTurn,
                PendingMessage,
                AgentCapacityPool,
                AgentCapacityReservation,
            )
        ],
    )
    for module in (conductor, conductor.graph, core.db):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    task, policy = feedback_task()
    key = "conductor_1"
    workflow = f"factory-node:{task['id']}:{key}:1"
    assert conductor._add(
        task, policy, key, "plan", [], "opus", "test:queue", "queue test"
    ).ok
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task['id']}",
        "hydration_branch": "main",
        "workflow_id": workflow,
        "artifact_path": ".factory/plan.json",
        "artifact_schema": conductor.DECISION_SCHEMA,
    }
    assert conductor.reserve_node(task["id"], key, workflow, context)
    run = conductor.graph.node_runs(task["id"])[0]
    with Session(engine) as db:
        owner = AgentSession(
            local_session_id=f"factory:{task['id']}:{key}:1",
            workspace="guest",
            branch="main",
            repo=task["repo"],
            model="opus",
            workflow_id=workflow,
            node_key=key,
            node_attempt=1,
            admission_tier="project",
        )
        db.add(owner)
        db.flush()
        sid = owner.id
        db.add(
            PendingMessage(
                session_id=sid, seq=1, message_text="queued planner", model="opus"
            )
        )
        db.commit()
    return SimpleNamespace(
        engine=engine, task=task, policy=policy, run=run, sid=sid, context=context
    )


@pytest.fixture
def not_invoked_factory(queued_factory, monkeypatch, request):
    import json
    from sqlmodel import Session, SQLModel, select
    from agent_sessions import admission, store
    from agent_sessions.models import (
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from swarm import factory_controls as controls
    from swarm import factory_supervision, node_workflows

    s = queued_factory
    SQLModel.metadata.create_all(s.engine, tables=[AgentResultReceipt.__table__])
    for module in (admission, store, controls):
        monkeypatch.setattr(module, "get_engine", lambda: s.engine)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "false")

    def unexpected(*_args, **_kwargs):
        pytest.fail("typed outcome reconciliation must not invoke or clean up")

    monkeypatch.setattr(factory_supervision, "_http", unexpected)
    monkeypatch.setattr(node_workflows, "_cleanup_node", unexpected)
    monkeypatch.setattr(node_workflows, "_read_reconciliation_head", unexpected)
    owner = "original-executor"
    assert store.claim_pending_message_for_session_sync(s.sid, owner) == 1
    assert admission.recheck(s.sid, 1, owner, "claude-runtime")
    # The text the session owner recorded for the failure. The default is a
    # generic one; a test that needs the control plane's own refusal passes the
    # status line the transport raises, which is how a denial is recognised.
    store.mark_turn_error_sync(
        s.sid,
        1,
        getattr(request, "param", "create failed before invoke"),
        owner,
        invocation_not_attempted=True,
        dispatch_count=1,
    )
    with Session(s.engine) as db:
        turn = db.exec(select(AgentTurn)).one()
        permit = db.exec(select(AgentCapacityReservation)).one()
        assert turn.cost_usd is None
        assert (
            json.loads(turn.usage_json)["recovery"]["invocation_phase"] == "not_invoked"
        )
        assert permit.state == "settled" and permit.outcome == "not_invoked"
        assert db.exec(select(PendingMessage)).first() is None
    s.result = {
        "status": "uncertain",
        "reason": "terminal reason does not confirm clean completion; reconcile before retry",
        "session_id": s.sid,
        "cost_usd": None,
    }

    def state(workflow):
        assert workflow == s.run["pin"]["workflow_id"]
        return SimpleNamespace(status="SUCCESS")

    s.dbos = SimpleNamespace(
        get_workflow_status=state,
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: s.result),
        start_workflow=unexpected,
        cancel_workflow=unexpected,
    )

    def native_snapshot():
        with Session(s.engine) as db:
            return {
                model.__tablename__: [
                    row.model_dump() for row in db.exec(select(model))
                ]
                for model in (
                    AgentSession,
                    AgentTurn,
                    PendingMessage,
                    AgentCapacityReservation,
                    AgentResultReceipt,
                )
            }

    s.native_snapshot = native_snapshot
    return s


def _persist_uncertain_not_invoked(s):
    import json
    from swarm import factory_controls as controls

    assert conductor.graph.record_dispatch(
        s.task["id"], s.run["node_key"], 1, s.sid, None
    ).ok
    assert conductor.graph.record_outcome(
        s.task["id"],
        s.run["node_key"],
        1,
        "uncertain",
        None,
        None,
        json.dumps(s.result),
    ).ok
    assert controls.record_start_outcome(
        s.task["id"],
        s.run["dispatch_key"],
        "uncertain",
        "original-reconciler",
        session_id=s.sid,
    )["ok"]
    s.run = conductor.graph.node_runs(s.task["id"])[0]


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("bound_guest", [False, True, "prepared_receipt"])
@pytest.mark.parametrize("supervision", [False, True])
def test_not_invoked_settles_actual_factory_path_at_zero_without_cleanup(
    not_invoked_factory, monkeypatch, historical, bound_guest, supervision
):
    import json
    from datetime import datetime, timedelta, timezone
    from sqlmodel import Session
    from agent_sessions.models import AgentResultReceipt, AgentSession
    from swarm import factory_controls as controls

    s = not_invoked_factory
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", str(supervision).lower())
    if bound_guest:
        with Session(s.engine) as db:
            agent = db.get(AgentSession, s.sid)
            agent.ember_session_id = "created-before-post-setup-failed"
            agent.ember_session_token = "preserved-test-token"
            agent.ember_lineage_id = "preserved-lineage"
            agent.cli_session_id = "preserved-cli"
            db.add(agent)
            if bound_guest == "prepared_receipt":
                now = datetime.now(timezone.utc)
                db.add(
                    AgentResultReceipt(
                        id="a" * 32,
                        token_sha256="b" * 64,
                        session_id=s.sid,
                        local_session_id=agent.local_session_id,
                        seq=1,
                        dispatch_count=1,
                        claim_owner="original-executor",
                        guest_id=agent.ember_session_id,
                        request_sha256="c" * 64,
                        created_at=now,
                        accept_until=now + timedelta(hours=13),
                        retain_until=now + timedelta(days=7),
                    )
                )
            db.commit()
    if historical:
        _persist_uncertain_not_invoked(s)
    before = controls.task_snapshot(s.task["id"])
    native = s.native_snapshot()
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    run = conductor.graph.node_runs(s.task["id"])[0]
    current = controls.task_snapshot(s.task["id"])
    result = json.loads(run["outcome_json"])
    assert run["status"] == current["starts"][0]["status"] == "failed"
    assert run["finished_at"] is not None and run["pin"] == s.run["pin"]
    assert run["cost_usd"] is current["starts"][0]["cost_usd"] is None
    assert run["accounted_cost_usd"] == 0.0
    assert run["accounting_basis"] == "no_model_post"
    # The start ledger books what the graph books. A start still charged its
    # reserved ceiling would refuse the retry the graph just released (#6045).
    assert current["committed_cost_usd"] == 0.0
    assert current["starts"][0]["accounting_basis"] == "no_model_post"
    assert current["state"] == "admitted" and current["unresolved_starts"] == 0
    assert current["turns_used"] == before["turns_used"] == 0
    assert current["planner_turns_used"] == before["planner_turns_used"] == 1
    assert current["deadline_at"] == before["deadline_at"]
    assert current["policy"] == before["policy"]
    assert controls.status()["admitted_count"] == 1
    assert result["previous_outcome"] == s.result
    assert result["not_invoked"]["invocation_phase"] == "not_invoked"
    assert result["not_invoked"]["claim_owner"] == "original-executor"
    assert result["not_invoked"]["dispatch_count"] == 1
    assert s.native_snapshot() == native
    # The failed pre-model attempt spent no graph allowance, so the same node
    # remains ready for the retry promised by its attempt bound.
    assert conductor._ready_nodes(conductor.graph.load_graph(s.task["id"]), [run])
    assert s.native_snapshot() == native


# The text the transport actually raises: httpx's status line, then the body
# _status_error_detail appends, which carries the control plane's reason.
CAPACITY_DENIAL_ERROR = (
    "Client error '429 Too Many Requests' for url "
    "'https://embervm.embervm.svc.cluster.local/v1/workloads/claude/sessions'\n"
    "For more information check: "
    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429\n"
    'response body: {"error":"session create denied","reason":"no_capacity",'
    '"workload":"claude","retryable":true}'
)


@pytest.mark.parametrize(
    "not_invoked_factory",
    [CAPACITY_DENIAL_ERROR],
    indirect=True,
)
def test_capacity_denied_create_is_marked_audited_and_not_charged_an_attempt(
    not_invoked_factory,
):
    """EmberVM having no slot is not the node failing, so it keeps its attempt."""
    import json
    from sqlmodel import Session, select
    from swarm.factory_models import FactoryAudit

    s = not_invoked_factory
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["status"] == "failed" and run["capacity_denied"] is True
    result = json.loads(run["outcome_json"])
    assert result["capacity_denied"] is True
    assert result["not_invoked"]["capacity_denied"] is True
    assert conductor.graph.attempts_spent([run], run["node_key"]) == 0
    with Session(s.engine) as db:
        denials = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "capacity_denied")
        ).all()
    assert len(denials) == 1
    detail = json.loads(denials[0].detail_json)
    assert detail["node_key"] == run["node_key"] and detail["attempt"] == 1
    assert detail["session_id"] == s.sid


@pytest.mark.parametrize(
    "not_invoked_factory",
    [
        "create failed before invoke",
        # A 429 on the sessions endpoint whose body says the request was
        # wrong rather than the workload full. Only the reasons the control
        # plane emits for a capacity refusal excuse the attempt.
        "Client error '429 Too Many Requests' for url "
        "'https://embervm/v1/workloads/claude/sessions'\n"
        'response body: {"error":"session create denied",'
        '"reason":"invalid_idempotency_key","retryable":false}',
        # The reason, but from another endpoint entirely.
        "Client error '429 Too Many Requests' for url "
        "'https://embervm/v1/workloads/claude/turns'\n"
        'response body: {"reason":"no_capacity"}',
        # The status line alone, with the body lost.
        "Client error '429 Too Many Requests' for url "
        "'https://embervm/v1/workloads/claude/sessions'",
    ],
    indirect=True,
)
def test_a_failure_that_is_not_a_capacity_refusal_spends_its_attempt(
    not_invoked_factory,
):
    """Only the control plane's own refusal to place a session excuses it."""
    import json

    s = not_invoked_factory
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["capacity_denied"] is False
    assert json.loads(run["outcome_json"])["capacity_denied"] is False
    assert conductor.graph.attempts_spent([run], run["node_key"]) == 1


def _ready_node(key, **overrides):
    node = {"node_key": key, "deps": [], "max_attempts": 2, "max_cost_usd": 4.0}
    node.update(overrides)
    return node


def _ready_run(key, **overrides):
    run = {
        "node_key": key,
        "status": "failed",
        "accounted_cost_usd": 0.0,
        "capacity_denied": False,
    }
    run.update(overrides)
    return run


def test_ready_nodes_excuse_capacity_denials_up_to_their_bound():
    """A node with one attempt left survives three refused slots, not four."""
    node = _ready_node("implement_fix", max_attempts=1)
    denied = _ready_run("implement_fix", capacity_denied=True)
    for count in range(1, 4):
        assert conductor._ready_nodes([node], [denied] * count) == [node]
    assert conductor._ready_nodes([node], [denied] * 4) == []
    assert conductor._ready_nodes([node], [_ready_run("implement_fix")]) == []


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "generic_error",
        "owner",
        "dispatch_zero",
        "dispatch_newer",
        "dispatch_bool",
        "dispatch_string",
        "dispatch_missing",
        "future_dispatch",
        "permit_missing",
        "permit_uncertain",
        "permit_running",
        "permit_outcome",
        "permit_owner",
        "permit_local",
        "permit_timestamp",
        "workflow",
        "model",
        "new_pending",
        "new_turn",
        "captured_receipt",
        "receipt_owner",
        "fence",
        "cleanup",
        "pin",
    ],
)
def test_not_invoked_refuses_conflicting_or_insufficient_proof(
    not_invoked_factory, case
):
    import copy
    import json
    from datetime import datetime, timedelta, timezone
    from sqlmodel import Session, select
    from agent_sessions.constants import UNKNOWN_INVOCATION
    from agent_sessions.models import (
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from swarm import factory_controls as controls

    s = not_invoked_factory
    _persist_uncertain_not_invoked(s)
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        turn = db.exec(select(AgentTurn)).one()
        permit = db.exec(select(AgentCapacityReservation)).one()
        usage = json.loads(turn.usage_json)
        if case == "unknown":
            turn.stop_reason = UNKNOWN_INVOCATION
        elif case == "generic_error":
            usage["recovery"].pop("invocation_phase")
        elif case == "owner":
            usage["recovery"]["claim_owner"] = "other-owner"
        elif case.startswith("dispatch_"):
            value = {
                "dispatch_zero": 0,
                "dispatch_newer": 2,
                "dispatch_bool": True,
                "dispatch_string": "1",
                "dispatch_missing": None,
            }[case]
            usage["recovery"]["dispatch_count"] = value
        elif case == "future_dispatch":
            usage["recovery"]["last_dispatch_at"] = (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat()
        elif case == "permit_missing":
            db.delete(permit)
        elif case.startswith("permit_"):
            field, value = {
                "permit_uncertain": ("state", "uncertain"),
                "permit_running": ("state", "running"),
                "permit_outcome": ("outcome", "delivery_error"),
                "permit_owner": ("owner", "new-owner"),
                "permit_local": ("local_session_id", "another-session"),
                "permit_timestamp": ("settled_at", None),
            }[case]
            setattr(permit, field, value)
            db.add(permit)
        elif case == "workflow":
            agent.workflow_id = "factory-node:another:work:1"
        elif case == "model":
            turn.model = "luna"
        elif case == "new_pending":
            db.add(PendingMessage(session_id=s.sid, seq=2, message_text="new work"))
        elif case == "new_turn":
            db.add(AgentTurn(session_id=s.sid, seq=2, prompt="new", result_text="new"))
        elif case in {"captured_receipt", "receipt_owner"}:
            agent.ember_session_id = "same-created-guest"
            now = datetime.now(timezone.utc)
            db.add(
                AgentResultReceipt(
                    id="a" * 32,
                    token_sha256="b" * 64,
                    session_id=s.sid,
                    local_session_id=agent.local_session_id,
                    seq=1,
                    dispatch_count=1,
                    claim_owner="other-owner"
                    if case == "receipt_owner"
                    else permit.owner,
                    guest_id=agent.ember_session_id,
                    request_sha256="c" * 64,
                    created_at=now,
                    accept_until=now + timedelta(hours=13),
                    retain_until=now + timedelta(days=7),
                    received_at=now if case == "captured_receipt" else None,
                    result_sha256="d" * 64 if case == "captured_receipt" else None,
                    result_body=b"{}" if case == "captured_receipt" else None,
                )
            )
        elif case == "fence":
            agent.result_receipt_fence_id = "a" * 32
        elif case == "cleanup":
            agent.guest_cleanup_id = "a" * 32
        turn.usage_json = json.dumps(usage)
        db.add_all([agent, turn])
        db.commit()
    if case == "pin":
        s.run = copy.deepcopy(s.run)
        s.run["pin"]["prompt"] = "different immutable request"
    before = controls.task_snapshot(s.task["id"])
    native = s.native_snapshot()
    runs = conductor.graph.node_runs(s.task["id"])
    if case in {"workflow", "pin"}:
        with pytest.raises(ValueError, match="ownership conflict|attempt changed"):
            conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    else:
        conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert conductor.graph.node_runs(s.task["id"]) == runs
    assert controls.task_snapshot(s.task["id"])["starts"] == before["starts"]
    assert controls.task_snapshot(s.task["id"])["state"] == "uncertain"
    assert s.native_snapshot() == native


def test_not_invoked_atomic_rollback_then_historical_reconciliation(
    not_invoked_factory,
):
    from sqlalchemy import event
    from sqlmodel import Session
    from swarm import factory_controls as controls
    from swarm.factory_models import FactoryAudit

    s = not_invoked_factory
    _persist_uncertain_not_invoked(s)
    before = controls.task_snapshot(s.task["id"])
    runs = conductor.graph.node_runs(s.task["id"])
    native = s.native_snapshot()

    def fail_audit(db, *_):
        if any(
            isinstance(row, FactoryAudit) and row.action == "record_start_outcome"
            for row in db.new
        ):
            raise RuntimeError("injected settlement failure")

    event.listen(Session, "before_flush", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="injected settlement failure"):
            conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    finally:
        event.remove(Session, "before_flush", fail_audit)
    assert conductor.graph.node_runs(s.task["id"]) == runs
    assert controls.task_snapshot(s.task["id"]) == before
    assert s.native_snapshot() == native
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "failed"
    assert controls.task_snapshot(s.task["id"])["unresolved_starts"] == 0
    assert s.native_snapshot() == native


@pytest.mark.parametrize(
    "workflow_status", ["PENDING", "ENQUEUED", "ERROR", "CANCELLED"]
)
def test_not_invoked_honors_terminal_workflow_boundary(
    not_invoked_factory, workflow_status
):
    from swarm import factory_controls as controls

    s = not_invoked_factory
    _persist_uncertain_not_invoked(s)
    s.dbos.get_workflow_status = lambda _: SimpleNamespace(status=workflow_status)
    before = controls.task_snapshot(s.task["id"])
    native = s.native_snapshot()
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    current = controls.task_snapshot(s.task["id"])
    if workflow_status in {"PENDING", "ENQUEUED"}:
        assert current == before
        assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "uncertain"
    else:
        assert current["unresolved_starts"] == 0
        assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "failed"
        assert current["turns_used"] == 0
        # The planning round is spent: this attempt reached no model, but it
        # is still an attempt the node made. Only its budget is released.
        assert current["planner_turns_used"] == 1
        assert current["committed_cost_usd"] == 0.0
    assert s.native_snapshot() == native


@pytest.mark.parametrize("action", ["pause_task", "stop"])
def test_not_invoked_reconciliation_does_not_override_operating_controls(
    not_invoked_factory, action
):
    from swarm import factory_controls as controls

    s = not_invoked_factory
    _persist_uncertain_not_invoked(s)
    assert controls.set_control(
        action, "operator", task_id=s.task["id"] if action == "pause_task" else None
    )["ok"]
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    runs = conductor.graph.node_runs(s.task["id"])
    assert runs[0]["status"] == "failed"
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert conductor.graph.node_runs(s.task["id"]) == runs
    assert len(conductor.graph.load_graph(s.task["id"])) == 1
    current = controls.task_snapshot(s.task["id"])
    assert current["turns_used"] == 0 and current["planner_turns_used"] == 1
    assert current["committed_cost_usd"] == 0.0
    assert (
        current["task_paused"]
        if action == "pause_task"
        else current["cancellation_requested"]
    )


def test_reservation_pins_original_task_deadline_and_replays(queued_factory):
    from swarm.factory_controls import task_snapshot

    s = queued_factory
    original = s.run["pin"]
    assert original["task_deadline_at"] == task_snapshot(s.task["id"])["deadline_at"]
    assert conductor.reserve_node(
        s.task["id"], s.run["node_key"], s.run["dispatch_key"], s.context
    )
    runs = conductor.graph.node_runs(s.task["id"])
    assert len(runs) == 1 and runs[0]["pin"] == original


def test_timeout_reconciliation_settles_both_ledgers_without_budget_credit(
    queued_factory, monkeypatch
):
    import json
    from sqlmodel import Session, select
    from swarm import factory_controls as controls
    from agent_sessions.models import AgentTurn, PendingMessage
    import swarm.node_workflows as nodes

    s = queued_factory
    monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: None)
    result = {
        "status": "uncertain",
        "reason": "timeout: session cessation is unconfirmed; reconcile before retry",
        "cost_usd": None,
        "session_id": s.sid,
    }
    dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: result),
    )
    original = controls.task_snapshot(s.task["id"])
    conductor._submit_or_reconcile(s.task, s.run, dbos)
    runs = conductor.graph.node_runs(s.task["id"])
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert runs[0]["pin"] == s.run["pin"]
    assert runs[0]["accounted_cost_usd"] == s.run["reserved_cost_usd"]
    assert json.loads(runs[0]["outcome_json"])["cost_usd"] is None
    current = controls.task_snapshot(s.task["id"])
    assert current["starts"][0]["status"] == "failed"
    assert current["turns_used"] == original["turns_used"] == 0
    assert current["planner_turns_used"] == original["planner_turns_used"] == 1
    assert current["deadline_at"] == original["deadline_at"]
    with Session(s.engine) as db:
        assert db.exec(select(PendingMessage)).first() is None
        assert db.exec(select(AgentTurn)).one().model is None


def test_timeout_reconciliation_rollback_preserves_queue_and_ledgers(
    queued_factory, monkeypatch
):
    from sqlmodel import Session, select
    from agent_sessions.models import AgentTurn, PendingMessage
    from swarm import factory_controls as controls
    import swarm.node_workflows as nodes

    s = queued_factory
    monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: None)
    monkeypatch.setattr(
        controls,
        "record_start_outcome",
        lambda *_args, **_kwargs: {"ok": False, "reason": "injected failure"},
    )
    result = {"status": "uncertain", "reason": "timeout: queued", "session_id": s.sid}
    dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: result),
    )
    with pytest.raises(ValueError, match="injected failure"):
        conductor._submit_or_reconcile(s.task, s.run, dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "admitted"
    with Session(s.engine) as db:
        assert db.exec(select(PendingMessage)).one().dispatch_count == 0
        assert db.exec(select(AgentTurn)).first() is None


@pytest.fixture
def stranded_factory(queued_factory, monkeypatch):
    """A queued node whose DBOS workflow outlived the image that started it."""
    from dbos._utils import GlobalParams
    from swarm import factory_controls as controls
    from swarm import node_workflows as nodes

    s = queued_factory
    monkeypatch.setattr(controls, "get_engine", lambda: s.engine)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "false")
    monkeypatch.setattr(GlobalParams, "app_version", "running-version")
    monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: None)
    s.cancelled = []
    s.key = s.run["pin"]["workflow_id"]

    def dbos_for(status, version, *, result=None):
        """A DBOS whose cancel really makes the workflow terminal, as DBOS does."""
        state = {"status": status}

        def cancel_workflow(key, cancel_children=False):
            s.cancelled.append((key, cancel_children))
            state["status"] = "CANCELLED"

        return SimpleNamespace(
            get_workflow_status=lambda _: SimpleNamespace(
                status=state["status"], app_version=version
            ),
            cancel_workflow=cancel_workflow,
            retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: result),
        )

    s.dbos_for = dbos_for
    return s


def _stranded_audits(s):
    import json
    from sqlmodel import Session, select
    from swarm.factory_models import FactoryAudit

    with Session(s.engine) as db:
        return [
            json.loads(row.detail_json)
            for row in db.exec(
                select(FactoryAudit).where(FactoryAudit.action == "workflow_stranded")
            ).all()
        ]


@pytest.mark.parametrize("workflow_status", ["PENDING", "ENQUEUED"])
def test_a_version_stranded_node_workflow_is_cancelled_and_settled_uncertain(
    stranded_factory, workflow_status
):
    import json

    s = stranded_factory
    conductor._submit_or_reconcile(
        s.task, s.run, s.dbos_for(workflow_status, "deployed-version")
    )
    assert s.cancelled == [(s.key, True)]
    assert _stranded_audits(s) == [
        {
            "workflow_id": s.key,
            "workflow_version": "deployed-version",
            "running_version": "running-version",
        }
    ]
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["status"] == "uncertain"
    assert (
        json.loads(run["outcome_json"])["reason"]
        == "node workflow stranded by application version change"
    )


def test_a_stranded_node_retries_once_its_real_outcome_is_reconciled(
    stranded_factory, monkeypatch
):
    from swarm import node_workflows as nodes

    s = stranded_factory
    monkeypatch.setattr(
        nodes,
        "reconcile_completed_node",
        lambda *_: {
            "status": "failed",
            "reason": "the session ended while its workflow was stranded",
            "cost_usd": 0.5,
            "cost_basis": "measured",
            "session_id": s.sid,
        },
    )
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": "c" * 40}}
    )
    conductor._submit_or_reconcile(s.task, s.run, s.dbos_for("PENDING", "old-version"))
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "failed"
    conductor.reconcile_task(
        s.task["id"], s.policy, s.dbos_for("PENDING", "old-version")
    )
    assert [r["attempt"] for r in conductor.graph.node_runs(s.task["id"])] == [1, 2]


def test_a_pending_node_workflow_on_the_running_version_is_left_alone(stranded_factory):
    s = stranded_factory
    conductor._submit_or_reconcile(
        s.task, s.run, s.dbos_for("PENDING", "running-version")
    )
    assert s.cancelled == [] and _stranded_audits(s) == []
    assert conductor.graph.node_runs(s.task["id"]) == [s.run]


def test_an_unresolvable_running_version_strands_nothing(stranded_factory, monkeypatch):
    from dbos._utils import GlobalParams

    s = stranded_factory
    monkeypatch.setattr(GlobalParams, "app_version", "")
    conductor._submit_or_reconcile(s.task, s.run, s.dbos_for("PENDING", "old-version"))
    assert s.cancelled == [] and _stranded_audits(s) == []
    assert conductor.graph.node_runs(s.task["id"]) == [s.run]


def test_a_successful_old_version_workflow_still_returns_its_result(stranded_factory):
    import json

    s = stranded_factory
    result = {
        "status": "succeeded",
        "summary": "Finished before the deploy replaced the pods.",
        "cost_usd": 1.0,
        "cost_basis": "measured",
        "head_sha": "d" * 40,
        "session_id": s.sid,
    }
    conductor._submit_or_reconcile(
        s.task, s.run, s.dbos_for("SUCCESS", "old-version", result=result)
    )
    assert s.cancelled == [] and _stranded_audits(s) == []
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["status"] == "succeeded"
    assert json.loads(run["outcome_json"])["cost_usd"] == 1.0


def _stalled_dbos(s, *, idle_seconds, monkeypatch):
    """A PENDING workflow on the running version whose last step is old.

    Also steps past the post-start settling window, which a test process is
    always inside of.
    """
    import time

    timeout = s.run["pin"]["turn_timeout_seconds"]
    monkeypatch.setattr(
        conductor,
        "_last_step_epoch_ms",
        lambda _key: int((time.time() - timeout - idle_seconds) * 1000),
    )
    monkeypatch.setattr(
        conductor, "_STARTED_AT", time.monotonic() - conductor.TICK_SECONDS * 3
    )
    return s.dbos_for("PENDING", "running-version")


def _stall_audits(s):
    import json
    from sqlmodel import Session, select
    from swarm.factory_models import FactoryAudit

    with Session(s.engine) as db:
        return [
            json.loads(row.detail_json)
            for row in db.exec(
                select(FactoryAudit).where(FactoryAudit.action == "node_stalled")
            ).all()
        ]


def test_a_stalled_node_is_cancelled_and_settled_uncertain_once(
    stranded_factory, monkeypatch
):
    """Three ticks, one cancellation, one audit, one warning, one outcome.

    The planner is not consulted here. The settled node reaches it through the
    ordinary deviation path once it has no runnable retry, which is what keeps
    this working at the default parallel limit of one.
    """
    import json
    from sqlmodel import Session, select
    from swarm.models import SwarmConductorCall

    s = stranded_factory
    notified = []
    monkeypatch.setattr(
        conductor, "_notify_node_stalled", lambda *args: notified.append(args)
    )
    dbos = _stalled_dbos(s, idle_seconds=30, monkeypatch=monkeypatch)

    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    conductor.reconcile_task(s.task["id"], s.policy, dbos)

    audits = _stall_audits(s)
    assert len(audits) == 1
    assert audits[0]["workflow_id"] == s.key
    assert audits[0]["node_key"] == s.run["node_key"]
    assert audits[0]["turn_timeout_seconds"] == s.run["pin"]["turn_timeout_seconds"]
    assert len(notified) == 1 and notified[0][1] == s.run["node_key"]
    assert s.cancelled == [(s.key, True)]

    runs = conductor.graph.node_runs(s.task["id"])
    assert len(runs) == 1 and runs[0]["status"] == "uncertain"
    assert "node workflow stalled" in json.loads(runs[0]["outcome_json"])["reason"]
    # No planner node was inserted by the stall handler.
    assert [
        node["node_key"]
        for node in conductor.graph.load_graph(s.task["id"])
        if node["node_key"].startswith("conductor_")
    ] == ["conductor_1"]
    # The settlement is recorded once, not once per tick.
    with Session(s.engine) as db:
        outcomes = db.exec(
            select(SwarmConductorCall).where(
                SwarmConductorCall.tool == "record_outcome"
            )
        ).all()
    assert len(outcomes) == 1


def test_a_settled_stalled_node_reaches_the_planner_by_the_ordinary_path(
    stranded_factory, monkeypatch
):
    """The deviation the planner sees is the existing one for a settled node."""
    s = stranded_factory
    monkeypatch.setattr(conductor, "_notify_node_stalled", lambda *_args: None)
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": "c" * 40}}
    )
    dbos = _stalled_dbos(s, idle_seconds=30, monkeypatch=monkeypatch)
    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "uncertain"

    # An uncertain run holds its reservation, so nothing is planned while it
    # stands. Reconciling it to a terminal failure is what frees the graph.
    import json

    settled = json.dumps({"status": "failed", "cost_usd": 0.5, "session_id": s.sid})
    assert conductor.graph.record_outcome(
        s.task["id"], s.run["node_key"], s.run["attempt"], "failed", 0.5, None, settled
    ).ok
    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    assert [
        node["node_key"]
        for node in conductor.graph.load_graph(s.task["id"])
        if node["node_key"].startswith("conductor_")
    ] == ["conductor_1"]
    assert [r["attempt"] for r in conductor.graph.node_runs(s.task["id"])] == [1, 2]


def test_a_node_checkpointing_within_its_turn_timeout_is_not_stalled(
    stranded_factory, monkeypatch
):
    import time

    s = stranded_factory
    monkeypatch.setattr(
        conductor, "_last_step_epoch_ms", lambda _key: int(time.time() * 1000)
    )
    conductor.reconcile_task(
        s.task["id"], s.policy, s.dbos_for("PENDING", "running-version")
    )
    assert _stall_audits(s) == []
    assert conductor.graph.node_runs(s.task["id"]) == [s.run]
    assert [
        node["node_key"]
        for node in conductor.graph.load_graph(s.task["id"])
        if node["node_key"].startswith("conductor_")
    ] == ["conductor_1"]


def _aged_pending_dbos(s, *, age_seconds):
    """PENDING on the running version, created age_seconds ago. Never cancels."""
    import time

    return SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(
            status="PENDING",
            app_version="running-version",
            created_at=int((time.time() - age_seconds) * 1000),
        ),
        cancel_workflow=lambda *args, **kwargs: s.cancelled.append(args),
    )


def test_an_unreadable_step_history_does_not_call_a_node_stalled(
    stranded_factory, monkeypatch
):
    """A failed read is not evidence of a stall, even on an old workflow.

    A real WorkflowStatus always carries created_at, so collapsing a failed
    read into "no progress since creation" would cancel a live node that is
    only waiting a long time for a guest, which is bounded by the task deadline
    rather than by the turn timeout.
    """
    import time

    s = stranded_factory
    timeout = s.run["pin"]["turn_timeout_seconds"]
    monkeypatch.setattr(
        conductor, "_last_step_epoch_ms", lambda _key: conductor.UNREADABLE_STEPS
    )
    monkeypatch.setattr(
        conductor, "_STARTED_AT", time.monotonic() - conductor.TICK_SECONDS * 3
    )
    conductor.reconcile_task(
        s.task["id"], s.policy, _aged_pending_dbos(s, age_seconds=timeout * 10)
    )
    assert _stall_audits(s) == [] and s.cancelled == []
    assert conductor.graph.node_runs(s.task["id"]) == [s.run]


def test_a_workflow_that_has_checkpointed_nothing_is_dated_by_its_creation(
    stranded_factory, monkeypatch
):
    """A successful read with no rows is a real observation, unlike a failure."""
    import time

    s = stranded_factory
    timeout = s.run["pin"]["turn_timeout_seconds"]
    monkeypatch.setattr(conductor, "_last_step_epoch_ms", lambda _key: None)
    monkeypatch.setattr(conductor, "_notify_node_stalled", lambda *_args: None)
    monkeypatch.setattr(
        conductor, "_STARTED_AT", time.monotonic() - conductor.TICK_SECONDS * 3
    )
    conductor.reconcile_task(
        s.task["id"], s.policy, _aged_pending_dbos(s, age_seconds=timeout * 10)
    )
    assert len(_stall_audits(s)) == 1
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "uncertain"


def test_no_stall_is_called_in_the_settling_window_after_process_start(
    stranded_factory, monkeypatch
):
    """DBOS recovers workflows on a background thread after launch.

    A tick between launch and that thread's first checkpoint sees no recent
    step on a node DBOS is about to resume, and an outage longer than the turn
    timeout makes every one of them look wedged at once.
    """
    import time

    s = stranded_factory
    timeout = s.run["pin"]["turn_timeout_seconds"]
    monkeypatch.setattr(
        conductor,
        "_last_step_epoch_ms",
        lambda _key: int((time.time() - timeout * 10) * 1000),
    )
    monkeypatch.setattr(conductor, "_STARTED_AT", time.monotonic())
    conductor.reconcile_task(
        s.task["id"], s.policy, s.dbos_for("PENDING", "running-version")
    )
    assert _stall_audits(s) == [] and s.cancelled == []

    # Past the window the same observation is acted on.
    monkeypatch.setattr(conductor, "_notify_node_stalled", lambda *_args: None)
    monkeypatch.setattr(
        conductor, "_STARTED_AT", time.monotonic() - conductor.TICK_SECONDS * 3
    )
    conductor.reconcile_task(
        s.task["id"], s.policy, s.dbos_for("PENDING", "running-version")
    )
    assert len(_stall_audits(s)) == 1


def test_a_stranded_workflow_is_settled_rather_than_called_stalled(
    stranded_factory, monkeypatch
):
    """Version stranding wins: the workflow is dead, not merely quiet."""
    s = stranded_factory
    monkeypatch.setattr(conductor, "_last_step_epoch_ms", lambda _key: 0)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos_for("PENDING", "old-version"))
    assert _stall_audits(s) == []
    assert s.cancelled == [(s.key, True)]
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "uncertain"


def test_a_session_less_uncertain_run_resolves_its_session_for_supervision(
    stranded_factory, monkeypatch
):
    """record_dispatch binds a session only at completion, so resolve it here.

    Without this the attempt is handed to supervision as None, refused at its
    int guard, and the guest is never confirmed ceased.
    """
    from swarm import factory_supervision

    s = stranded_factory
    seen = []
    monkeypatch.setattr(
        factory_supervision,
        "reconcile_uncertain_attempt",
        lambda _pin, session_id, _result, status: (
            seen.append((session_id, status)) or False
        ),
    )
    assert s.run["session_id"] is None
    conductor._submit_or_reconcile(s.task, s.run, s.dbos_for("PENDING", "old-version"))
    assert seen == [(s.sid, "CANCELLED")]
    # The resolved session is bound to the run, so the next tick reads it
    # directly rather than resolving again.
    assert conductor.graph.node_runs(s.task["id"])[0]["session_id"] == s.sid


def test_an_unresolvable_session_records_one_outcome_not_one_per_tick(
    stranded_factory, monkeypatch
):
    from sqlmodel import Session, select
    from agent_sessions.models import AgentSession, PendingMessage
    from swarm.models import SwarmConductorCall

    s = stranded_factory
    with Session(s.engine) as db:
        db.delete(db.exec(select(PendingMessage)).one())
        db.delete(db.get(AgentSession, s.sid))
        db.commit()
    dbos = s.dbos_for("PENDING", "old-version")

    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    conductor.reconcile_task(s.task["id"], s.policy, dbos)

    runs = conductor.graph.node_runs(s.task["id"])
    assert len(runs) == 1 and runs[0]["status"] == "uncertain"
    assert runs[0]["session_id"] is None
    with Session(s.engine) as db:
        outcomes = db.exec(
            select(SwarmConductorCall).where(
                SwarmConductorCall.tool == "record_outcome"
            )
        ).all()
    assert len(outcomes) == 1


def test_a_session_owned_by_another_attempt_raises_rather_than_being_adopted(
    stranded_factory,
):
    """The key is deterministic, so a mismatch on it is a real conflict."""
    from sqlmodel import Session
    from agent_sessions.models import AgentSession

    s = stranded_factory
    with Session(s.engine) as db:
        owner = db.get(AgentSession, s.sid)
        owner.node_attempt = 2
        db.add(owner)
        db.commit()
    with pytest.raises(ValueError, match="node session ownership conflict"):
        conductor._submit_or_reconcile(
            s.task, s.run, s.dbos_for("PENDING", "old-version")
        )


def test_the_stranded_audit_is_written_once_and_after_the_cancellation(
    stranded_factory, monkeypatch
):
    """A raising cancel must not leave one audit row per tick behind it."""
    s = stranded_factory
    failures = [RuntimeError("DBOS unavailable"), RuntimeError("DBOS unavailable")]

    def cancel_workflow(key, cancel_children=False):
        if failures:
            raise failures.pop()
        s.cancelled.append((key, cancel_children))

    dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(
            status="PENDING", app_version="old-version"
        ),
        cancel_workflow=cancel_workflow,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="DBOS unavailable"):
            conductor._submit_or_reconcile(s.task, s.run, dbos)
    assert _stranded_audits(s) == []
    conductor._submit_or_reconcile(s.task, s.run, dbos)
    assert len(_stranded_audits(s)) == 1
    assert s.cancelled == [(s.key, True)]


def test_a_cost_arriving_on_an_uncertain_run_is_recorded(stranded_factory, monkeypatch):
    """The one uncertain observation worth recording over another.

    A completed workflow can report unknown execution while carrying real
    provider spend. Dropping it would leave the attempt accounted at its full
    reservation with nothing measured to settle against.
    """
    import json
    from swarm import node_workflows as nodes

    s = stranded_factory
    monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: None)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos_for("PENDING", "old-version"))
    assert conductor.graph.node_runs(s.task["id"])[0]["cost_usd"] is None

    priced = {
        "status": "uncertain",
        "reason": "unknown_invocation: reconcile before retry",
        "cost_usd": 0.25,
        "cost_basis": "provider",
        "session_id": s.sid,
    }
    conductor._submit_or_reconcile(
        s.task, s.run, s.dbos_for("SUCCESS", "old-version", result=priced)
    )
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["status"] == "uncertain" and run["cost_usd"] == 0.25
    assert json.loads(run["outcome_json"])["cost_basis"] == "provider"


def test_a_cost_less_uncertain_observation_is_not_recorded_again(
    stranded_factory, monkeypatch
):
    from sqlmodel import Session, select
    from swarm import node_workflows as nodes
    from swarm.models import SwarmConductorCall

    s = stranded_factory
    monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: None)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos_for("PENDING", "old-version"))
    unpriced = {
        "status": "uncertain",
        "reason": "a different unknown, still with no measured cost",
        "cost_usd": None,
        "cost_basis": "unknown",
        "session_id": s.sid,
    }
    conductor._submit_or_reconcile(
        s.task, s.run, s.dbos_for("SUCCESS", "old-version", result=unpriced)
    )
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["cost_usd"] is None
    assert "stranded by application version change" in run["outcome_json"]
    with Session(s.engine) as db:
        outcomes = db.exec(
            select(SwarmConductorCall).where(
                SwarmConductorCall.tool == "record_outcome"
            )
        ).all()
    assert len(outcomes) == 1


@pytest.fixture
def uncertain_factory(queued_factory, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import copy
    import json
    from sqlmodel import Session, select
    from agent_sessions import admission, store
    from agent_sessions.models import AgentSession, AgentTurn
    from swarm import factory_controls as controls
    from swarm import factory_supervision as supervisor

    s = queued_factory
    for module in (controls, admission, store):
        monkeypatch.setattr(module, "get_engine", lambda: s.engine)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "true")
    owner = "original-factory-executor"
    assert store.claim_pending_message_for_session_sync(s.sid, owner) == 1
    assert admission.recheck(s.sid, 1, owner)
    # store.claim_pending_message_for_session_sync stamps last_dispatch_at when
    # the executor claims the turn, before the guest is invoked, so the control
    # plane's own invoke stamps fall after it and before the failed turn.
    s.dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    s.invoke_started_at = int(
        (s.dispatched_at + timedelta(seconds=1)).timestamp() * 1000
    )
    s.last_invoke_at = s.invoke_started_at + 1000
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        agent.ember_session_id = "s-exact-factory"
        agent.ember_lineage_id = "lineage-preserved"
        agent.cli_session_id = "cli-preserved"
        pending = store.get_pending_message(db, s.sid, 1)
        pending.last_dispatch_at = s.dispatched_at
        pending.partial_text = "Unpublished partial implementation"
        db.add_all([agent, pending])
        db.commit()
    store.finish_unknown_pending_sync(s.sid, 1, owner, 1, "executor_cancelled")
    with Session(s.engine) as db:
        # The moment the failure was recorded. Every control-plane stamp this
        # attempt produced is ordered against this, not against the claim.
        failed = db.exec(
            select(AgentTurn).where(AgentTurn.session_id == s.sid, AgentTurn.seq == 1)
        ).one()
        s.failed_turn_at = (
            failed.created_at.replace(tzinfo=timezone.utc)
            if failed.created_at.tzinfo is None
            else failed.created_at
        )
    s.result = {
        "status": "uncertain",
        "session_id": s.sid,
        "cost_usd": None,
        "reason": "unknown_invocation: reconcile before retry",
        "head_sha": "a" * 40,
    }
    conductor.graph.record_dispatch(
        s.task["id"],
        s.run["node_key"],
        1,
        s.sid,
        "b" * 40,
    )
    assert conductor.graph.record_outcome(
        s.task["id"],
        s.run["node_key"],
        1,
        "uncertain",
        None,
        "a" * 40,
        json.dumps(s.result),
    ).ok
    assert controls.record_start_outcome(
        s.task["id"],
        s.run["pin"]["workflow_id"],
        "uncertain",
        "executor",
        session_id=s.sid,
    )["ok"]
    s.run = conductor.graph.node_runs(s.task["id"])[0]
    s.precondition = {
        "session_id": "s-exact-factory",
        "generation": 0,
        "invoke_started_at": s.invoke_started_at,
        "vm_id": "vm-original",
        "node_id": "node-1",
        "instance_id": "node-1/pod-original",
        "pod_uid": "pod-original",
        "boot_id": "boot-original",
    }
    s.cp = {
        "session_id": "s-exact-factory",
        "state": "running",
        "generation": 0,
        "invoke_started_at": s.invoke_started_at,
        "last_invoke_at": s.last_invoke_at,
        "stop_precondition": s.precondition,
        "stop_intent": None,
        "stop_completion": None,
    }
    s.calls = []

    def http(guest_id, precondition=None):
        s.calls.append((guest_id, copy.deepcopy(precondition)))
        if precondition is not None:
            assert precondition == s.precondition
            s.cp["state"] = "destroying"
            s.cp["stop_intent"] = {
                **s.precondition,
                "operation_id": "stop-original",
                "requested_at_unix_ms": s.last_invoke_at + 1000,
            }
        return copy.deepcopy(s.cp)

    s.http = http
    monkeypatch.setattr(supervisor, "_http", http)
    s.dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: s.result),
    )

    def complete():
        s.cp["state"] = "destroyed"
        s.cp["stop_completion"] = {
            **s.cp["stop_intent"],
            "completed_at_unix_ms": s.last_invoke_at + 2000,
        }

    s.complete = complete
    return s


def _uncertain_snapshot(s):
    from sqlmodel import Session, select
    from agent_sessions.models import (
        AgentCapacityReservation,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from swarm import factory_controls as controls

    with Session(s.engine) as db:
        return {
            "session": db.get(AgentSession, s.sid).model_dump(),
            "turns": [row.model_dump() for row in db.exec(select(AgentTurn)).all()],
            "pending": [
                row.model_dump() for row in db.exec(select(PendingMessage)).all()
            ],
            "permits": [
                row.model_dump()
                for row in db.exec(select(AgentCapacityReservation)).all()
            ],
            "runs": conductor.graph.node_runs(s.task["id"], session=db),
            "factory": controls.task_snapshot(s.task["id"], session=db),
        }


def test_attempt_stop_preview_survives_original_observer_loss(
    queued_factory, monkeypatch
):
    from sqlmodel import Session, select
    from agent_sessions import admission, store
    from agent_sessions.constants import UNKNOWN_INVOCATION
    from agent_sessions.models import AgentCapacityReservation, AgentSession, AgentTurn
    from swarm import factory_attempt_stop as stop
    from swarm import factory_controls as controls
    from swarm import factory_supervision as supervisor

    s = queued_factory
    for module in (controls, admission, store):
        monkeypatch.setattr(module, "get_engine", lambda: s.engine)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "true")
    owner = "original-executor"
    assert store.claim_pending_message_for_session_sync(s.sid, owner) == 1
    assert admission.recheck(s.sid, 1, owner)
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        agent.ember_session_id = "s-original"
        agent.ember_lineage_id = "lineage-original"
        pending = store.get_pending_message(db, s.sid, 1)
        pending.partial_text = "Preserved partial implementation"
        db.add_all([agent, pending])
        db.commit()
    before = stop.read_attempt_stop(s.task["id"], s.run["node_key"], 1, s.sid)
    assert store.release_pending_message_claim_sync(
        s.sid, 1, owner, "replica_lost", dispatch_count=1
    )
    assert stop.read_attempt_stop(s.task["id"], s.run["node_key"], 1, s.sid) == before
    original = _uncertain_snapshot(s)
    precondition = {
        "session_id": "s-original",
        "generation": 0,
        "invoke_started_at": 100,
        "vm_id": "vm-original",
        "node_id": "node-original",
        "instance_id": "node-original/pod-original",
        "pod_uid": "pod-original",
        "boot_id": "boot-original",
    }
    calls = []

    def http(guest):
        calls.append(guest)
        return {
            "session_id": guest,
            "state": "running",
            "generation": 0,
            "invoke_started_at": 100,
            "stop_precondition": precondition,
            "stop_intent": None,
            "stop_completion": None,
        }

    monkeypatch.setattr(supervisor, "_http", http)
    args = {
        "task_id": s.task["id"],
        "node_key": s.run["node_key"],
        "attempt": 1,
        "session_id": s.sid,
        "request_key": "request-original",
        "expected_identity_sha256": before["identity_sha256"],
        "reason": "Original observer disappeared",
        "actor": "operator:test",
    }
    requested = stop.request_attempt_stop(**args)
    assert requested["state"] == "requested"
    assert stop.request_attempt_stop(**args) == requested
    assert calls == ["s-original"]
    assert stop.read_attempt_stop(s.task["id"], s.run["node_key"], 1, s.sid) == before
    after = _uncertain_snapshot(s)
    events = after["factory"].pop("stop_events")
    original["factory"].pop("stop_events")
    assert after == original
    assert any(event["action"] == "attempt_stop_requested" for event in events)
    assert stop.executor_stop_requested(s.sid, 1, owner, 1)
    assert not stop.executor_stop_requested(s.sid, 1, owner, 2)
    assert not stop.executor_stop_requested(s.sid, 1, "new-owner", 1)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "false")
    assert stop.executor_stop_requested(s.sid, 1, owner, 1)
    with Session(s.engine) as db:
        turn = db.exec(select(AgentTurn)).one()
        assert turn.stop_reason == UNKNOWN_INVOCATION
        assert turn.result_text == "Preserved partial implementation"
        assert turn.cost_usd is None
        assert db.exec(select(AgentCapacityReservation)).one().state == "uncertain"
        assert db.get(AgentSession, s.sid).ember_lineage_id == "lineage-original"


@pytest.mark.parametrize("workflow_status", [None, "PENDING"])
def test_attempt_stop_missing_or_unresponsive_workflow_is_bounded_and_visible(
    uncertain_factory, workflow_status
):
    import json
    from sqlmodel import Session, select
    from swarm import factory_attempt_stop as stop
    from swarm.factory_models import FactoryAudit

    s = uncertain_factory
    before = stop.read_attempt_stop(s.task["id"], s.run["node_key"], 1, s.sid)
    stop.request_attempt_stop(
        task_id=s.task["id"],
        node_key=s.run["node_key"],
        attempt=1,
        session_id=s.sid,
        request_key="request-original",
        expected_identity_sha256=before["identity_sha256"],
        reason="Original observer disappeared",
        actor="operator:test",
    )
    calls = []
    dbos = SimpleNamespace(
        get_workflow_status=lambda _: (
            SimpleNamespace(status=workflow_status) if workflow_status else None
        ),
        cancel_workflow=lambda workflow, **kwargs: calls.append((workflow, kwargs)),
    )
    for _ in range(4):
        assert stop.process_attempt_stop(s.run["pin"], s.sid, dbos) == (True, s.sid)
    assert len(calls) == (2 if workflow_status else 0)
    assert all(kwargs == {"cancel_children": False} for _, kwargs in calls)
    with Session(s.engine) as db:
        observations = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "stop_observation")
        ).all()
        assert len(observations) == 1
        detail = json.loads(observations[0].detail_json)
        assert detail["reason"] == (
            "stop_cancel_bound_reached"
            if workflow_status
            else "stop_workflow_unavailable"
        )
        assert detail["intervention_required"]
        assert not detail["cessation_confirmed"]


@pytest.mark.parametrize("cleanup_claim", [False, True, "retired"])
def test_durable_stop_reconciles_production_factory_owner_without_replay(
    uncertain_factory,
    cleanup_claim,
    monkeypatch,
):
    from swarm import factory_controls as controls
    from sqlmodel import Session, select
    from swarm.factory_models import FactoryAudit

    s = uncertain_factory
    if cleanup_claim:
        _factory_cleanup_claim(s)
    before = _uncertain_snapshot(s)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    waiting = _uncertain_snapshot(s)
    for key in ("session", "turns", "pending", "permits", "runs"):
        assert waiting[key] == before[key]
    assert len([call for call in s.calls if call[1] is not None]) == 1
    s.complete()
    if cleanup_claim == "retired":
        from agent_sessions import store

        # A concurrent exact terminal cleanup releases only its fence while
        # UNKNOWN preserves the binding. The durable factory intent stays valid.
        with Session(s.engine) as db:
            assert not store.finish_guest_cleanup(
                db, s.sid, "s-exact-factory", s.run["pin"]["workflow_id"], "c" * 32
            )
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    after = _uncertain_snapshot(s)
    assert after["turns"] == before["turns"]
    assert after["turns"][0]["cost_usd"] is None
    assert after["turns"][0]["stop_reason"] == "invocation_outcome_unknown"
    assert after["turns"][0]["result_text"] == "Unpublished partial implementation"
    assert after["pending"] == []
    assert after["permits"][0]["state"] == "settled"
    assert after["permits"][0]["outcome"] == "guest_cessation_confirmed"
    assert after["session"]["ember_session_id"] is None
    for field in (
        "guest_cleanup_id",
        "guest_cleanup_guest_id",
        "guest_cleanup_workflow_id",
        "guest_cleanup_dispatch_json",
        "guest_cleanup_started_at",
    ):
        assert after["session"][field] is None
    assert after["session"]["prior_ember_lineage_id"] == "lineage-preserved"
    assert after["session"]["prior_cli_session_id"] == "cli-preserved"
    assert after["runs"][0]["status"] == "failed"
    assert after["runs"][0]["pin"] == before["runs"][0]["pin"]
    assert (
        after["runs"][0]["accounted_cost_usd"]
        == before["runs"][0]["accounted_cost_usd"]
    )
    for key in ("turns_used", "committed_cost_usd", "deadline_at"):
        assert after["factory"][key] == before["factory"][key]
    assert after["factory"]["starts"][0]["status"] == "failed"
    event = after["factory"]["stop_events"][0]
    assert event["action"] == "stop_settled"
    assert event["cessation_confirmed"] is True
    assert event["intervention_required"] is False
    assert event["session_id"] == s.sid
    assert "identity_sha256" not in event and "claim_owner" not in event
    assert controls.can_start(s.task["id"])["ok"]
    # The old immutable DBOS result cannot reopen the reconciled execution.
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    assert _uncertain_snapshot(s) == after
    assert len([call for call in s.calls if call[1] is not None]) == 1
    # A later ordinary tick repairs this supervision-settled terminal run
    # idempotently before deciding whether more work is permitted.
    monkeypatch.setattr(
        controls, "can_start", lambda _: {"ok": False, "reason": "task_paused"}
    )
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert _uncertain_snapshot(s) == after
    with Session(s.engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == "stop_settled")
                ).all()
            )
            == 1
        )


def test_evicted_guest_settles_factory_without_committed_stop_intent(
    uncertain_factory, monkeypatch
):
    """The headline case, in the shape the control plane actually returns.

    SessionStopProof.identity/2 (projects/embervm/control/lib/embervm/
    session_stop_proof.ex) returns nil for every non-running session, and
    session_manager.ex only falls back to a stop intent's precondition, so a
    guest evicted before any stop was sent reports stop_precondition null.
    """
    import json
    from datetime import datetime, timedelta, timezone
    from sqlmodel import Session, select
    from swarm import factory_controls as controls
    from swarm import factory_supervision as supervisor
    from swarm.factory_models import FactoryAudit

    s = uncertain_factory
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    observed_at = datetime.now(timezone.utc)
    monkeypatch.setattr(supervisor, "_now", lambda: observed_at + timedelta(seconds=1))
    s.cp.update(
        state="evicted",
        stop_precondition=None,
        # The guest ceased after the failure was recorded, which is the
        # ordering the loop requires and the only one production produces.
        updated_at=int((s.failed_turn_at + timedelta(seconds=1)).timestamp() * 1000),
    )

    assert supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    after = _uncertain_snapshot(s)
    assert after["permits"][0]["state"] == "settled"
    assert after["permits"][0]["outcome"] == "guest_cessation_confirmed"
    assert after["runs"][0]["status"] == "failed"
    assert after["factory"]["starts"][0]["status"] == "failed"
    event = after["factory"]["stop_events"][0]
    assert event["action"] == "stop_settled"
    assert event["cessation_confirmed"] is True
    assert event["intervention_required"] is False
    assert all(precondition is None for _guest, precondition in s.calls)
    with Session(s.engine) as db:
        rows = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == s.task["id"],
                FactoryAudit.action.in_(("stop_intent", "stop_settled")),
            )
        ).all()
        assert [row.action for row in rows] == ["stop_settled"]
        detail = json.loads(rows[0].detail_json)
        assert detail["identity"]["session_id"] == s.sid
        assert detail["completion"]["updated_at"] == s.cp["updated_at"]
        assert (
            controls.task_snapshot(s.task["id"], session=db)["unresolved_starts"] == 0
        )


def test_evicted_guest_settles_factory_from_the_committed_stop_intent(
    uncertain_factory, monkeypatch
):
    """A stop was already requested, so a stop_intent audit is committed. The
    guest is then evicted rather than stopped, which drops the CP's
    stop_precondition back to null (SessionStopProof.identity/2 answers only
    for a running session). The committed precondition is the identity then.
    """
    import json
    from datetime import datetime, timedelta, timezone
    from sqlmodel import Session, select
    from swarm import factory_supervision as supervisor
    from swarm.factory_models import FactoryAudit

    s = uncertain_factory
    monkeypatch.delenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", raising=False)
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    with Session(s.engine) as db:
        committed = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "stop_intent")
        ).all()
        assert len(committed) == 1
        assert json.loads(committed[0].detail_json)["precondition"] == s.precondition

    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    observed_at = datetime.now(timezone.utc)
    monkeypatch.setattr(supervisor, "_now", lambda: observed_at + timedelta(seconds=1))
    s.cp.update(
        state="evicted",
        stop_precondition=None,
        stop_intent=None,
        stop_completion=None,
        # The guest ceased after the failure was recorded, which is the
        # ordering the loop requires and the only one production produces.
        updated_at=int((s.failed_turn_at + timedelta(seconds=1)).timestamp() * 1000),
    )
    assert supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    after = _uncertain_snapshot(s)
    assert after["permits"][0]["state"] == "settled"
    assert after["permits"][0]["outcome"] == "guest_cessation_confirmed"
    assert after["runs"][0]["status"] == "failed"


def test_supervision_holds_until_the_computed_stop_deadline(
    uncertain_factory, monkeypatch
):
    """_locked_attempt reads its deadline from _stop_deadline and nowhere else.

    A deadline still ahead refuses the tick with factory_stop_not_due, which
    makes no control-plane call and records no observation. Moving it behind
    the clock releases exactly the same tick.
    """
    from datetime import datetime, timedelta, timezone
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    monkeypatch.setattr(
        supervisor,
        "_stop_deadline",
        lambda *_args: datetime.now(timezone.utc) + timedelta(hours=4),
    )
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    assert s.calls == []
    assert _uncertain_snapshot(s)["factory"]["stop_events"] == []

    monkeypatch.setattr(
        supervisor,
        "_stop_deadline",
        lambda *_args: datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    assert [guest for guest, _precondition in s.calls] == [
        "s-exact-factory",
        "s-exact-factory",
    ]


def test_evicted_guest_with_a_foreign_stop_precondition_is_refused(
    uncertain_factory, monkeypatch
):
    """A populated precondition still has to name this guest and invocation."""
    from datetime import timedelta
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    s.cp.update(
        state="evicted",
        updated_at=int((s.failed_turn_at + timedelta(seconds=1)).timestamp() * 1000),
    )
    s.cp["stop_precondition"] = {**s.precondition, "generation": 7}
    identity = {
        "guest_id": "s-exact-factory",
        "dispatched_at": s.dispatched_at.isoformat(),
        "failed_turn_at": s.failed_turn_at.isoformat(),
    }
    assert supervisor._control_plane_cessation(s.cp, identity) is None
    saved = {"precondition": {**s.precondition, "generation": 7}}
    s.cp["stop_precondition"] = None
    assert supervisor._control_plane_cessation(s.cp, identity, saved) is None
    assert supervisor._control_plane_cessation(s.cp, identity) is not None


def test_evicted_factory_guest_does_not_settle_without_new_flag(
    uncertain_factory, monkeypatch
):
    from datetime import datetime, timedelta, timezone
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    monkeypatch.delenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", raising=False)
    observed_at = datetime.now(timezone.utc)
    monkeypatch.setattr(supervisor, "_now", lambda: observed_at + timedelta(seconds=1))
    s.cp.update(
        state="evicted",
        # The guest ceased after the failure was recorded, which is the
        # ordering the loop requires and the only one production produces.
        updated_at=int((s.failed_turn_at + timedelta(seconds=1)).timestamp() * 1000),
    )
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    assert _uncertain_snapshot(s)["permits"][0]["state"] == "uncertain"


def test_new_flag_does_not_poll_factory_before_stop_deadline(
    uncertain_factory, monkeypatch
):
    from datetime import datetime, timedelta, timezone
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    before_deadline = datetime.now(timezone.utc) - timedelta(seconds=119)
    monkeypatch.setattr(supervisor, "_now", lambda: before_deadline)
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    assert s.calls == []


def test_old_cessation_timestamp_falls_through_to_stop_completion(
    uncertain_factory, monkeypatch
):
    from datetime import datetime, timedelta, timezone
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    monkeypatch.delenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", raising=False)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(supervisor, "_now", lambda: now + timedelta(seconds=1))
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    s.complete()
    dispatched_at = now - timedelta(days=1)
    identity = {
        "guest_id": "s-exact-factory",
        "dispatched_at": dispatched_at.isoformat(),
        "failed_turn_at": s.failed_turn_at.isoformat(),
    }
    s.cp["updated_at"] = int(dispatched_at.timestamp() * 1000)
    assert supervisor._control_plane_cessation(s.cp, identity) is None
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    assert supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    assert _uncertain_snapshot(s)["permits"][0]["state"] == "settled"


def test_factory_cessation_rejects_reinvoked_guest(uncertain_factory):
    """An invoke that began after the failure was recorded is a later attempt.

    agent_sessions/store.py stamps last_dispatch_at when the executor claims
    the turn, before the invoke, so dispatched_at cannot separate this attempt
    from the next one. The failed turn's created_at can: the invocation that
    failed had to start before the failure was written.
    """
    from datetime import timedelta
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    started = int((s.failed_turn_at + timedelta(seconds=1)).timestamp() * 1000)
    s.cp.update(
        state="evicted",
        invoke_started_at=started,
        last_invoke_at=started + 1,
        updated_at=started + 2,
    )
    s.cp["stop_precondition"] = {
        **s.precondition,
        "invoke_started_at": started,
    }
    identity = {
        "guest_id": "s-exact-factory",
        "dispatched_at": s.dispatched_at.isoformat(),
        "failed_turn_at": s.failed_turn_at.isoformat(),
    }
    assert supervisor._control_plane_cessation(s.cp, identity) is None
    # The same view, moved back before the recorded failure, is accepted.
    started = int((s.failed_turn_at - timedelta(seconds=1)).timestamp() * 1000)
    s.cp.update(
        invoke_started_at=started,
        last_invoke_at=started + 1,
        updated_at=int((s.failed_turn_at + timedelta(seconds=1)).timestamp() * 1000),
    )
    s.cp["stop_precondition"] = {**s.precondition, "invoke_started_at": started}
    assert supervisor._control_plane_cessation(s.cp, identity) is not None


@pytest.mark.parametrize("evidence", ["generation", "invoke_stamp", "completed_reset"])
def test_same_guest_restart_evidence_settles_the_old_invocation(
    uncertain_factory, monkeypatch, evidence
):
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    # Commit the exact pre-restart operation identity first.
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    if evidence == "generation":
        current = {
            **s.precondition,
            "generation": s.precondition["generation"] + 1,
            "invoke_started_at": None,
            "vm_id": "vm-relit",
            "instance_id": "node-1/pod-relit",
            "pod_uid": "pod-relit",
            "boot_id": "boot-relit",
        }
    elif evidence == "invoke_stamp":
        current = {
            **s.precondition,
            "invoke_started_at": s.precondition["invoke_started_at"] + 10_000,
        }
    else:
        current = {**s.precondition, "invoke_started_at": None}
    s.cp.update(
        state="running",
        generation=current["generation"],
        invoke_started_at=current["invoke_started_at"],
        last_invoke_at=(
            s.precondition["invoke_started_at"] + 1
            if evidence == "completed_reset"
            else None
        ),
        stop_precondition=current,
        stop_intent=None,
        stop_completion=None,
    )

    assert supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    after = _uncertain_snapshot(s)
    assert after["permits"][0]["state"] == "settled"
    assert after["permits"][0]["outcome"] == "guest_cessation_confirmed"
    proof = _stop_events(s)[-1]["completion"]
    assert proof["session_id"] == "s-exact-factory"
    assert (
        proof["replacement_evidence"]
        == {
            "generation": "generation_advanced",
            "invoke_stamp": "invoke_advanced",
            "completed_reset": "invoke_completed",
        }[evidence]
    )
    # Reconciliation observes only. It does not invoke or relight the guest.
    assert len([call for call in s.calls if call[1] is not None]) == 1


@pytest.mark.parametrize(
    "mismatch,expected_error",
    [
        ("guest", "wrong_stop_observation"),
        ("precondition_guest", "wrong_stop_session"),
        ("older_invoke", "changed_stop_invocation"),
        ("reset_unknown", "changed_stop_invocation"),
    ],
)
def test_restart_evidence_refuses_foreign_or_nonmonotonic_invocations(
    uncertain_factory, monkeypatch, mismatch, expected_error
):
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    current = dict(s.precondition)
    if mismatch == "guest":
        s.cp["session_id"] = "s-unrelated"
    elif mismatch == "precondition_guest":
        current["session_id"] = "s-unrelated"
        current["invoke_started_at"] += 10_000
        s.cp["invoke_started_at"] = current["invoke_started_at"]
    elif mismatch == "older_invoke":
        current["invoke_started_at"] -= 1
        s.cp["invoke_started_at"] = current["invoke_started_at"]
    else:
        current["invoke_started_at"] = None
        s.cp["invoke_started_at"] = None
    s.cp.update(
        state="running",
        last_invoke_at=None,
        stop_precondition=current,
        stop_intent=None,
        stop_completion=None,
    )

    before = _uncertain_snapshot(s)
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    after = _uncertain_snapshot(s)
    for key in ("session", "turns", "pending", "permits", "runs"):
        assert after[key] == before[key]
    observations = [
        event
        for event in after["factory"]["stop_events"]
        if event.get("reason") == "stop_evidence_or_ownership_changed"
    ]
    assert len(observations) == 1
    assert observations[0]["error"] == expected_error


@pytest.mark.parametrize("failure", ["permit", "graph", "start"])
@pytest.mark.parametrize("cleanup_claim", [False, True])
def test_stop_settlement_rollback_retains_all_original_holds(
    uncertain_factory, monkeypatch, failure, cleanup_claim
):
    from swarm import factory_controls as controls
    from swarm import factory_supervision as supervisor
    from agent_sessions import admission

    s = uncertain_factory
    if cleanup_claim:
        _factory_cleanup_claim(s)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    before = _uncertain_snapshot(s)
    s.complete()
    if failure == "permit":

        def refuse(*args, **kwargs):
            raise ValueError("injected permit failure")

        monkeypatch.setattr(admission, "settle", refuse)
    elif failure == "graph":
        monkeypatch.setattr(
            supervisor.graph,
            "record_outcome",
            lambda *args, **kwargs: SimpleNamespace(ok=False),
        )
    else:
        monkeypatch.setattr(
            controls, "record_start_outcome", lambda *args, **kwargs: {"ok": False}
        )
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    after = _uncertain_snapshot(s)
    for key in ("session", "turns", "pending", "permits", "runs"):
        assert after[key] == before[key]
    assert after["factory"]["starts"] == before["factory"]["starts"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("boot_id", "boot-new"),
        ("pod_uid", "pod-new"),
        ("vm_id", "vm-new"),
        ("operation_id", "stop-new"),
        ("session_id", "s-new"),
        ("generation", False),
        ("completed_at_unix_ms", True),
    ],
)
def test_wrong_or_malformed_completion_cannot_settle(uncertain_factory, field, value):
    s = uncertain_factory
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    before = _uncertain_snapshot(s)
    s.complete()
    s.cp["stop_completion"][field] = value
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    after = _uncertain_snapshot(s)
    for key in ("session", "turns", "pending", "permits", "runs"):
        assert after[key] == before[key]


def test_lost_ack_reuses_committed_stop_without_another_delete(
    uncertain_factory, monkeypatch
):
    from swarm import factory_supervision as supervisor

    s = uncertain_factory

    def lost_ack(guest_id, precondition=None):
        value = s.http(guest_id, precondition)
        if precondition is not None:
            raise TimeoutError("response lost after durable CP acceptance")
        return value

    monkeypatch.setattr(supervisor, "_http", lost_ack)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    for _ in range(3):
        conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    assert len([call for call in s.calls if call[1] is not None]) == 1
    s.complete()
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "failed"


@pytest.mark.parametrize(
    "change", ["operation", "top_invoke", "instance", "stamp_type"]
)
def test_persisted_stop_operation_and_current_invocation_must_match(
    uncertain_factory, change
):
    s = uncertain_factory
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    # Observe and durably retain the CP operation before completion.
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    before = _uncertain_snapshot(s)
    s.complete()
    if change == "operation":
        s.cp["stop_intent"]["operation_id"] = "other-operation"
        s.cp["stop_completion"]["operation_id"] = "other-operation"
    elif change == "top_invoke":
        s.cp["invoke_started_at"] += 1
    elif change == "instance":
        s.cp["stop_intent"]["instance_id"] = "unrelated-instance"
        s.cp["stop_completion"]["instance_id"] = "unrelated-instance"
    else:
        s.cp["stop_intent"]["requested_at_unix_ms"] = 1
        s.cp["stop_completion"]["requested_at_unix_ms"] = True
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    after = _uncertain_snapshot(s)
    for key in ("session", "turns", "pending", "permits", "runs"):
        assert after[key] == before[key]


def test_lost_stop_requests_keep_one_identity_and_persistent_bound(
    uncertain_factory, monkeypatch
):
    import json
    from sqlmodel import Session, select
    from swarm.factory_models import FactoryAudit
    from swarm import factory_supervision as supervisor

    s = uncertain_factory

    def dropped(guest_id, precondition=None):
        if precondition is not None:
            s.calls.append((guest_id, precondition))
            raise TimeoutError("request outcome unknown")
        return dict(s.cp)

    monkeypatch.setattr(supervisor, "_http", dropped)
    for _ in range(8):
        conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    assert len(s.calls) == supervisor.MAX_STOP_REQUESTS == 3
    assert all(call[1] == s.precondition for call in s.calls)
    assert _uncertain_snapshot(s)["permits"][0]["state"] == "uncertain"
    with Session(s.engine) as db:
        audit = db.exec(
            select(FactoryAudit).where(FactoryAudit.action.like("stop_%"))
        ).all()
        assert sum(row.action == "stop_intent" for row in audit) == 1
        assert sum(row.action == "stop_request" for row in audit) == 3
        notes = [
            json.loads(row.detail_json)["reason"]
            for row in audit
            if row.action == "stop_observation"
        ]
        assert set(notes) == {"stop_request_unconfirmed", "stop_request_bound_reached"}


def test_pending_completion_surfaces_one_bounded_intervention_event(
    uncertain_factory, monkeypatch
):
    from datetime import timedelta
    from swarm import factory_controls as controls
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    future = supervisor._now() + timedelta(
        seconds=supervisor.COMPLETION_ALARM_SECONDS + 1
    )
    monkeypatch.setattr(supervisor, "_now", lambda: future)
    for _ in range(3):
        conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    events = controls.task_snapshot(s.task["id"])["stop_events"]
    pending = [
        event for event in events if event.get("reason") == "node_completion_pending"
    ]
    assert len(pending) == 1 and pending[0]["intervention_required"]
    assert not pending[0]["cessation_confirmed"]
    assert _uncertain_snapshot(s)["permits"][0]["state"] == "uncertain"


def test_failed_request_budget_write_prevents_external_stop(
    uncertain_factory, monkeypatch
):
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    original = supervisor._audit

    def refuse(db, pin, action, **detail):
        if action == "stop_request":
            raise ValueError("request budget commit failed")
        return original(db, pin, action, **detail)

    monkeypatch.setattr(supervisor, "_audit", refuse)
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    assert all(call[1] is None for call in s.calls)
    assert _uncertain_snapshot(s)["permits"][0]["state"] == "uncertain"


def test_operator_stop_can_reconcile_before_deadline_without_resuming_work(
    uncertain_factory, monkeypatch
):
    from datetime import timedelta
    from swarm import factory_controls as controls
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    before_deadline = supervisor._now() - timedelta(seconds=119)
    monkeypatch.setattr(supervisor, "_now", lambda: before_deadline)
    assert controls.set_control("stop", "operator")["ok"]
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    assert len([call for call in s.calls if call[1] is not None]) == 1
    s.complete()
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    assert _uncertain_snapshot(s)["permits"][0]["state"] == "settled"
    assert not controls.can_start(s.task["id"])["ok"]


@pytest.mark.parametrize("change", ["pending", "permit_owner", "turn", "guest", "run"])
def test_changed_local_owner_after_get_cannot_authorize_stop(
    uncertain_factory, monkeypatch, change
):
    from sqlmodel import Session, select
    from agent_sessions.models import (
        AgentSession,
        AgentTurn,
        PendingMessage,
        AgentCapacityReservation,
    )
    from swarm.models import SwarmNodeRun
    from swarm import factory_supervision as supervisor

    s = uncertain_factory

    def changed(guest_id, precondition=None):
        assert precondition is None, (
            "stale local ownership authorized a destructive request"
        )
        with Session(s.engine) as db:
            if change == "pending":
                db.add(PendingMessage(session_id=s.sid, seq=2, message_text="new work"))
            elif change == "permit_owner":
                row = db.exec(select(AgentCapacityReservation)).one()
                row.owner = "new-executor"
                db.add(row)
            elif change == "turn":
                row = db.exec(select(AgentTurn)).one()
                row.result_text = "new evidence"
                db.add(row)
            elif change == "guest":
                row = db.get(AgentSession, s.sid)
                row.ember_session_id = "s-replacement"
                db.add(row)
            else:
                row = db.get(SwarmNodeRun, s.run["id"])
                row.session_id = 999
                db.add(row)
            db.commit()
        return dict(s.cp)

    monkeypatch.setattr(supervisor, "_http", changed)
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, "SUCCESS"
    )
    assert _uncertain_snapshot(s)["permits"][0]["state"] == "uncertain"
    observations = [
        event
        for event in _uncertain_snapshot(s)["factory"]["stop_events"]
        if event.get("reason") == "stop_evidence_or_ownership_changed"
    ]
    assert len(observations) == 1
    assert observations[0]["error"]


@pytest.mark.parametrize(
    "blocked",
    ["disabled", "not_due", "pending_dbos", "missing_identity", "legacy_terminal"],
)
def test_supervision_never_substitutes_silence_or_state_for_stop_authority(
    uncertain_factory, monkeypatch, blocked
):
    from datetime import timedelta
    from swarm import factory_supervision as supervisor

    s = uncertain_factory
    status = "SUCCESS"
    if blocked == "disabled":
        monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "false")
    elif blocked == "not_due":
        real_now = supervisor._now()
        monkeypatch.setattr(
            supervisor, "_now", lambda: real_now - timedelta(seconds=119)
        )
    elif blocked == "pending_dbos":
        status = "PENDING"
    elif blocked == "missing_identity":
        s.cp["stop_precondition"] = None
    else:
        s.cp["state"] = "destroyed"
        s.cp["stop_precondition"] = None
    before = _uncertain_snapshot(s)
    assert not supervisor.reconcile_uncertain_attempt(
        s.run["pin"], s.sid, s.result, status
    )
    assert all(call[1] is None for call in s.calls)
    after = _uncertain_snapshot(s)
    for key in ("session", "turns", "pending", "permits", "runs"):
        assert after[key] == before[key]


@pytest.mark.parametrize("control_action", ["pause_task", "stop", "expired", None])
def test_waiting_factory_priority_and_live_fence_after_capacity_release(
    queued_factory, control_action, monkeypatch
):
    from datetime import timedelta
    from sqlmodel import Session, select
    from swarm import factory_controls as controls
    from swarm.api import factory_session_allowed
    from agent_sessions import admission
    from agent_sessions.models import AgentSession, AgentCapacityReservation

    s = queued_factory
    with Session(s.engine) as db:
        # Fully occupy background capacity before presenting queued project work.
        for i in range(3):
            db.add(
                AgentCapacityReservation(
                    local_session_id=f"occupied-{i}",
                    pending_seq=1,
                    tier="kg" if i < 2 else "probe",
                    model="luna",
                )
            )
        db.commit()
        assert not admission.reserve_start(db, "fresh-kg", tier="kg", model="luna")
        db.rollback()
    if control_action == "expired":
        deadline = controls.task_snapshot(s.task["id"])["deadline_at"]
        from datetime import datetime

        monkeypatch.setattr(
            controls,
            "_now",
            lambda: datetime.fromisoformat(deadline) + timedelta(seconds=1),
        )
    elif control_action:
        assert controls.set_control(
            control_action,
            "operator",
            task_id=s.task["id"] if control_action == "pause_task" else None,
        )["ok"]
    with Session(s.engine) as db:
        row = db.exec(
            select(AgentCapacityReservation).where(
                AgentCapacityReservation.local_session_id == "occupied-0"
            )
        ).one()
        row.state = "settled"
        db.add(row)
        db.commit()
        owner = db.get(AgentSession, s.sid)
        assert factory_session_allowed(owner.local_session_id) is (
            control_action is None
        )
        assert admission.reserve_start(db, "fresh-kg", tier="kg", model="luna") is (
            control_action is not None
        )
        db.rollback()


@pytest.mark.parametrize("cause", ["attempted", "not_timeout"])
def test_conductor_keeps_unknown_execution_fenced(queued_factory, monkeypatch, cause):
    from sqlmodel import Session, select
    from agent_sessions.models import AgentTurn, PendingMessage
    import swarm.node_workflows as nodes

    s = queued_factory
    monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: None)
    if cause == "attempted":
        with Session(s.engine) as db:
            pending = db.exec(select(PendingMessage)).one()
            pending.dispatch_count = 1
            db.add(pending)
            db.commit()
    result = {
        "status": "uncertain",
        "reason": "timeout: unknown"
        if cause == "attempted"
        else "start_failed: unknown",
        "session_id": s.sid,
    }
    dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: result),
    )
    conductor._submit_or_reconcile(s.task, s.run, dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "uncertain"
    with Session(s.engine) as db:
        assert db.exec(select(PendingMessage)).one()
        assert db.exec(select(AgentTurn)).first() is None


def test_expired_task_finishes_only_after_queue_attempt_reconciles(
    queued_factory, monkeypatch
):
    from datetime import datetime, timedelta
    from swarm import factory_controls as controls
    import swarm.node_workflows as nodes

    s = queued_factory
    deadline = controls.task_snapshot(s.task["id"])["deadline_at"]
    monkeypatch.setattr(
        controls,
        "_now",
        lambda: datetime.fromisoformat(deadline) + timedelta(seconds=1),
    )
    monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: None)
    result = {
        "status": "uncertain",
        "reason": "timeout: task expired",
        "session_id": s.sid,
    }
    dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: result),
    )
    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "failed"
    conductor.reconcile_task(s.task["id"], s.policy, dbos)
    receipt = controls.task_snapshot(s.task["id"])
    assert receipt["state"] == "failed"
    assert receipt["deadline_at"] == deadline
    assert receipt["turns_used"] == 0 and receipt["planner_turns_used"] == 1
    assert receipt["evidence"]["state"] == "task_deadline"


def test_legacy_reservation_replays_unchanged_and_uses_original_wait(
    feedback_db, monkeypatch
):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from swarm import factory_controls as controls
    import swarm.node_workflows as nodes

    task, policy = feedback_task()
    node_key = "conductor_1"
    workflow = f"factory-node:{task['id']}:{node_key}:1"
    assert conductor._add(
        task, policy, node_key, "plan", [], "opus", "test:legacy", "legacy replay"
    ).ok
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task['id']}",
        "hydration_branch": "main",
        "workflow_id": workflow,
        "artifact_path": ".factory/legacy.json",
        "artifact_schema": conductor.DECISION_SCHEMA,
    }
    # Reproduce admission by the previous release, before deadline pinning.
    with Session(feedback_db) as db:
        with controls._locked_session(db):
            admitted = conductor.graph.admit_dispatch(
                task["id"],
                node_key,
                dispatch_key=workflow,
                execution_context=context,
                session=db,
            )
            assert admitted.ok
            assert controls.authorize_start(
                task["id"],
                workflow,
                "legacy-controller",
                model="opus",
                max_cost_usd=admitted.pin["max_cost_usd"],
                session=db,
            )["ok"]
        db.commit()
    original = conductor.graph.node_runs(task["id"])
    original_starts = controls.task_snapshot(task["id"])["starts"]
    assert "task_deadline_at" not in original[0]["pin"]
    assert conductor.reserve_node(task["id"], node_key, workflow, context)
    current = conductor.graph.node_runs(task["id"])
    assert current == original and len(current) == 1
    assert "task_deadline_at" not in current[0]["pin"]
    assert controls.task_snapshot(task["id"])["starts"] == original_starts

    waits = []
    monkeypatch.setattr(
        nodes, "observe_clock", lambda: datetime.now(timezone.utc).isoformat()
    )
    monkeypatch.setattr(
        nodes, "_start_node_session", lambda *_: {"started": True, "session_id": 7}
    )
    monkeypatch.setattr(nodes, "_await_node_turn", lambda *args: waits.append(args))
    monkeypatch.setattr(
        nodes,
        "_await_dispatched_node_turn",
        lambda *_: pytest.fail("legacy durable step sequence changed"),
    )
    result = nodes.execute_node.__wrapped__(current[0]["pin"])
    assert len(waits) == 1 and waits[0][0] == 7
    assert result["status"] == "uncertain"


def test_review_model_override_is_refused_before_graph_admission(feedback_db):
    task, policy = feedback_task()
    policy["reviewer_model"] = "opus"
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "review_fix",
            "role": "review",
            "model": "luna",
            "prompt": "review",
            "deps": [],
            "reason": "independent review",
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    audits = feedback_audits(feedback_db, task["id"])
    assert audits[0]["refusal_code"] == "reviewer_model_mismatch"
    assert not any(
        node["node_key"] == "review_fix"
        for node in conductor.graph.load_graph(task["id"])
    )


def _factory_cleanup_claim(s, **changes):
    from datetime import datetime, timezone
    from sqlmodel import Session
    from agent_sessions.models import AgentSession

    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        for field, value in {
            "guest_cleanup_id": "c" * 32,
            "guest_cleanup_guest_id": agent.ember_session_id,
            "guest_cleanup_workflow_id": agent.workflow_id,
            "guest_cleanup_dispatch_json": "[]",
            "guest_cleanup_started_at": datetime.now(timezone.utc),
            **changes,
        }.items():
            setattr(agent, field, value)
        db.add(agent)
        db.commit()


@pytest.mark.parametrize(
    "change",
    [
        {"guest_cleanup_guest_id": "different-guest"},
        {"guest_cleanup_workflow_id": "different-workflow"},
        {"guest_cleanup_id": None},
    ],
)
def test_factory_stop_refuses_mismatched_cleanup_claim(uncertain_factory, change):
    s = uncertain_factory
    _factory_cleanup_claim(s, **change)
    before = _uncertain_snapshot(s)
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    after = _uncertain_snapshot(s)
    for key in ("session", "turns", "pending", "permits", "runs"):
        assert after[key] == before[key]
    assert not s.calls


@pytest.mark.parametrize(
    "change",
    [
        {"guest_cleanup_guest_id": "different-guest"},
        {"guest_cleanup_workflow_id": "different-workflow"},
    ],
)
def test_factory_stop_refuses_cleanup_ownership_changed_after_observation(
    uncertain_factory, change
):
    from sqlmodel import Session
    from agent_sessions.reconciliation import (
        read_uncertain_factory_attempt,
        settle_uncertain_factory_attempt,
    )

    s = uncertain_factory
    _factory_cleanup_claim(s)
    with Session(s.engine) as db:
        identity = read_uncertain_factory_attempt(db, s.run["pin"], s.sid)
    _factory_cleanup_claim(s, **change)
    before = _uncertain_snapshot(s)
    with Session(s.engine) as db:
        with pytest.raises(ValueError, match="cleanup claim ownership conflict"):
            settle_uncertain_factory_attempt(db, s.run["pin"], identity)
        db.rollback()
    assert _uncertain_snapshot(s) == before


@pytest.fixture
def escalating_factory(feedback_db, monkeypatch):
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "false")
    task, policy = feedback_task()
    key = "implement_escalate"
    workflow = f"factory-node:{task['id']}:{key}:1"
    assert conductor._add(
        task, policy, key, "bounded work", [], "luna", "test:escalate", "test"
    ).ok
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task['id']}",
        "workflow_id": workflow,
        "artifact_path": ".factory/escalate.json",
        "artifact_schema": conductor.RESULT_SCHEMA,
        "hydration_branch": "main",
    }
    assert conductor.reserve_node(task["id"], key, workflow, context)
    run = conductor.graph.node_runs(task["id"])[0]
    value = {
        "status": "escalate",
        "summary": "Needs a stronger worker",
        "reason": "Implementation exceeds the current worker's capability",
        "requested_model": "opus",
        "pr_number": None,
        "head_sha": None,
    }
    result = {
        "status": "escalated",
        "session_id": 77,
        "cost_usd": 0.25,
        "value": value,
        "artifact": {"status": "ok", "value": value, "errors": []},
    }
    dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: result),
        start_workflow=lambda *_: pytest.fail("must not invoke another worker"),
    )
    return SimpleNamespace(
        engine=feedback_db, task=task, policy=policy, run=run, result=result, dbos=dbos
    )


@pytest.mark.parametrize("cost", [0.25, None, 35.0])
@pytest.mark.parametrize("recovered", [False, True])
def test_escalation_settles_both_ledgers_without_retry_or_successful_dependency(
    escalating_factory, monkeypatch, cost, recovered
):
    import json
    from swarm import factory_controls as controls
    from swarm import node_workflows as nodes

    s = escalating_factory
    s.result["cost_usd"] = cost
    before = controls.task_snapshot(s.task["id"])
    assert conductor._add(
        s.task,
        s.policy,
        "review_blocked",
        "review",
        [s.run["node_key"]],
        "opus",
        "test:dependent",
        "test",
        review=True,
    ).ok
    if recovered:
        s.dbos.get_workflow_status = lambda _: SimpleNamespace(status="ERROR")
        monkeypatch.setattr(nodes, "reconcile_completed_node", lambda *_: s.result)
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["status"] == "escalated" and run["finished_at"] is not None
    assert run["pin"] == s.run["pin"]
    assert json.loads(run["outcome_json"]) == s.result
    assert run["accounted_cost_usd"] == (2 if cost is None else cost)
    current = controls.task_snapshot(s.task["id"])
    assert current["starts"][0]["status"] == "failed"
    assert current["starts"][0]["session_id"] == 77
    assert current["starts"][0]["cost_usd"] == cost
    assert current["unresolved_starts"] == 0
    assert current["turns_used"] == before["turns_used"] == 1
    assert current["committed_cost_usd"] == (2 if cost is None else cost)
    assert current["deadline_at"] == before["deadline_at"]
    assert current["policy"] == before["policy"]
    # The next tick may ask the conductor to replan; it cannot retry the worker
    # or treat escalation as a successful dependency, even with attempts left.
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert conductor.graph.node_runs(s.task["id"]) == [run]
    assert controls.task_snapshot(s.task["id"])["starts"] == current["starts"]
    if cost != 35.0:
        assert any(
            n["node_key"] == "conductor_1"
            for n in conductor.graph.load_graph(s.task["id"])
        )


@pytest.mark.parametrize("previous", ["reserved", "uncertain", "failed"])
def test_historical_escalation_settles_exact_start_before_further_admission(
    escalating_factory, monkeypatch, previous
):
    import json
    from swarm import factory_controls as controls

    s = escalating_factory
    s.result["cost_usd"] = None
    key = s.run["pin"]["workflow_id"]
    if previous != "reserved":
        assert controls.record_start_outcome(
            s.task["id"], key, previous, "test", session_id=77
        )["ok"]
    assert conductor.graph.record_outcome(
        s.task["id"],
        s.run["node_key"],
        1,
        "escalated",
        None,
        None,
        json.dumps(s.result),
    ).ok
    before = conductor.graph.node_runs(s.task["id"])
    checks = []

    def check_after_settlement(task_id):
        checks.append(controls.task_snapshot(task_id))
        return {"ok": False, "reason": "task_paused"}

    monkeypatch.setattr(controls, "can_start", check_after_settlement)
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert len(checks) == 2 and checks[0] == checks[1]
    assert checks[0]["starts"][0]["status"] == "failed"
    assert checks[0]["unresolved_starts"] == 0
    assert checks[0]["committed_cost_usd"] == 2
    assert checks[0]["turns_used"] == 1
    assert conductor.graph.node_runs(s.task["id"]) == before


@pytest.mark.parametrize(
    "status,cost,session_id,reason",
    [
        ("succeeded", 0.25, 77, "conflicting_outcome"),
        ("failed", 0.5, 77, "conflicting_outcome"),
        ("uncertain", None, 78, "conflicting_session"),
    ],
)
def test_historical_escalation_conflict_stops_before_new_work(
    escalating_factory, monkeypatch, status, cost, session_id, reason
):
    import json
    from swarm import factory_controls as controls

    s = escalating_factory
    assert controls.record_start_outcome(
        s.task["id"],
        s.run["pin"]["workflow_id"],
        status,
        "test",
        cost_usd=cost,
        session_id=session_id,
    )["ok"]
    assert conductor.graph.record_outcome(
        s.task["id"],
        s.run["node_key"],
        1,
        "escalated",
        0.25,
        None,
        json.dumps(s.result),
    ).ok
    before = controls.task_snapshot(s.task["id"])
    monkeypatch.setattr(
        controls, "can_start", lambda *_: pytest.fail("conflict must block admission")
    )
    with pytest.raises(ValueError, match=reason):
        conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert controls.task_snapshot(s.task["id"]) == before


def test_escalation_settlement_rolls_back_both_ledgers_on_audit_failure(
    escalating_factory,
):
    from sqlalchemy import event
    from sqlmodel import Session
    from swarm import factory_controls as controls
    from swarm.factory_models import FactoryAudit

    s = escalating_factory
    before_runs = conductor.graph.node_runs(s.task["id"])
    before = controls.task_snapshot(s.task["id"])

    def reject_settlement_audit(db, *_):
        if any(
            isinstance(row, FactoryAudit) and row.action == "record_start_outcome"
            for row in db.new
        ):
            raise RuntimeError("injected audit failure")

    event.listen(Session, "before_flush", reject_settlement_audit)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    finally:
        event.remove(Session, "before_flush", reject_settlement_audit)
    assert conductor.graph.node_runs(s.task["id"]) == before_runs
    assert controls.task_snapshot(s.task["id"]) == before
    conductor._submit_or_reconcile(s.task, s.run, s.dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "escalated"
    assert controls.task_snapshot(s.task["id"])["unresolved_starts"] == 0


@pytest.mark.parametrize("selected_profile", [None, "historical-profile"])
def test_planner_keeps_pin_profile_distinct_from_current_node_and_provider(
    monkeypatch, selected_profile
):
    import json

    task, runs = delivery(monkeypatch)
    task["task_text"] = "Preserve historical dispatch evidence."
    run = runs[-1]
    run["pin"]["selected_profile"] = selected_profile
    outcome = json.loads(run["outcome_json"])
    outcome["provider_model"] = "native-provider-model"
    run["outcome_json"] = json.dumps(outcome)
    nodes = [{"node_key": run["node_key"], "model": "current-node-profile", "deps": []}]
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _: [])
    context = planner_context(conductor.planner_prompt(task, nodes, runs))
    expected = selected_profile or "opus"
    assert context["graph"][0]["model"] == "current-node-profile"
    for projected in (
        context["runs"][-1],
        context["delivery_evidence"]["latest_review"],
    ):
        assert projected["selected_profile"] == expected
        assert projected["model"] == "opus"
        assert projected["provider_model"] == "native-provider-model"


@pytest.mark.parametrize("already_settled", [False, True])
def test_stop_after_escalation_settles_task_without_cancelling_worker(
    escalating_factory, monkeypatch, already_settled
):
    from agent_sessions import api
    from swarm import factory_controls as controls

    s = escalating_factory
    s.result["cost_usd"] = None

    async def unexpected_reap(*_):
        pytest.fail("completed escalation needs no external guest cancellation")

    # The execution export is lazy; install the boundary without importing a
    # transport client, since neither successful path should call it.
    monkeypatch.setitem(api.__dict__, "reap_sessions_for_workflow", unexpected_reap)
    s.dbos.cancel_workflow = lambda *_args, **_kwargs: pytest.fail(
        "completed escalation needs no workflow cancellation"
    )
    if already_settled:
        conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    original = controls.task_snapshot(s.task["id"])
    assert controls.set_control("stop", "operator:test")["ok"]
    conductor.cancel_owned(s.task["id"], s.dbos)
    current = controls.task_snapshot(s.task["id"])
    assert current["state"] == "cancelled"
    assert current["unresolved_starts"] == 0
    assert current["starts"][0]["status"] == "failed"
    assert current["starts"][0]["cost_usd"] is None
    assert current["committed_cost_usd"] == original["committed_cost_usd"] == 2
    assert current["turns_used"] == original["turns_used"] == 1
    assert current["deadline_at"] == original["deadline_at"]
    assert current["policy"] == original["policy"]
    run = conductor.graph.node_runs(s.task["id"])[0]
    assert run["status"] == "escalated" and run["pin"] == s.run["pin"]
    assert controls.status()["active_tasks"] == []
    assert controls.status()["state"] == "stopped"


def pooled_task(monkeypatch, quota):
    """Admit a task whose policy pools Codex models ahead of cheaper fallbacks."""
    import swarm.model_pool as model_pool
    from swarm import factory_controls as controls
    from swarm.factory_intake import admit_next, receive_issue

    monkeypatch.setattr(model_pool, "quota_summary", lambda: quota)
    policy = {
        "repo": "owner/repo",
        "issue_numbers": [9],
        "generation": 0,
        "max_tasks": 1,
        "max_turns_per_task": 24,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["astra", "spark", "sol", "sonnet", "opus"],
        "conductor_model": "astra",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "model_pools": {"conductor": ["astra", "spark"], "worker": ["sol", "sonnet"]},
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "task_timeout_seconds": 3600,
        "max_attempts": 2,
    }
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        "owner/repo",
        9,
        "Fix issue",
        "Untrusted issue text",
        "https://github.com/owner/repo/issues/9",
        "poller",
    )
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    return conductor._task(admitted["task_id"]), controls.status()["policy"]


def _node(task_id, node_key):
    return next(
        n for n in conductor.graph.load_graph(task_id) if n["node_key"] == node_key
    )


@pytest.mark.parametrize(
    "quota, expected",
    [
        ({"codex": {"headline_used_percent": 12.0, "age_seconds": 5.0}}, "astra"),
        ({"codex": {"exhausted": True}}, "spark"),
        ({}, "astra"),
    ],
)
def test_first_planner_node_follows_conductor_pool_and_quota(
    feedback_db, monkeypatch, quota, expected
):
    task, policy = pooled_task(monkeypatch, quota)
    conductor.reconcile_task(task["id"], policy, object())
    assert _node(task["id"], "conductor_1")["model"] == expected


def test_planner_add_node_without_model_uses_worker_pool_when_codex_walled(
    feedback_db, monkeypatch
):
    task, policy = pooled_task(monkeypatch, {"codex": {"exhausted": True}})
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "implement_fix",
            "role": "implement",
            "prompt": "do the work",
            "deps": [],
            "reason": "implement",
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    assert _node(task["id"], "implement_fix")["model"] == "sonnet"
    from sqlmodel import Session, select
    from swarm.models import SwarmPlanVersion

    with Session(feedback_db) as db:
        version = db.exec(
            select(SwarmPlanVersion)
            .where(SwarmPlanVersion.task_id == task["id"])
            .order_by(SwarmPlanVersion.version.desc())
        ).first()
    assert "model fallback sol -> sonnet: sol exhausted" in version.stated_reason


def test_planner_add_node_review_never_falls_back(feedback_db, monkeypatch):
    task, policy = pooled_task(
        monkeypatch, {"codex": {"exhausted": True}, "claude": {"exhausted": True}}
    )
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "review_fix",
            "role": "review",
            "prompt": "review the work",
            "deps": [],
            "reason": "review",
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    assert _node(task["id"], "review_fix")["model"] == "opus"


@pytest.mark.parametrize(
    ("chosen", "original", "expected"),
    [
        (None, {"cost_basis": "list", "cost_usd": 0.4}, ("unknown", "unknown_cost")),
        (0.4, {"cost_basis": "list", "cost_usd": 0.4}, ("list", "list_priced_cost")),
        (0.9, {"cost_basis": "list", "cost_usd": 0.4}, ("provider", "reported_cost")),
        (
            0.4,
            {"cost_basis": "provider", "cost_usd": 0.4},
            ("provider", "reported_cost"),
        ),
        (0.4, {}, ("provider", "reported_cost")),
    ],
)
def test_cessation_settlement_writes_basis_and_label_together(
    chosen, original, expected
):
    from swarm.factory_supervision import _settlement_accounting

    basis, label = expected
    assert _settlement_accounting(chosen, original) == {
        "cost_basis": basis,
        "accounting": label,
    }


HEAD_ONE = "a" * 40
HEAD_TWO = "b" * 40


def node_planner_context(node):
    """A planner node's prompt is the boundary, then one line of JSON context."""
    import json

    return json.loads(node["prompt"].rsplit("\n", 1)[1])


def plan_edit(node_key, role, deps=(), **overrides):
    edit = {
        "action": "add_node",
        "reason": f"the plan needs {node_key}",
        "node_key": node_key,
        "role": role,
        "prompt": f"Do the {role} work for {node_key}",
        "deps": list(deps),
    }
    edit.update(overrides)
    return edit


@pytest.mark.parametrize(
    "task_class", ["advisory-diagnosis", "advisory-triage", "refine"]
)
@pytest.mark.parametrize("role", ["investigate", "implement", "review"])
def test_advisory_task_refuses_planner_dag_edits(feedback_db, task_class, role):
    task, policy = feedback_task(task_class=task_class)
    with pytest.raises(conductor._EditRefused) as exc:
        conductor._prepare_add(task, policy, plan_edit("work", role))
    assert exc.value.code == "advisory_task_no_dag"


def test_judgment_task_refuses_a_named_cheap_reviewer(feedback_db):
    """Review is not exempt from the floor: a cheaper reviewer for judgment
    work could never run, because dispatch makes it wait for Opus."""
    task, policy = feedback_task(
        task_class="judgment-analysis",
        allowed_models=["opus", "astra", "luna"],
        model_pools={"conductor": ["opus"], "reviewer": ["opus", "astra"]},
    )
    with pytest.raises(conductor._EditRefused) as exc:
        conductor._prepare_add(task, policy, plan_edit("gate", "review", model="astra"))
    assert exc.value.code == "below_judgment_floor"


def test_judgment_task_uses_floor_and_refuses_named_cheap_model(feedback_db):
    task, policy = feedback_task(
        task_class="judgment-analysis",
        model_pools={"conductor": ["opus"], "worker": ["luna", "opus"]},
    )
    prepared = conductor._prepare_add(task, policy, plan_edit("work", "implement"))
    assert prepared["model"] == "opus"
    assert "model fallback luna -> opus" in prepared["stated_reason"]
    with pytest.raises(conductor._EditRefused) as exc:
        conductor._prepare_add(
            task,
            policy,
            plan_edit("named", "implement", model="luna"),
        )
    assert exc.value.code == "below_judgment_floor"


def test_machine_verified_task_still_uses_pool_selection(feedback_db, monkeypatch):
    task, policy = feedback_task(task_class="bug-fix")
    calls = []

    def choose(role, selected_policy):
        calls.append((role, selected_policy))
        return {
            "model": "luna",
            "preferred": "luna",
            "fallback_from": None,
            "skipped": [],
            "reason": "test",
        }

    monkeypatch.setattr(conductor, "select_model", choose)
    assert (
        conductor._prepare_add(task, policy, plan_edit("work", "implement"))["model"]
        == "luna"
    )
    # An implement node draws on the implement pool, which defaults to the
    # worker pool; any other role stays on the worker pool itself.
    assert calls == [("implement", policy)]
    conductor._prepare_add(task, policy, plan_edit("look", "investigate"))
    assert calls[1] == ("worker", policy)


def test_planner_prompt_names_only_the_judgment_floor(feedback_db):
    task, _policy = feedback_task()
    judgment = conductor.planner_prompt(task, [], [], task_class="judgment-analysis")
    machine = conductor.planner_prompt(task, [], [], task_class="bug-fix")
    sentence = "every implementation node runs on an Opus-class model"
    assert sentence in judgment
    assert sentence not in machine


def test_reconcile_passes_receipt_class_to_planner_prompt(feedback_db):
    task, policy = feedback_task(task_class="judgment-analysis")
    conductor.reconcile_task(task["id"], policy, object())
    planner = conductor.graph.load_graph(task["id"])[0]
    assert "every implementation node runs on an Opus-class model" in planner["prompt"]


def planned_task(feedback_task_result, edits, **decision):
    """Run one planner node whose decision is a batched plan."""
    task, policy = feedback_task_result
    body = {"action": "plan", "reason": "Deliver the requested outcome", "edits": edits}
    body.update(decision)
    run = complete_feedback_node(task, policy, "conductor_1", body)
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    return task, policy


def test_plan_action_is_a_valid_decision_and_bounds_its_edits():
    assert not schema_errors(
        {
            "action": "plan",
            "reason": "Deliver the fix",
            "edits": [plan_edit("scope", "investigate")],
        },
        conductor.DECISION_SCHEMA,
    )
    assert schema_errors(
        {"action": "plan", "reason": "no edits", "edits": []},
        conductor.DECISION_SCHEMA,
    )
    assert schema_errors(
        {
            "action": "plan",
            "reason": "too many",
            "edits": [plan_edit("scope", "investigate")]
            * (conductor.MAX_PLAN_EDITS + 1),
        },
        conductor.DECISION_SCHEMA,
    )
    # A nested edit cannot smuggle a finish, a nested plan or a policy field.
    assert schema_errors(
        {
            "action": "plan",
            "reason": "nested",
            "edits": [{"action": "finish", "reason": "done", "pr_number": 3}],
        },
        conductor.DECISION_SCHEMA,
    )
    assert schema_errors(
        {
            "action": "plan",
            "reason": "policy",
            "edits": [plan_edit("scope", "investigate")],
            "max_review_rounds": 9,
        },
        conductor.DECISION_SCHEMA,
    )
    assert schema_errors(
        {
            "action": "plan",
            "reason": "policy in an edit",
            "edits": [plan_edit("scope", "investigate", max_review_rounds=9)],
        },
        conductor.DECISION_SCHEMA,
    )


def test_plan_applies_a_whole_dag_under_one_expected_version(feedback_db):
    task, policy = planned_task(
        feedback_task(),
        [
            plan_edit("scope", "investigate"),
            plan_edit("fix", "implement", ["investigate_scope"]),
            plan_edit("check", "review", ["implement_fix"]),
        ],
    )
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert set(nodes) == {
        "conductor_1",
        "investigate_scope",
        "implement_fix",
        "review_check",
    }
    assert [
        nodes[key]["created_in_version"]
        for key in ("investigate_scope", "implement_fix", "review_check")
    ] == [2, 3, 4]
    assert conductor.graph.current_version(task["id"]) == 4
    assert nodes["review_check"]["deps"] == ["implement_fix"]
    assert nodes["review_check"]["model"] == "opus"
    assert nodes["implement_fix"]["model"] == "luna"
    assert nodes["implement_fix"]["max_cost_usd"] == policy["turn_budget_usd"]
    assert nodes["review_check"]["prompt"].startswith(
        conductor._boundary(task, review=True)
    )
    assert feedback_audits(feedback_db, task["id"]) == []


@pytest.mark.parametrize(
    "bad, code",
    [
        ({"model": "fable"}, "model_not_allowed"),
        ({"max_cost_usd": 99.0}, "bound_exceeds_policy"),
        ({"max_attempts": 0}, "bound_invalid"),
        ({"deps": ["never_added"]}, "unknown_dep"),
    ],
)
def test_one_bad_edit_rejects_the_whole_plan(feedback_db, bad, code):
    task, _policy = planned_task(
        feedback_task(),
        [
            plan_edit("scope", "investigate"),
            plan_edit("fix", "implement", **bad),
            plan_edit("check", "review", ["implement_fix"]),
        ],
    )
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])] == [
        "conductor_1"
    ]
    assert conductor.graph.current_version(task["id"]) == 1
    audits = feedback_audits(feedback_db, task["id"])
    assert len(audits) == 1 and audits[0]["refusal_code"] == code
    assert audits[0]["decision_action"] == "plan"
    assert "edit 1" in audits[0]["reason"] and "fix" in audits[0]["reason"]


def test_plan_preserves_the_reserved_conductor_and_engine_round_prefixes(feedback_db):
    task, _policy = planned_task(
        feedback_task(),
        [
            plan_edit("scope", "investigate"),
            plan_edit("review_1", "review"),
        ],
    )
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])] == [
        "conductor_1"
    ]
    audits = feedback_audits(feedback_db, task["id"])
    assert audits[0]["refusal_code"] == "engine_loop_key_reserved"


def test_plan_refuses_a_stale_expected_version_whole(feedback_db):
    task, policy = feedback_task()
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "plan",
            "reason": "Deliver the requested outcome",
            "expected_version": 0,
            "edits": [plan_edit("scope", "investigate")],
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])] == [
        "conductor_1"
    ]
    assert (
        feedback_audits(feedback_db, task["id"])[0]["refusal_code"] == "stale_version"
    )


def test_a_decision_cannot_set_the_server_owned_review_round_bound(feedback_db):
    task, policy = feedback_task()
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "reason": "more rounds please",
            "node_key": "fix",
            "role": "implement",
            "prompt": "Fix it",
            "deps": [],
            "max_review_rounds": 9,
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    audits = feedback_audits(feedback_db, task["id"])
    assert audits[0]["refusal_code"] == "bound_exceeds_policy"
    assert "max_review_rounds" in audits[0]["reason"]
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])] == [
        "conductor_1"
    ]


def reviewed_task(*, verdict="changes_requested", rounds=None, head=HEAD_ONE):
    """One implementation and one independent review, with no planner node."""
    task, policy = feedback_task()
    if rounds is not None:
        policy["max_review_rounds"] = rounds
    complete_feedback_node(
        task,
        policy,
        "implement_fix",
        {
            "status": "complete",
            "summary": "Delivered the fix",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
    )
    complete_feedback_node(
        task,
        policy,
        "review_fix",
        {
            "verdict": verdict,
            "summary": "Tighten the retry bound and add a regression test.",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
        deps=["implement_fix"],
    )
    return task, policy


def test_changes_requested_opens_an_engine_owned_correction_round(feedback_db):
    task, policy = reviewed_task()
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert set(nodes) == {"implement_fix", "review_fix", "correct_1", "review_1"}
    correct = nodes["correct_1"]
    assert correct["deps"] == ["review_fix"]
    # The correction runs on the model that produced the reviewed head.
    assert correct["model"] == "luna"
    assert correct["kind"] == "work" and correct["side_effects"]
    # One attempt: a correction that fails is a deviation for the planner, not
    # a turn to spend again, which is what lets the allowance reserve a round
    # at the two turns it really costs.
    assert correct["max_attempts"] == 1
    assert correct["max_cost_usd"] == policy["turn_budget_usd"]
    assert correct["turn_timeout_seconds"] == policy["turn_timeout_seconds"]
    assert "Tighten the retry bound and add a regression test." in correct["prompt"]
    assert HEAD_ONE in correct["prompt"] and "21" in correct["prompt"]
    assert correct["prompt"].startswith(conductor._boundary(task))
    review = nodes["review_1"]
    assert review["max_attempts"] == 1
    assert review["deps"] == ["correct_1"] and review["model"] == "opus"
    assert review["kind"] == "gate" and not review["side_effects"]
    assert review["prompt"].startswith(conductor._boundary(task, review=True))
    assert conductor._schema("correct_1") is conductor.RESULT_SCHEMA
    assert conductor._schema("review_1") is conductor.REVIEW_SCHEMA


def test_the_correction_round_costs_no_planner_turn_and_is_applied_once(
    feedback_db, monkeypatch
):
    from swarm import factory_controls as controls
    from swarm.models import SwarmPlanVersion
    from sqlmodel import Session, select

    task, policy = reviewed_task()
    before = controls.task_snapshot(task["id"])["turns_used"]
    conductor.reconcile_task(task["id"], policy, object())
    version = conductor.graph.current_version(task["id"])
    assert controls.task_snapshot(task["id"])["turns_used"] == before
    with Session(feedback_db) as db:
        causes = db.exec(select(SwarmPlanVersion.cause_ref)).all()
    assert causes.count("factory-loop:review_1") == 2
    assert conductor._review_rounds_used(task["id"]) == 1
    # The correction already depends on that review, so no second pair opens and
    # the next tick dispatches the correction rather than another planner.
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_ONE}}
    )
    conductor.reconcile_task(task["id"], policy, object())
    assert conductor.graph.current_version(task["id"]) == version
    assert not any(
        node["node_key"].startswith("conductor_")
        for node in conductor.graph.load_graph(task["id"])
    )
    assert any(
        run["node_key"] == "correct_1" for run in conductor.graph.node_runs(task["id"])
    )
    assert controls.task_snapshot(task["id"])["turns_used"] == before + 1


def test_an_approving_review_inserts_no_correction_round(feedback_db):
    task, policy = reviewed_task(verdict="approve")
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert "correct_1" not in keys and "review_1" not in keys
    # Delivery is still the planner's call, so it is asked to finish.
    assert "conductor_1" in keys
    assert conductor._review_rounds_used(task["id"]) == 0


def run_correction_round(task, policy, ordinal, *, verdict, head):
    """Settle one engine-inserted pair the way a guest would."""
    run_feedback_node(
        task,
        f"correct_{ordinal}",
        {
            "status": "complete",
            "summary": f"Applied round {ordinal}",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
    )
    run_feedback_node(
        task,
        f"review_{ordinal}",
        {
            "verdict": verdict,
            "summary": f"Round {ordinal} still needs work.",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
    )


def reviewed_after_three_turns(max_turns, *, head=HEAD_ONE):
    """An investigate, an implement and a review that asked for changes."""
    task, policy = feedback_task(max_turns=max_turns)
    complete_feedback_node(
        task,
        policy,
        "investigate_scope",
        {
            "status": "complete",
            "summary": "scoped",
            "pr_number": None,
            "head_sha": None,
        },
    )
    complete_feedback_node(
        task,
        policy,
        "implement_fix",
        {
            "status": "complete",
            "summary": "Delivered the fix",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
        deps=["investigate_scope"],
    )
    complete_feedback_node(
        task,
        policy,
        "review_fix",
        {
            "verdict": "changes_requested",
            "summary": "Tighten the retry bound and add a regression test.",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
        deps=["implement_fix"],
    )
    return task, policy


def test_an_engine_round_is_admitted_on_its_own_nodes_not_on_the_next_round(
    feedback_db,
):
    """The reserve is the planner's headroom, never a charge on the engine.

    Three spent work turns under an envelope of six. The round this review
    asks for costs two real nodes at one attempt each, which fits. Counting
    the reserve for the round behind it in the same check would ask for eight
    and refuse the first correction the loop ever tries.
    """
    from swarm import factory_controls as controls

    task, policy = reviewed_after_three_turns(6)
    assert controls.task_snapshot(task["id"])["turns_used"] == 3
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_1", "review_1"} <= keys
    assert feedback_audits(feedback_db, task["id"]) == []
    # Three spent turns, the round's two real nodes, and the reserve for the
    # round behind it: seven against an envelope of six. The reserve bounds
    # what the planner may add next rather than what the engine already
    # opened, and the figure admission reads stays clamped to the envelope.
    rounds = conductor._rounds_remaining(task["id"], policy)
    derived = controls.derive_allowance(
        task["id"], policy, review_rounds_remaining=rounds
    )
    assert rounds == 1 and derived["turns"] == 3 + 2 + 2
    assert controls.task_snapshot(task["id"])["allowance"]["turns"] == 6


def test_a_second_engine_round_is_refused_when_its_own_nodes_do_not_fit(feedback_db):
    from swarm import factory_controls as controls

    # Five spent turns once the first round settles, so the second round's two
    # nodes need seven against an envelope of six.
    task, policy = reviewed_after_three_turns(6)
    conductor.reconcile_task(task["id"], policy, object())
    run_correction_round(task, policy, 1, verdict="changes_requested", head=HEAD_TWO)
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert "correct_2" not in keys
    audits = feedback_audits(feedback_db, task["id"])
    assert [audit["refusal_code"] for audit in audits] == ["envelope_exceeded"]
    import json

    detail = json.loads(audits[0]["reason"].split("envelope exceeded: ", 1)[1])
    assert detail["turns"] == {"needed": 5 + 2, "allowed": 6}
    # One turn of six is spare against a round that costs two, which is what
    # the refusal tells the planner it has to work inside.
    assert detail["spare_turns"] == 1
    assert controls.task_snapshot(task["id"])["allowance"]["turns"] == 6


def test_a_second_engine_round_opens_when_its_own_nodes_fit(feedback_db):
    from swarm import factory_controls as controls

    # Two turns of room over the refusing case is all the same round needs.
    task, policy = reviewed_after_three_turns(8)
    conductor.reconcile_task(task["id"], policy, object())
    run_correction_round(task, policy, 1, verdict="changes_requested", head=HEAD_TWO)
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_2", "review_2"} <= keys
    assert feedback_audits(feedback_db, task["id"]) == []
    # Both rounds are spent, so nothing is reserved behind this one.
    assert controls.task_snapshot(task["id"])["allowance"]["turns"] == 5 + 2


def test_the_round_bound_hands_an_unresolved_review_back_to_the_planner(feedback_db):
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    run_correction_round(task, policy, 1, verdict="changes_requested", head=HEAD_TWO)
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_2", "review_2"} <= keys
    assert not any(key.startswith("conductor_") for key in keys)
    run_correction_round(task, policy, 2, verdict="changes_requested", head=HEAD_TWO)

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert "correct_3" not in nodes and "review_3" not in nodes
    assert conductor._review_rounds_used(task["id"]) == 2
    planner = nodes["conductor_1"]
    deviation = node_planner_context(planner)["deviation"]
    assert deviation["code"] == "review_rounds_exhausted"
    assert deviation["node_key"] == "review_2"
    assert "max_review_rounds: 2" in deviation["evidence"]


def test_zero_review_rounds_never_opens_one(feedback_db):
    task, policy = reviewed_task(rounds=0)
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert "correct_1" not in nodes
    assert (
        node_planner_context(nodes["conductor_1"])["deviation"]["code"]
        == "review_rounds_exhausted"
    )


def fail_round_node(task, node_key):
    """Settle one engine round node the way a guest that never pushed does.

    Session 3723 edited five files, reported that it could not run the local
    test tooling, and ended the turn with no commit, no push and no typed
    artifact, so the run failed artifact_missing.
    """
    return run_feedback_node(
        task,
        node_key,
        {
            "status": "needs_work",
            "summary": f"{node_key} edited files and never pushed.",
            "pr_number": 21,
            "head_sha": None,
        },
        status="failed",
        reason="artifact_missing: the turn ended with no typed artifact",
    )


def test_a_failed_correction_opens_the_next_round_against_the_same_review(
    feedback_db, monkeypatch
):
    """The live wedge: a failed correct_1 left nothing that could move the task.

    _pending_correction reads the review as already answered, the planner is
    refused correct_<n> and review_<n>, and a node with runs cannot be
    discarded, so conductor_5 paused task t-5361e8a4 with no supported
    task-local recovery path.
    """
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_ONE}}
    )
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    fail_round_node(task, "correct_1")

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_2", "review_2"} <= set(nodes)
    assert not any(key.startswith("conductor_") for key in nodes)
    # The replacement carries the reviewed head and findings, not the
    # correction that never landed.
    assert nodes["correct_2"]["deps"] == ["review_fix"]
    assert HEAD_ONE in nodes["correct_2"]["prompt"]
    assert "Tighten the retry bound" in nodes["correct_2"]["prompt"]
    assert nodes["review_2"]["deps"] == ["correct_2"]
    # The failed round is still spent against the bound.
    assert conductor._review_rounds_used(task["id"]) == 2
    # Once, not once per tick: the next tick dispatches the correction.
    version = conductor.graph.current_version(task["id"])
    conductor.reconcile_task(task["id"], policy, object())
    assert conductor.graph.current_version(task["id"]) == version
    assert any(
        run["node_key"] == "correct_2" for run in conductor.graph.node_runs(task["id"])
    )


def test_a_delivered_reopened_round_does_not_name_the_failed_one(
    feedback_db, monkeypatch
):
    """The failure that paused t-5361e8a4, now at the moment work is delivered.

    correct_1 keeps its failed run for good, because the graph refuses
    discarding a node that has run. Scanning it as an open failure would hand
    the planner node_failed on a key it may not add and a node it may not
    discard, exactly the prompt shape that produced "no supported task-local
    recovery path".
    """
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_ONE}}
    )
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    fail_round_node(task, "correct_1")
    conductor.reconcile_task(task["id"], policy, object())
    run_correction_round(task, policy, 2, verdict="approve", head=HEAD_TWO)

    nodes = conductor.graph.load_graph(task["id"])
    runs = conductor.graph.node_runs(task["id"])
    assert any(node["node_key"] == "correct_1" for node in nodes)
    deviation = conductor.deviations.factory_deviation(
        nodes,
        runs,
        review_rounds_used=2,
        max_review_rounds=2,
        pending_review=None,
    )
    assert deviation["code"] == "graph_exhausted"

    conductor.reconcile_task(task["id"], policy, object())
    planner = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert node_planner_context(planner["conductor_1"])["deviation"] == deviation


def test_the_newest_round_is_still_scanned_when_it_fails(feedback_db, monkeypatch):
    """Only a superseded round is skipped, never the one that is current."""
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_ONE}}
    )
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    fail_round_node(task, "correct_1")
    conductor.reconcile_task(task["id"], policy, object())
    run_feedback_node(
        task,
        "correct_2",
        {
            "status": "complete",
            "summary": "Applied round 2",
            "pr_number": 21,
            "head_sha": HEAD_TWO,
        },
        head=HEAD_TWO,
    )
    fail_round_node(task, "review_2")

    deviation = conductor.deviations.factory_deviation(
        conductor.graph.load_graph(task["id"]),
        conductor.graph.node_runs(task["id"]),
        review_rounds_used=2,
        max_review_rounds=2,
        pending_review=None,
    )
    assert deviation["code"] == "node_failed"
    assert deviation["node_key"] == "review_2"


def test_an_exhausted_loop_names_the_correction_that_delivered_nothing(
    feedback_db, monkeypatch
):
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_ONE}}
    )
    task, policy = reviewed_task(rounds=1)
    conductor.reconcile_task(task["id"], policy, object())
    fail_round_node(task, "correct_1")

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    deviation = node_planner_context(nodes["conductor_1"])["deviation"]
    assert deviation["code"] == "review_rounds_exhausted"
    assert deviation["node_key"] == "review_fix"
    assert "correct_1 delivered nothing" in deviation["text"]
    assert "artifact_missing" in deviation["text"]


def test_an_exhausted_loop_on_a_delivering_round_names_no_correction(feedback_db):
    """The ordinary bound: every round delivered and the reviewer kept objecting."""
    task, policy = reviewed_task(rounds=1)
    conductor.reconcile_task(task["id"], policy, object())
    run_correction_round(task, policy, 1, verdict="changes_requested", head=HEAD_TWO)

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    deviation = node_planner_context(nodes["conductor_1"])["deviation"]
    assert deviation["code"] == "review_rounds_exhausted"
    assert "delivered nothing" not in deviation["text"]


def test_a_reopened_round_is_briefed_at_the_live_task_branch_head(
    feedback_db, monkeypatch
):
    """A correction can push and then die, so the branch outruns the findings."""
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    fail_round_node(task, "correct_1")
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_TWO}}
    )

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    prompt = nodes["correct_2"]["prompt"]
    assert f"at head {HEAD_ONE}" in prompt
    assert f"has since moved to {HEAD_TWO}" in prompt
    assert "did not complete" in prompt
    # The re-review still names the head the findings were written against.
    assert HEAD_ONE in nodes["review_2"]["prompt"]


def test_a_reopened_round_falls_back_to_the_reviewed_head(feedback_db, monkeypatch):
    """An unreadable branch is absent evidence, not a reason to refuse a round."""
    import httpx

    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    fail_round_node(task, "correct_1")

    def unreachable(*_args):
        raise httpx.ConnectError("github unreachable")

    monkeypatch.setattr(conductor, "github_get", unreachable)
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    prompt = nodes["correct_2"]["prompt"]
    assert f"at head {HEAD_ONE}" in prompt
    assert "has since moved" not in prompt


def test_an_unrun_re_review_leaves_with_the_round_it_belonged_to(
    feedback_db, monkeypatch
):
    """review_1 can never run once correct_1 failed, so it stops being funded."""
    from swarm import factory_controls as controls

    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_ONE}}
    )
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    before = controls.task_snapshot(task["id"])["allowance"]
    fail_round_node(task, "correct_1")

    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert "review_1" not in keys
    assert {"correct_1", "correct_2", "review_2"} <= keys
    after = controls.task_snapshot(task["id"])["allowance"]
    # Three work turns spent (implement_fix, review_fix, correct_1), two slots
    # left in the live graph (correct_2, review_2), and no reserve because both
    # rounds are now spent. correct_1 stays live but has no attempt left, so it
    # contributes nothing. With review_1 still live it would be six.
    assert before["turns"] == 6
    assert after["turns"] == 5


def test_a_delivering_round_keeps_its_re_review(feedback_db):
    """The ordinary path discards nothing: review_1 produced the verdict."""
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    run_correction_round(task, policy, 1, verdict="changes_requested", head=HEAD_TWO)

    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_1", "review_1", "correct_2", "review_2"} <= keys


def test_a_re_review_that_settled_without_a_verdict_opens_the_next_round(
    feedback_db, monkeypatch
):
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_TWO}}
    )
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    run_feedback_node(
        task,
        "correct_1",
        {
            "status": "complete",
            "summary": "Applied round 1",
            "pr_number": 21,
            "head_sha": HEAD_TWO,
        },
        head=HEAD_TWO,
    )
    fail_round_node(task, "review_1")

    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_2", "review_2"} <= keys
    assert not any(key.startswith("conductor_") for key in keys)


def test_a_failed_final_round_reaches_the_planner_as_exhausted(feedback_db):
    task, policy = reviewed_task(rounds=1)
    conductor.reconcile_task(task["id"], policy, object())
    fail_round_node(task, "correct_1")

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert "correct_2" not in nodes and "review_2" not in nodes
    assert conductor._review_rounds_used(task["id"]) == 1
    deviation = node_planner_context(nodes["conductor_1"])["deviation"]
    assert deviation["code"] == "review_rounds_exhausted"
    assert deviation["node_key"] == "review_fix"
    assert "max_review_rounds: 1" in deviation["evidence"]


def test_an_escalated_correction_is_left_to_the_planner(feedback_db):
    """Escalation asks for the planner, so another round would talk over it."""
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    run_feedback_node(
        task,
        "correct_1",
        {
            "status": "escalate",
            "summary": "The findings contradict the issue.",
            "reason": "conflicting authority",
            "pr_number": 21,
            "head_sha": None,
        },
        status="escalated",
    )

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert "correct_2" not in nodes
    deviation = node_planner_context(nodes["conductor_1"])["deviation"]
    assert deviation["code"] == "node_escalated"
    assert deviation["node_key"] == "correct_1"


def test_a_discarded_round_is_not_a_failed_round(feedback_db):
    """A round dropped before it ran leaves the review unanswered, not failed.

    The ordinary changes_requested path reopens it. The failed-round path must
    contribute nothing, or the two would race to insert the same pair.
    """
    task, policy = reviewed_task(rounds=2)
    conductor.reconcile_task(task["id"], policy, object())
    for node_key in ("review_1", "correct_1"):
        assert conductor.graph.discard_node(
            task["id"],
            node_key,
            author_kind="engine",
            author="test",
            cause_kind="factory_conductor",
            cause_ref=f"test:discard:{node_key}",
            stated_reason="test fixture",
            expected_version=conductor.graph.current_version(task["id"]),
        ).ok
    nodes = conductor.graph.load_graph(task["id"])
    runs = conductor.graph.node_runs(task["id"])
    assert conductor._failed_round(nodes, runs) is None
    assert conductor._pending_correction(nodes, runs)["node_key"] == "review_fix"

    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_2", "review_2"} <= keys
    assert not any(key.startswith("conductor_") for key in keys)


def test_a_round_inherits_the_timeouts_the_planner_sized(feedback_db):
    """A correction repeats the implementation and a re-review the review."""
    task, policy = feedback_task(turn_timeout_seconds=3600, task_timeout_seconds=14400)
    assert conductor._add(
        task,
        policy,
        "implement_fix",
        "bounded work",
        [],
        "luna",
        "test:implement_fix",
        "test fixture",
        turn_timeout_seconds=1800,
    ).ok
    run_feedback_node(
        task,
        "implement_fix",
        {
            "status": "complete",
            "summary": "Delivered the fix",
            "pr_number": 21,
            "head_sha": HEAD_ONE,
        },
        head=HEAD_ONE,
    )
    assert conductor._add(
        task,
        policy,
        "review_fix",
        "bounded work",
        ["implement_fix"],
        "opus",
        "test:review_fix",
        "test fixture",
        review=True,
        turn_timeout_seconds=900,
    ).ok
    run_feedback_node(
        task,
        "review_fix",
        {
            "verdict": "changes_requested",
            "summary": "Tighten the retry bound.",
            "pr_number": 21,
            "head_sha": HEAD_ONE,
        },
        head=HEAD_ONE,
    )

    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert policy["turn_timeout_seconds"] == 3600
    assert nodes["correct_1"]["turn_timeout_seconds"] == 1800
    assert nodes["review_1"]["turn_timeout_seconds"] == 900
    # Round 2 repeats round 1, so the sizing carries down the chain.
    run_correction_round(task, policy, 1, verdict="changes_requested", head=HEAD_TWO)
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert nodes["correct_2"]["turn_timeout_seconds"] == 1800
    assert nodes["review_2"]["turn_timeout_seconds"] == 900


def test_a_round_never_widens_past_a_tightened_policy_ceiling(feedback_db):
    task, policy = feedback_task(turn_timeout_seconds=3600, task_timeout_seconds=14400)
    for node_key, model, deps, value in (
        (
            "implement_fix",
            "luna",
            [],
            {
                "status": "complete",
                "summary": "Delivered the fix",
                "pr_number": 21,
                "head_sha": HEAD_ONE,
            },
        ),
        (
            "review_fix",
            "opus",
            ["implement_fix"],
            {
                "verdict": "changes_requested",
                "summary": "Tighten the retry bound.",
                "pr_number": 21,
                "head_sha": HEAD_ONE,
            },
        ),
    ):
        assert conductor._add(
            task,
            policy,
            node_key,
            "bounded work",
            deps,
            model,
            f"test:{node_key}",
            "test fixture",
            review=node_key.startswith("review_"),
            turn_timeout_seconds=3600,
        ).ok
        run_feedback_node(task, node_key, value, head=HEAD_ONE)

    tightened = {**policy, "turn_timeout_seconds": 600}
    conductor.reconcile_task(task["id"], tightened, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert nodes["correct_1"]["turn_timeout_seconds"] == 600
    assert nodes["review_1"]["turn_timeout_seconds"] == 600


def test_the_correction_brief_states_the_completion_contract(feedback_db):
    """Round 1 reported no commit because local test tooling was missing."""
    task, policy = reviewed_task()
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    prompt = nodes["correct_1"]["prompt"]
    assert "confirm the pull request head moved" in prompt
    assert "write the declared JSON artifact" in prompt
    assert "no push and no artifact fails the round" in prompt
    assert "runs on the pull request and not in the guest" in prompt
    # The schema itself still comes from node_workflows._node_prompt.
    assert "$schema" not in prompt and "additionalProperties" not in prompt


def test_absent_policy_review_rounds_uses_the_server_default(feedback_db):
    from swarm.factory_controls import DEFAULT_MAX_REVIEW_ROUNDS

    task, policy = reviewed_task()
    assert "max_review_rounds" not in policy
    for ordinal in range(1, DEFAULT_MAX_REVIEW_ROUNDS + 1):
        conductor.reconcile_task(task["id"], policy, object())
        assert conductor._review_rounds_used(task["id"]) == ordinal
        run_correction_round(
            task, policy, ordinal, verdict="changes_requested", head=HEAD_TWO
        )
    conductor.reconcile_task(task["id"], policy, object())
    assert conductor._review_rounds_used(task["id"]) == DEFAULT_MAX_REVIEW_ROUNDS
    assert any(
        node["node_key"].startswith("conductor_")
        for node in conductor.graph.load_graph(task["id"])
    )


def test_a_failed_node_with_no_retry_names_its_deviation_to_the_planner(feedback_db):
    task, policy = feedback_task()
    assert conductor._add(
        task,
        policy,
        "implement_fix",
        "bounded work",
        [],
        "luna",
        "test:implement_fix",
        "test fixture",
        max_attempts=1,
    ).ok
    run_feedback_node(
        task,
        "implement_fix",
        {"status": "needs_work", "summary": "no", "pr_number": None, "head_sha": None},
        status="failed",
    )
    conductor.reconcile_task(task["id"], policy, object())
    planner = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == "conductor_1"
    )
    deviation = node_planner_context(planner)["deviation"]
    assert deviation["code"] == "node_failed"
    assert deviation["node_key"] == "implement_fix"
    assert "max_attempts: 1" in deviation["evidence"]


def test_the_first_planner_call_is_named_as_the_initial_plan(feedback_db):
    task, policy = feedback_task()
    conductor.reconcile_task(task["id"], policy, object())
    planner = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == "conductor_1"
    )
    assert node_planner_context(planner)["deviation"]["code"] == "initial_plan"


def test_a_maximal_deviation_is_bounded_with_the_rest_of_the_context(
    feedback_db, monkeypatch
):
    # The deviation is why this planner exists, so it must survive trimming and
    # must never be the thing that tips the prompt over its bound and pauses.
    task, policy = feedback_task()
    task = {**task, "task_text": "x" * (conductor.PLANNER_CONTEXT_CHARS * 2)}
    deviation = {
        "code": "node_failed",
        "node_key": "implement_fix",
        "evidence": "e" * 4000,
        "text": "t" * 4000,
    }
    import json

    prompt = conductor.planner_prompt(task, [], [], deviation=deviation)
    context = json.loads(prompt.rsplit("\n", 1)[1])
    assert context["deviation"]["code"] == "node_failed"
    assert context["deviation"]["node_key"] == "implement_fix"
    assert len(conductor._planner_json(context)) <= conductor.PLANNER_CONTEXT_CHARS
    assert context["omitted"]["task_characters"] > 0


def test_a_maximal_deviation_does_not_pause_a_task_that_would_otherwise_plan(
    feedback_db, monkeypatch
):
    from swarm import deviations
    from swarm import factory_controls as controls

    # A long task text is the evidence the shrink loop can trade away to make
    # room, which is the whole point of the deviation living inside the bound.
    task, policy = feedback_task(body="u" * 60000)
    assert conductor._add(
        task,
        policy,
        "implement_fix",
        "bounded work",
        [],
        "luna",
        "test:implement_fix",
        "test fixture",
        max_attempts=1,
    ).ok
    run_feedback_node(
        task,
        "implement_fix",
        {"status": "needs_work", "summary": "no", "pr_number": None, "head_sha": None},
        status="failed",
    )
    nodes = conductor.graph.load_graph(task["id"])
    runs = conductor.graph.node_runs(task["id"])
    # Squeeze the bound to exactly what this evidence needs with no deviation,
    # so a deviation added after the shrink loop could only overflow it.
    import json

    baseline = conductor._planner_context(task, nodes, runs)
    without = len(baseline)
    baseline_omitted = json.loads(baseline)["omitted"]["task_characters"]
    monkeypatch.setattr(conductor, "PLANNER_CONTEXT_CHARS", without)
    monkeypatch.setattr(
        deviations,
        "factory_deviation",
        lambda *_args, **_kwargs: {
            "code": "node_failed",
            "node_key": "implement_fix",
            "evidence": "e" * 4000,
            "text": "t" * 4000,
        },
    )
    conductor.reconcile_task(task["id"], policy, object())
    assert not controls.task_snapshot(task["id"])["task_paused"]
    planner = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == "conductor_1"
    )
    context = node_planner_context(planner)
    assert context["deviation"]["code"] == "node_failed"
    assert context["deviation"]["node_key"] == "implement_fix"
    assert len(conductor._planner_json(context)) <= without
    assert context["deviation"]["evidence"].endswith("[text omitted]")
    assert len(context["deviation"]["evidence"]) <= conductor.PLANNER_TEXT_CHARS
    # The deviation earned its place by displacing other evidence, not by
    # being trimmed away or by tipping the prompt over the bound.
    assert context["omitted"]["task_characters"] > baseline_omitted


def test_a_plan_lands_whichever_order_and_dep_spelling_it_uses(feedback_db):
    task, _policy = planned_task(
        feedback_task(),
        [
            plan_edit("check", "review", ["fix"]),
            plan_edit("fix", "implement", ["investigate_scope"]),
            plan_edit("scope", "investigate"),
        ],
    )
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert set(nodes) == {
        "conductor_1",
        "investigate_scope",
        "implement_fix",
        "review_check",
    }
    # The review named the implementation by the key it wrote, and the batch
    # was written leaves first.
    assert nodes["review_check"]["deps"] == ["implement_fix"]
    assert nodes["implement_fix"]["deps"] == ["investigate_scope"]
    assert [
        nodes[key]["created_in_version"]
        for key in ("investigate_scope", "implement_fix", "review_check")
    ] == [2, 3, 4]
    assert feedback_audits(feedback_db, task["id"]) == []


def test_a_refused_review_round_does_not_switch_the_loop_off(feedback_db, monkeypatch):
    task, policy = reviewed_task()
    real = conductor.graph.apply_edits
    attempts = []

    def refuse_once(*args, **kwargs):
        attempts.append(kwargs.get("cause_ref"))
        if len(attempts) == 1:
            return conductor.graph.GraphOp(
                ok=False,
                version=conductor.graph.current_version(task["id"]),
                refusal_code="stale_version",
                detail="a competing edit landed first",
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(conductor.graph, "apply_edits", refuse_once)
    conductor.reconcile_task(task["id"], policy, object())
    audits = feedback_audits(feedback_db, task["id"])
    assert audits[-1]["refusal_code"] == "stale_version"
    assert audits[-1]["cause"] == "factory-loop:review_1"
    planner = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == "conductor_1"
    )
    assert node_planner_context(planner)["deviation"]["code"] == "loop_insert_refused"
    # A refused round consumed nothing, so the bound is untouched.
    assert conductor._review_rounds_used(task["id"]) == 0

    # The planner answers, and the very next reconciliation opens the round the
    # task is still owed rather than treating that cause as spent forever.
    run_feedback_node(
        task,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "conductor_forbidden",
            "role": "implement",
            "prompt": "not authorized",
            "deps": [],
            "reason": "a decision the server refuses",
        },
    )
    conductor.reconcile_task(task["id"], policy, object())
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert {"correct_1", "review_1"} <= keys
    assert attempts == ["factory-loop:review_1"] * 2
    assert conductor._review_rounds_used(task["id"]) == 1


def test_a_correction_falls_through_a_model_the_policy_no_longer_allows(feedback_db):
    task, policy = feedback_task()
    head = HEAD_ONE
    complete_feedback_node(
        task,
        policy,
        "implement_fix",
        {
            "status": "complete",
            "summary": "Delivered",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
        model="luna",
    )
    complete_feedback_node(
        task,
        policy,
        "review_fix",
        {
            "verdict": "changes_requested",
            "summary": "Needs work.",
            "pr_number": 21,
            "head_sha": head,
        },
        head=head,
        deps=["implement_fix"],
    )
    # The reviewed head's model is no longer permitted, so the loop keeps
    # looking instead of refusing itself out of existence.
    narrowed = {**policy, "allowed_models": ["opus"], "worker_model": "opus"}
    conductor.reconcile_task(task["id"], narrowed, object())
    correct = next(
        node
        for node in conductor.graph.load_graph(task["id"])
        if node["node_key"] == "correct_1"
    )
    assert correct["model"] == "opus"


def test_a_correction_round_names_the_resolved_bound_not_a_missing_field(feedback_db):
    from sqlmodel import Session, select
    from swarm.models import SwarmPlanVersion

    task, policy = reviewed_task()
    assert "max_review_rounds" not in policy
    conductor.reconcile_task(task["id"], policy, object())
    with Session(feedback_db) as db:
        reasons = db.exec(
            select(SwarmPlanVersion.stated_reason).where(
                SwarmPlanVersion.cause_ref == "factory-loop:review_1"
            )
        ).all()
    assert "correction round 1 of 2" in reasons[0]
    assert "None" not in reasons[0]


def test_a_failed_node_with_an_attempt_left_retries_before_the_planner(
    feedback_db, monkeypatch
):
    task, policy = feedback_task()
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": HEAD_ONE}}
    )
    assert conductor._add(
        task,
        policy,
        "implement_fix",
        "bounded work",
        [],
        "luna",
        "test:implement_fix",
        "test fixture",
    ).ok
    run_feedback_node(
        task,
        "implement_fix",
        {"status": "needs_work", "summary": "no", "pr_number": None, "head_sha": None},
        status="failed",
    )
    conductor.reconcile_task(task["id"], policy, object())
    assert not any(
        node["node_key"].startswith("conductor_")
        for node in conductor.graph.load_graph(task["id"])
    )
    assert [r["attempt"] for r in conductor.graph.node_runs(task["id"])] == [1, 2]


def parallel_plan(*, max_parallel_nodes=2, **policy_overrides):
    """A plan whose two implementations have no dependency between them."""
    return planned_task(
        feedback_task(max_parallel_nodes=max_parallel_nodes, **policy_overrides),
        [
            plan_edit("alpha", "implement"),
            plan_edit("beta", "implement"),
            plan_edit("check", "review", ["implement_alpha", "implement_beta"]),
        ],
    )


def task_ref(task_id, head=HEAD_ONE, *, existing=("task",)):
    """A GitHub ref reader that answers only for the branches named."""
    import httpx

    def get(_repo, suffix):
        assert suffix.startswith("git/ref/heads/")
        branch = suffix.removeprefix("git/ref/heads/").replace("%2F", "/")
        known = {"task": f"factory/{task_id}"}
        wanted = {known.get(name, name) for name in existing}
        if branch in wanted:
            return {"object": {"sha": head}}
        response = httpx.Response(
            404, request=httpx.Request("GET", "https://example.test/ref")
        )
        response.raise_for_status()

    return get


def graph_state(task_id):
    return conductor.graph.load_graph(task_id), conductor.graph.node_runs(task_id)


def test_a_wave_takes_its_own_branches_only_once_its_fan_in_exists(feedback_db):
    task, policy = parallel_plan()
    nodes, runs = graph_state(task["id"])
    assert conductor.fan_out_wave(task["id"], nodes, runs, 2) == [
        "implement_alpha",
        "implement_beta",
    ]
    # Until the fan-in is in the graph neither member may start at all: its own
    # branch would be one nothing reads, and the task branch would put two
    # writers on one branch.
    for key in ("implement_alpha", "implement_beta"):
        assert conductor._dispatch_branch(task["id"], key, nodes, runs, 2) is None
    conductor.reconcile_task(task["id"], policy, object())
    nodes, runs = graph_state(task["id"])
    for key in ("implement_alpha", "implement_beta"):
        assert conductor._dispatch_branch(
            task["id"], key, nodes, runs, 2
        ) == conductor.node_branch(task["id"], key)
    # The branch is a sibling of the task branch, never a path below it: git
    # cannot hold refs/heads/factory/<id> and a child of it at once.
    assert conductor.node_branch(task["id"], "implement_alpha") == (
        f"factory/{task['id']}-implement_alpha"
    )


def test_the_engine_fans_a_wave_back_into_the_task_branch(feedback_db):
    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert "integrate_1" in nodes
    integrate = nodes["integrate_1"]
    assert integrate["deps"] == ["implement_alpha", "implement_beta"]
    assert integrate["kind"] == "work" and integrate["side_effects"]
    assert integrate["model"] == "luna"
    assert integrate["max_attempts"] == policy["max_attempts"]
    assert integrate["max_cost_usd"] == policy["turn_budget_usd"]
    for key in ("implement_alpha", "implement_beta"):
        assert conductor.node_branch(task["id"], key) in integrate["prompt"]
    assert f"factory/{task['id']}" in integrate["prompt"]
    assert "report the integrated head" in integrate["prompt"]
    # Review now examines the head integrate reports, not one branch's head.
    assert nodes["review_check"]["deps"] == ["integrate_1"]
    assert conductor._schema("integrate_1") is conductor.RESULT_SCHEMA
    assert conductor._is_implementation("integrate_1")


def test_a_fan_in_never_spends_one_of_the_review_rounds(feedback_db):
    from sqlmodel import Session, select
    from swarm.models import SwarmPlanVersion

    task, policy = parallel_plan()
    before = conductor._rounds_remaining(task["id"], policy)
    conductor.reconcile_task(task["id"], policy, object())
    assert conductor._review_rounds_used(task["id"]) == 0
    assert conductor._rounds_remaining(task["id"], policy) == before
    with Session(feedback_db) as db:
        kinds = {
            version.cause_kind: version.cause_ref
            for version in db.exec(select(SwarmPlanVersion)).all()
            if version.author_kind == "engine"
        }
    assert kinds == {"factory_fanin": "factory-fanin:integrate_1"}


def test_the_fan_in_node_is_inserted_once_across_ticks(feedback_db, monkeypatch):
    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    version = conductor.graph.current_version(task["id"])
    monkeypatch.setattr(conductor, "_dispatch_ready", lambda *_a, **_k: False)
    conductor.reconcile_task(task["id"], policy, object())
    conductor.reconcile_task(task["id"], policy, object())
    assert conductor.graph.current_version(task["id"]) == version
    keys = [n["node_key"] for n in conductor.graph.load_graph(task["id"])]
    assert keys.count("integrate_1") == 1 and "integrate_2" not in keys


def test_a_planner_added_integrate_node_is_not_duplicated(feedback_db, monkeypatch):
    task, policy = planned_task(
        feedback_task(max_parallel_nodes=2),
        [
            plan_edit("alpha", "implement"),
            plan_edit("beta", "implement"),
            plan_edit("merge", "integrate", ["implement_alpha", "implement_beta"]),
            plan_edit("check", "review", ["integrate_merge"]),
        ],
    )
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert "integrate_merge" in keys and "integrate_1" not in keys
    assert feedback_audits(feedback_db, task["id"]) == []
    # A planner-named fan-in gives its wave branches exactly as an engine one does.
    nodes, runs = graph_state(task["id"])
    assert conductor._dispatch_branch(
        task["id"], "implement_alpha", nodes, runs, 2
    ) == conductor.node_branch(task["id"], "implement_alpha")


def test_the_reserved_engine_integrate_key_is_refused_to_a_planner(feedback_db):
    task, _policy = planned_task(
        feedback_task(), [plan_edit("integrate_1", "integrate")]
    )
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])] == [
        "conductor_1"
    ]
    audits = feedback_audits(feedback_db, task["id"])
    assert audits[0]["refusal_code"] == "engine_loop_key_reserved"


def test_a_parallel_limit_of_one_fans_nothing_out(feedback_db, monkeypatch):
    from swarm import factory_controls as controls

    task, policy = parallel_plan(max_parallel_nodes=1)
    assert controls.parallel_limit(policy) == 1
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    # The serial lane reads the pool too now: the factory yields the reserve to
    # the drainers and the probes before it takes a slot of its own.
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    nodes, runs = graph_state(task["id"])
    assert conductor.fan_out_wave(task["id"], nodes, runs, 1) == []
    conductor.reconcile_task(task["id"], policy, object())
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    # No fan-in, no repointed review, and one node on the task branch.
    assert not any(key.startswith("integrate_") for key in keys)
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert nodes["review_check"]["deps"] == ["implement_alpha", "implement_beta"]
    admitted = [
        run
        for run in conductor.graph.node_runs(task["id"])
        if run["status"] == "admitted"
    ]
    assert [run["node_key"] for run in admitted] == ["implement_alpha"]
    assert admitted[0]["pin"]["branch"] == f"factory/{task['id']}"


def test_a_wave_is_capped_at_the_parallel_limit(feedback_db, monkeypatch):
    task, policy = planned_task(
        feedback_task(max_parallel_nodes=2),
        [
            plan_edit("alpha", "implement"),
            plan_edit("beta", "implement"),
            plan_edit("gamma", "implement"),
            plan_edit(
                "check",
                "review",
                ["implement_alpha", "implement_beta", "implement_gamma"],
            ),
        ],
    )
    nodes, runs = graph_state(task["id"])
    assert conductor.fan_out_wave(task["id"], nodes, runs, 2) == [
        "implement_alpha",
        "implement_beta",
    ]
    conductor.reconcile_task(task["id"], policy, object())
    integrate = next(
        n
        for n in conductor.graph.load_graph(task["id"])
        if n["node_key"] == "integrate_1"
    )
    assert integrate["deps"] == ["implement_alpha", "implement_beta"]
    # The node past the limit waits: it takes no branch of its own this wave.
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    conductor.reconcile_task(task["id"], policy, object())
    admitted = {
        run["node_key"]: run["pin"]["branch"]
        for run in conductor.graph.node_runs(task["id"])
        if run["status"] == "admitted"
    }
    assert set(admitted) == {"implement_alpha", "implement_beta"}


def test_two_parallel_nodes_are_admitted_in_one_tick_on_their_own_branches(
    feedback_db, monkeypatch
):
    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    conductor.reconcile_task(task["id"], policy, object())
    runs = {
        run["node_key"]: run
        for run in conductor.graph.node_runs(task["id"])
        if run["status"] == "admitted"
    }
    assert set(runs) == {"implement_alpha", "implement_beta"}
    for key, run in runs.items():
        assert run["pin"]["branch"] == conductor.node_branch(task["id"], key)
        # A first attempt fans out from the task branch head as it stands now.
        assert run["pin"]["hydration_branch"] == f"factory/{task['id']}"


def test_a_retry_of_a_fanned_out_node_resumes_its_own_branch(feedback_db, monkeypatch):
    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    branch = conductor.node_branch(task["id"], "implement_alpha")
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    conductor.reconcile_task(task["id"], policy, object())
    settle_admitted_node(
        task,
        "implement_alpha",
        {"status": "complete", "summary": "no", "pr_number": None, "head_sha": None},
        status="failed",
    )
    settle_admitted_node(
        task,
        "implement_beta",
        {
            "status": "complete",
            "summary": "beta done",
            "pr_number": 21,
            "head_sha": HEAD_ONE,
        },
        head=HEAD_ONE,
    )
    # The branch now exists, so the second attempt resumes it rather than
    # hydrating the task branch without the first attempt's work.
    monkeypatch.setattr(
        conductor, "github_get", task_ref(task["id"], existing=("task", branch))
    )
    conductor.reconcile_task(task["id"], policy, object())
    retry = next(
        run
        for run in conductor.graph.node_runs(task["id"])
        if run["node_key"] == "implement_alpha" and run["attempt"] == 2
    )
    assert retry["pin"]["branch"] == branch
    assert retry["pin"]["hydration_branch"] == branch


def test_a_node_added_after_a_completed_wave_works_on_the_task_branch(
    feedback_db, monkeypatch
):
    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    conductor.reconcile_task(task["id"], policy, object())
    for key in ("implement_alpha", "implement_beta"):
        settle_admitted_node(
            task,
            key,
            {
                "status": "complete",
                "summary": f"{key} done",
                "pr_number": 21,
                "head_sha": HEAD_ONE,
            },
            head=HEAD_ONE,
        )
    conductor.reconcile_task(task["id"], policy, object())
    settle_admitted_node(
        task,
        "integrate_1",
        {
            "status": "complete",
            "summary": "integrated",
            "pr_number": 21,
            "head_sha": HEAD_TWO,
        },
        head=HEAD_TWO,
    )
    # A replan adds one more implementation. Its siblings have already run and
    # been integrated, so it has nobody to fan out beside.
    assert conductor._add(
        task,
        policy,
        "implement_late",
        "late work",
        [],
        "luna",
        "test:late",
        "fixture",
    ).ok
    nodes, runs = graph_state(task["id"])
    assert conductor.fan_out_wave(task["id"], nodes, runs, 2) == []
    assert conductor._dispatch_branch(task["id"], "implement_late", nodes, runs, 2) == (
        f"factory/{task['id']}"
    )
    assert conductor._integration_group(task["id"], nodes, runs, 2) == []


def test_a_dependant_of_a_wave_member_waits_for_the_fan_in(feedback_db):
    task, _policy = planned_task(
        feedback_task(max_parallel_nodes=2),
        [
            plan_edit("alpha", "implement"),
            plan_edit("beta", "implement"),
            plan_edit("follow", "implement", ["implement_alpha"]),
            plan_edit("check", "review", ["implement_follow", "implement_beta"]),
        ],
    )
    nodes, runs = graph_state(task["id"])
    # implement_follow has no dependency path to implement_beta, but its own
    # dependency is not on the task branch yet, so it is not in the wave.
    assert conductor.fan_out_wave(task["id"], nodes, runs, 2) == [
        "implement_alpha",
        "implement_beta",
    ]
    # It may not start at all while the wave is open: the task branch is what
    # the wave's fan-in will write, and its own dependency is not there yet.
    assert (
        conductor._dispatch_branch(task["id"], "implement_follow", nodes, runs, 2)
        is None
    )


def test_a_fanned_out_investigation_is_merged_by_the_same_fan_in(feedback_db):
    task, policy = planned_task(
        feedback_task(max_parallel_nodes=2),
        [
            plan_edit("alpha", "investigate"),
            plan_edit("beta", "implement"),
            plan_edit("check", "review", ["investigate_alpha", "implement_beta"]),
        ],
    )
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    # Every branch the engine hands out is a branch the fan-in merges.
    assert nodes["integrate_1"]["deps"] == ["implement_beta", "investigate_alpha"]
    assert nodes["review_check"]["deps"] == ["integrate_1"]


def test_a_refused_fan_in_never_hands_out_branches(feedback_db, monkeypatch):
    task, policy = parallel_plan()
    monkeypatch.setattr(
        conductor.graph,
        "apply_edits",
        lambda *_a, **kwargs: conductor.graph.GraphOp(
            ok=False, refusal_code="stale_version", detail="raced"
        ),
    )
    monkeypatch.setattr(
        conductor,
        "reserve_node",
        lambda *_a, **_k: pytest.fail("a wave with no fan-in must not dispatch"),
    )
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert "integrate_1" not in nodes
    # The planner is asked, naming the refusal, rather than branches going out.
    assert "conductor_2" in nodes
    deviation = node_planner_context(nodes["conductor_2"])["deviation"]
    assert deviation["code"] == "integration_insert_refused"
    assert "stale_version" in deviation["evidence"]


def admitted_keys(task):
    return [
        run["node_key"]
        for run in conductor.graph.node_runs(task["id"])
        if run["status"] == "admitted"
    ]


def test_a_refused_capacity_reservation_leaves_the_node_ready(feedback_db, monkeypatch):
    from swarm import factory_controls as controls

    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 0)
    conductor.reconcile_task(task["id"], policy, object())
    # An empty pool starts nothing at all, the first node included. Nothing
    # failed and nothing paused: both nodes are simply still ready.
    assert admitted_keys(task) == []
    assert controls.task_snapshot(task["id"])["task_paused"] is False
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 1)
    conductor.reconcile_task(task["id"], policy, object())
    # One slot starts one node. The second waits rather than failing.
    assert admitted_keys(task) == ["implement_alpha"]
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 2)
    settle_admitted_node(
        task,
        "implement_alpha",
        {
            "status": "complete",
            "summary": "alpha done",
            "pr_number": 21,
            "head_sha": HEAD_ONE,
        },
        head=HEAD_ONE,
    )
    conductor.reconcile_task(task["id"], policy, object())
    assert any(
        run["node_key"] == "implement_beta"
        for run in conductor.graph.node_runs(task["id"])
    )


def test_the_factory_leaves_the_shared_reserve_for_everything_else(
    feedback_db, monkeypatch
):
    """The pool is shared with the drainers and the probes, which cannot wait.

    A factory node the pool declines stays ready and starts on a later tick, so
    the factory is the member that yields. At a task ceiling of twelve without
    this, delivery could hold every background slot.
    """
    from swarm import factory_controls as controls

    monkeypatch.setenv("FACTORY_BACKGROUND_RESERVE", "2")
    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    # Two free slots are exactly the reserve, so the factory takes neither.
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 2)
    conductor.reconcile_task(task["id"], policy, object())
    assert admitted_keys(task) == []
    assert controls.task_snapshot(task["id"])["task_paused"] is False
    # A third slot is one the factory may take, and only that one.
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    conductor.reconcile_task(task["id"], policy, object())
    assert admitted_keys(task) == ["implement_alpha"]


def test_the_allowance_is_derived_from_the_accepted_plan(feedback_db):
    from swarm import factory_controls as controls

    task, policy = planned_task(
        feedback_task(),
        [
            plan_edit("scope", "investigate"),
            plan_edit("fix", "implement", ["investigate_scope"]),
            plan_edit("check", "review", ["implement_fix"]),
        ],
    )
    allowance = controls.task_snapshot(task["id"])["allowance"]
    # Three work nodes of two attempts, and the next review round only: one
    # correction and one re-review at a single attempt each. The round behind
    # it is reserved when this one is spent. The planner node costs money but
    # never a work turn.
    assert allowance["derived"] is True
    assert allowance["turns"] == 3 * 2 + 2
    assert allowance["review_rounds_reserved"] == 1
    assert allowance["fan_ins_reserved"] == 0
    assert allowance["graph_revision"] == conductor.graph.current_version(task["id"])
    assert feedback_audits(feedback_db, task["id"]) == []


def test_a_plan_that_fans_out_reserves_its_fan_in_up_front(feedback_db):
    from swarm import factory_controls as controls

    task, policy = parallel_plan()
    allowance = controls.task_snapshot(task["id"])["allowance"]
    assert allowance["fan_ins_reserved"] == 1
    # Two implementations and a review at two attempts, the fan-in the wave
    # will need at the same two, and the next review round at one attempt each.
    assert allowance["turns"] == 2 * 2 + 1 * 2 + policy["max_attempts"] + 2
    before = allowance["turns"]
    conductor.reconcile_task(task["id"], policy, object())
    # The reserve became the real fan-in node, so the insertion cost no turns.
    after = controls.task_snapshot(task["id"])["allowance"]
    assert after["fan_ins_reserved"] == 0 and after["turns"] == before


def test_an_at_envelope_plan_that_fans_out_is_refused_up_front(feedback_db):
    from swarm import factory_controls as controls

    # Exactly the turns the three nodes and the next review round need, with
    # nothing left for the fan-in the wave will require.
    envelope = 2 * 2 + 1 * 2 + 2
    task, policy = parallel_plan(max_turns=envelope)
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])] == [
        "conductor_1"
    ]
    audits = feedback_audits(feedback_db, task["id"])
    assert [audit["refusal_code"] for audit in audits] == ["envelope_exceeded"]
    import json

    detail = json.loads(audits[0]["reason"].split("envelope exceeded: ", 1)[1])
    assert detail["turns"] == {
        "needed": envelope + policy["max_attempts"],
        "allowed": envelope,
    }
    assert controls.task_snapshot(task["id"])["allowance"]["derived"] is False


def test_a_nine_turn_envelope_still_takes_a_review_after_three_spent_turns(
    feedback_db,
):
    """The live defect (t-5e48b6e1, #5981): a task with history could not be reviewed.

    Nine turns, two attempts, two review rounds. An investigate, a failed
    implement and a succeeded reconcile had spent three work turns when the
    planner proposed the one review node the delivery gate needs. Reserving
    both remaining rounds at two nodes of two attempts each asked for thirteen
    turns against nine, so the add was refused, and the planner kept proposing
    it until the planner round cap paused the task.
    """
    from swarm import factory_controls as controls

    task, policy = planned_task(
        feedback_task(max_turns=9),
        [
            plan_edit("scope", "investigate"),
            plan_edit("fix", "implement", ["investigate_scope"]),
            plan_edit("check", "review", ["implement_fix"]),
        ],
    )
    # Three nodes of two attempts and the next round at one attempt each.
    assert controls.task_snapshot(task["id"])["allowance"]["turns"] == 3 * 2 + 2
    run_feedback_node(
        task,
        "investigate_scope",
        {
            "status": "complete",
            "summary": "scoped",
            "pr_number": None,
            "head_sha": None,
        },
    )
    run_feedback_node(
        task,
        "implement_fix",
        {"status": "needs_work", "summary": "no", "pr_number": None, "head_sha": None},
        status="failed",
    )
    complete_feedback_node(
        task,
        policy,
        "implement_reconcile",
        {
            "status": "complete",
            "summary": "fixed",
            "pr_number": 21,
            "head_sha": HEAD_ONE,
        },
        head=HEAD_ONE,
        deps=["investigate_scope"],
    )
    run = complete_feedback_node(
        task,
        policy,
        "conductor_2",
        {
            "action": "add_node",
            "node_key": "reconcile",
            "role": "review",
            "prompt": "Review the reconciled head",
            "deps": ["implement_reconcile"],
            "reason": "the delivery gate needs a review of the reconciled head",
            "max_attempts": 1,
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert "review_reconcile" in keys
    assert feedback_audits(feedback_db, task["id"]) == []
    # Three work turns of history, the implement slot and the review node the
    # plan already held, the review just added, and the next round behind it.
    assert controls.task_snapshot(task["id"])["allowance"]["turns"] == 3 + 3 + 1 + 2


def test_a_refused_add_names_the_turns_that_would_still_fit(feedback_db):
    from swarm import factory_controls as controls

    task, policy = planned_task(
        feedback_task(max_turns=9),
        [
            plan_edit("scope", "investigate"),
            plan_edit("fix", "implement", ["investigate_scope"]),
            plan_edit("check", "review", ["implement_fix"]),
        ],
    )
    accounted = controls.task_snapshot(task["id"])["allowance"]["turns"]
    run = complete_feedback_node(
        task,
        policy,
        "conductor_2",
        {
            "action": "add_node",
            "node_key": "extra",
            "role": "implement",
            "prompt": "More work than the envelope holds",
            "deps": ["implement_fix"],
            "reason": "one node too many",
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    keys = {n["node_key"] for n in conductor.graph.load_graph(task["id"])}
    assert "implement_extra" not in keys
    audits = feedback_audits(feedback_db, task["id"])
    assert [audit["refusal_code"] for audit in audits] == ["envelope_exceeded"]
    import json

    detail = json.loads(audits[0]["reason"].split("envelope exceeded: ", 1)[1])
    assert detail["turns"] == {
        "needed": accounted + policy["max_attempts"],
        "allowed": 9,
    }
    # One turn is spare, so the planner is told to size a single-attempt node
    # rather than re-proposing the two-attempt one that was just refused. The
    # graph already holds a review node, so the round reserve is in both
    # readings and the spare figure is the plan-time allowance's own slack.
    assert detail["spare_turns"] == 9 - accounted == 1
    assert detail["spare_usd"] > 0


def test_an_engine_review_round_grows_the_allowance_by_one_round(feedback_db):
    from swarm import factory_controls as controls

    task, policy = reviewed_task()
    conductor._record_allowance(task["id"], policy, "test:baseline")
    before = controls.task_snapshot(task["id"])["allowance"]
    conductor.reconcile_task(task["id"], policy, object())
    after = controls.task_snapshot(task["id"])["allowance"]
    # The reserve became one real correction and one real re-review, and the
    # round behind it took the reserve's place, so the task grew by exactly one
    # round rather than having paid for every round at plan time.
    assert before["review_rounds_reserved"] == after["review_rounds_reserved"] == 1
    assert after["turns"] == before["turns"] + 2
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    assert nodes["correct_1"]["max_attempts"] == 1
    assert nodes["review_1"]["max_attempts"] == 1


def test_an_over_envelope_plan_is_refused_whole_with_its_excess(feedback_db):
    from swarm import factory_controls as controls

    task, policy = planned_task(
        feedback_task(max_turns=6),
        [
            plan_edit("scope", "investigate"),
            plan_edit("fix", "implement", ["investigate_scope"]),
            plan_edit("check", "review", ["implement_fix"]),
        ],
    )
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])] == [
        "conductor_1"
    ]
    audits = feedback_audits(feedback_db, task["id"])
    assert len(audits) == 1
    assert audits[0]["refusal_code"] == "envelope_exceeded"
    import json

    detail = json.loads(audits[0]["reason"].split("envelope exceeded: ", 1)[1])
    assert detail["turns"] == {"needed": 3 * 2 + 2, "allowed": 6}
    assert detail["usd"]["allowed"] == policy["task_budget_usd"]
    # Only the planner node is live, and the plan's own review node brings the
    # two-turn round reserve with it, so four of the six turns are spare. A
    # spare figure read off a graph with no review node would send the planner
    # back with a six-turn plan that derives eight and is refused again.
    assert detail["spare_turns"] == 4
    assert detail["spare_usd"] == round(policy["task_budget_usd"] - 4.25, 6)
    # Nothing was derived, so admission still reads the envelope.
    assert controls.task_snapshot(task["id"])["allowance"]["derived"] is False
    # The next planner is told exactly what to shrink.
    conductor.reconcile_task(task["id"], policy, object())
    nodes = {n["node_key"]: n for n in conductor.graph.load_graph(task["id"])}
    feedback = node_planner_context(nodes["conductor_2"])["decision_feedback"]
    assert feedback[0]["refusal_code"] == "envelope_exceeded"
    assert '"needed": 8' in feedback[0]["reason"]


def test_discarding_a_node_shrinks_the_allowance_without_refunding_history(
    feedback_db, monkeypatch
):
    from swarm import factory_controls as controls

    task, policy = feedback_task()
    complete_feedback_node(
        task,
        policy,
        "implement_fix",
        {"status": "complete", "summary": "done", "pr_number": None, "head_sha": None},
        status="failed",
    )
    assert conductor._add(
        task, policy, "implement_spare", "spare", [], "luna", "test:spare", "fixture"
    ).ok
    conductor._record_allowance(task["id"], policy, "test:baseline")
    before = controls.task_snapshot(task["id"])["allowance"]["turns"]
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "discard_node",
            "node_key": "implement_spare",
            "reason": "not needed",
        },
    )
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    after = controls.task_snapshot(task["id"])["allowance"]
    # The spare node's two unspent slots go; the failed attempt stays charged.
    assert before - after["turns"] == 2
    assert after["turns"] >= controls.task_snapshot(task["id"])["turns_used"]


def test_a_stale_allowance_is_re_derived_on_the_next_tick(feedback_db, monkeypatch):
    import json
    from sqlmodel import Session, select
    from swarm import factory_controls as controls
    from swarm.factory_models import FactoryReceipt

    task, policy = planned_task(
        feedback_task(),
        [
            plan_edit("fix", "implement"),
            plan_edit("check", "review", ["implement_fix"]),
        ],
    )
    derived = controls.task_snapshot(task["id"])["allowance"]
    # A crash between the accepted edit and its allowance write leaves the
    # stored figure behind the graph revision it was derived from.
    with Session(feedback_db) as db:
        row = db.exec(select(FactoryReceipt)).first()
        row.allowance_json = json.dumps({**derived, "turns": 1, "graph_revision": 0})
        db.add(row)
        db.commit()
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "reserve_node", lambda *_a, **_k: True)
    conductor.reconcile_task(task["id"], policy, object())
    assert controls.task_snapshot(task["id"])["allowance"] == derived


def test_only_wave_members_dispatch_when_the_graph_order_disagrees(
    feedback_db, monkeypatch
):
    """Graph order and wave order differ, so the queue must follow the wave.

    Four independent implementations at a limit of two: the wave is the first
    two by key, which is not the order the plan wrote them in. Dispatching the
    graph's first two would put two writers on the task branch at once.
    """
    task, policy = planned_task(
        feedback_task(max_parallel_nodes=2),
        [
            plan_edit("zulu", "implement"),
            plan_edit("yankee", "implement"),
            plan_edit("alpha", "implement"),
            plan_edit("bravo", "implement"),
            plan_edit(
                "check",
                "review",
                [
                    "implement_zulu",
                    "implement_yankee",
                    "implement_alpha",
                    "implement_bravo",
                ],
            ),
        ],
    )
    nodes, runs = graph_state(task["id"])
    assert [n["node_key"] for n in conductor.graph.load_graph(task["id"])][1:3] == [
        "implement_zulu",
        "implement_yankee",
    ]
    wave = conductor.fan_out_wave(task["id"], nodes, runs, 2)
    assert wave == ["implement_alpha", "implement_bravo"]
    conductor.reconcile_task(task["id"], policy, object())
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    conductor.reconcile_task(task["id"], policy, object())
    admitted = {
        run["node_key"]: run["pin"]["branch"]
        for run in conductor.graph.node_runs(task["id"])
        if run["status"] == "admitted"
    }
    assert set(admitted) == set(wave)
    # Every in-flight node writes a branch of its own, never one shared.
    assert sorted(admitted.values()) == sorted(
        conductor.node_branch(task["id"], key) for key in wave
    )
    assert len(set(admitted.values())) == len(admitted)
    # The nodes outside the wave are refused a branch rather than given the
    # task branch beside it.
    nodes, runs = graph_state(task["id"])
    for key in ("implement_zulu", "implement_yankee"):
        assert conductor._dispatch_branch(task["id"], key, nodes, runs, 2) is None


def test_a_planner_added_integrate_node_fans_nothing_out_at_a_limit_of_one(
    feedback_db, monkeypatch
):
    task, policy = planned_task(
        feedback_task(max_parallel_nodes=1),
        [
            plan_edit("alpha", "implement"),
            plan_edit("beta", "implement"),
            plan_edit("merge", "integrate", ["implement_alpha", "implement_beta"]),
            plan_edit("check", "review", ["integrate_merge"]),
        ],
    )
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    # The serial lane reads the pool too now: the factory yields the reserve to
    # the drainers and the probes before it takes a slot of its own.
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    nodes, runs = graph_state(task["id"])
    # The planner named a fan-in, but the operator did not turn fan-out on, so
    # its members run serially on the task branch and it merges nothing.
    for key in ("implement_alpha", "implement_beta"):
        assert conductor._dispatch_branch(task["id"], key, nodes, runs, 1) == (
            f"factory/{task['id']}"
        )
    conductor.reconcile_task(task["id"], policy, object())
    admitted = [
        run
        for run in conductor.graph.node_runs(task["id"])
        if run["status"] == "admitted"
    ]
    assert len(admitted) == 1
    assert admitted[0]["pin"]["branch"] == f"factory/{task['id']}"


def test_a_split_wave_still_reserves_the_fan_in_it_will_need(feedback_db):
    """Two nodes that fan out together carry different ancestor counts."""
    from swarm import factory_controls as controls

    task, policy = planned_task(
        feedback_task(max_parallel_nodes=2),
        [
            plan_edit("scope", "investigate"),
            plan_edit("alpha", "implement", ["investigate_scope"]),
            plan_edit("beta", "implement", ["investigate_scope"]),
            plan_edit("check", "review", ["implement_alpha", "implement_beta"]),
        ],
    )
    # investigate_scope is not concurrent with anything, so it is not in a
    # group; alpha and beta are, and they still owe one fan-in.
    assert (
        conductor._planned_fan_ins(
            task["id"], policy, conductor.graph.load_graph(task["id"])
        )
        == 1
    )
    assert controls.task_snapshot(task["id"])["allowance"]["fan_ins_reserved"] == 1


def test_a_stale_allowance_is_re_derived_before_a_top_up_dispatch(
    feedback_db, monkeypatch
):
    import json
    from sqlmodel import Session, select
    from swarm import factory_controls as controls
    from swarm.factory_models import FactoryReceipt

    task, policy = parallel_plan()
    conductor.reconcile_task(task["id"], policy, object())
    monkeypatch.setattr(conductor, "github_get", task_ref(task["id"]))
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 0)
    conductor.reconcile_task(task["id"], policy, object())
    derived = controls.task_snapshot(task["id"])["allowance"]
    with Session(feedback_db) as db:
        row = db.exec(select(FactoryReceipt)).first()
        row.allowance_json = json.dumps(
            {**derived, "turns": derived["turns"] + 40, "graph_revision": 0}
        )
        db.add(row)
        db.commit()
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 2)
    monkeypatch.setattr(conductor, "_submit_or_reconcile", lambda *_a: None)
    # One node is still in flight, so this is the top-up path. A stale-high
    # allowance must never be what the second admission is measured against.
    conductor.reconcile_task(task["id"], policy, object())
    assert controls.task_snapshot(task["id"])["allowance"] == derived


def test_ingest_eligible_classifies_the_operators_named_issues(monkeypatch):
    """The floor must not depend on which path found the work."""
    import swarm.factory_intake as intake

    received = []
    monkeypatch.setattr(
        conductor,
        "github_get",
        lambda _repo, _suffix: {
            "state": "open",
            "assignees": [],
            "title": "Judgment",
            "body": "body",
            "html_url": "https://github.com/owner/repo/issues/4",
            "labels": [{"name": "needs-thought"}],
        },
    )
    monkeypatch.setattr(
        intake,
        "receive_issue",
        lambda *_args, **kwargs: received.append(kwargs.get("task_class")),
    )
    conductor.ingest_eligible(
        {"repo": "owner/repo", "issue_numbers": [4], "generation": 0}
    )
    assert received == ["judgment-analysis"]


def test_a_correction_on_judgment_work_stays_at_the_floor():
    policy = {
        "conductor_model": "opus",
        "worker_model": "luna",
        "reviewer_model": "opus",
        "allowed_models": ["opus", "luna"],
        "model_pools": {"worker": ["luna"], "conductor": ["opus"]},
    }
    review_run = {"node_key": "review_1", "id": 2}
    model, provenance = conductor._correction_model(
        [], [], review_run, policy, "judgment-analysis"
    )
    assert model == "opus" and "judgment" in provenance
    model, _ = conductor._correction_model([], [], review_run, policy, "bug-fix")
    assert model == "luna"


def test_tick_ingests_the_operators_issues_before_it_discovers_one(monkeypatch):
    """Both paths write queued receipts and the oldest is admitted first."""
    import swarm.factory_controls as controls
    import swarm.factory_intake as intake
    import swarm.factory_intake_loop as intake_loop

    policy = {"max_tasks": 1, "generation": 0}
    monkeypatch.delenv("FACTORY_MAX_CONCURRENT_TASKS", raising=False)
    monkeypatch.setattr(
        controls,
        "status",
        lambda: {"state": "enabled", "policy": policy, "active_tasks": []},
    )
    monkeypatch.setattr(conductor.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(conductor.runtime, "init_dbos", lambda: object())
    order = []
    monkeypatch.setattr(conductor, "ingest_eligible", lambda _p: order.append("ingest"))
    monkeypatch.setattr(
        intake_loop,
        "intake_tick",
        lambda _p, **_kwargs: order.append("intake"),
    )
    monkeypatch.setattr(intake, "admit_next", lambda _actor, **_kwargs: {"ok": False})
    monkeypatch.setattr(conductor, "reconcile_task", lambda *_args: None)
    conductor.tick()
    assert order == ["ingest", "intake"]


def _review_node(node_key, model="opus"):
    return {
        "node_key": node_key,
        "deps": [],
        "model": model,
        "max_attempts": 1,
        "max_cost_usd": 1.0,
    }


def routed_dispatch(
    monkeypatch, node_keys, *, reviewer, models=None, task_class="bug-fix"
):
    """Drive _dispatch_ready over a fixed ready set and record what was pinned."""
    import swarm.factory_quota_guard as quota_guard
    import swarm.factory_refine as refine

    models = models or {}
    nodes = [_review_node(key, models.get(key, "opus")) for key in node_keys]
    monkeypatch.setattr(conductor, "_ready_nodes", lambda *_a: list(nodes))
    monkeypatch.setattr(conductor, "hydration_branch", lambda _task: "main")
    monkeypatch.setattr(conductor, "branch_hydration", lambda *_a: "main")
    monkeypatch.setattr(conductor, "_dispatch_branch", lambda *_a: "factory/t-1")
    monkeypatch.setattr(conductor, "fan_out_wave", lambda *_a: [])
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: len(nodes))
    monkeypatch.setattr(conductor, "_schema", lambda _key: {})
    monkeypatch.setattr(refine, "task_class_for", lambda _task_id: task_class)
    monkeypatch.setattr(
        quota_guard,
        "reviewer_for",
        lambda _policy, _task_class, **_k: {"model": reviewer, "skipped": []},
    )
    import swarm.factory_controls as controls_module

    monkeypatch.setattr(controls_module, "window_high", lambda **_k: True)
    audited = []
    monkeypatch.setattr(
        conductor,
        "_audit_once",
        lambda task_id, key, action, detail: audited.append((action, detail)) or True,
    )
    started = []

    def reserve(_task_id, node_key, _key, _context, *, model=None):
        started.append((node_key, model))
        return True

    monkeypatch.setattr(conductor, "reserve_node", reserve)
    task = {"id": "t-1", "repo": "owner/repo"}
    policy = {"allowed_models": ["opus", "astra", "luna"]}
    conductor._dispatch_ready(
        task, nodes, [], len(nodes), fan_out=True, parallel=len(nodes), policy=policy
    )
    return SimpleNamespace(started=started, audits=audited)


def test_a_spent_window_pins_the_fallback_reviewer_on_the_attempt(monkeypatch):
    result = routed_dispatch(
        monkeypatch, ["implement_fix", "review_1"], reviewer="astra"
    )
    # The implement node is untouched: only review spends the Claude window.
    assert result.started == [("implement_fix", None), ("review_1", "astra")]


def test_a_quiet_window_pins_nothing_and_keeps_the_planned_reviewer(monkeypatch):
    result = routed_dispatch(
        monkeypatch, ["implement_fix", "review_1"], reviewer="opus"
    )
    assert result.started == [("implement_fix", None), ("review_1", None)]


def test_a_review_with_no_available_reviewer_waits_without_holding_the_rest(
    monkeypatch,
):
    import swarm.factory_controls as controls_module

    monkeypatch.setattr(
        controls_module,
        "set_control",
        lambda *_a, **_k: pytest.fail("a waiting review is not a paused task"),
    )
    result = routed_dispatch(monkeypatch, ["implement_fix", "review_1"], reviewer=None)
    assert result.started == [("implement_fix", None)]
    assert [action for action, _detail in result.audits] == ["review_node_waiting"]


def test_a_reviewer_the_policy_does_not_allow_waits(monkeypatch):
    result = routed_dispatch(monkeypatch, ["review_1"], reviewer="fable")
    assert result.started == []
    assert result.audits[0][1]["skipped"][-1] == {
        "model": "fable",
        "reason": "not_allowed",
    }


def test_a_waiting_judgment_review_says_so_in_its_audit(monkeypatch):
    result = routed_dispatch(
        monkeypatch, ["review_1"], reviewer=None, task_class="judgment-analysis"
    )
    assert result.started == []
    action, detail = result.audits[0]
    assert action == "review_node_waiting" and detail["judgment"] is True
    assert detail["task_class"] == "judgment-analysis"


def test_the_window_is_observed_before_any_task_reconciles(monkeypatch):
    """A factory at its limit never admits, so admission cannot be the gate."""
    import swarm.factory_controls as controls_module
    import swarm.factory_intake as intake

    order = []
    policy = {"max_tasks": 1}
    monkeypatch.delenv("FACTORY_MAX_CONCURRENT_TASKS", raising=False)
    monkeypatch.setattr(
        controls_module,
        "status",
        lambda: {
            "state": "enabled",
            "policy": policy,
            "active_tasks": [{"task_id": "t-1", "policy": policy}],
        },
    )
    monkeypatch.setattr(conductor.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(conductor.runtime, "init_dbos", lambda: object())
    monkeypatch.setattr(
        conductor, "observe_reviewer_routing", lambda _p: order.append("routing")
    )
    monkeypatch.setattr(
        conductor,
        "reconcile_task",
        lambda task_id, _p, _dbos: order.append(task_id),
    )
    monkeypatch.setattr(
        intake, "admit_next", lambda *_a, **_k: pytest.fail("at the limit")
    )
    conductor.tick()
    assert order == ["routing", "t-1"]


def test_routing_that_cannot_be_observed_never_stops_the_tick(monkeypatch):
    import swarm.factory_quota_guard as quota_guard

    def explode(_policy):
        raise RuntimeError("broker down")

    monkeypatch.setattr(quota_guard, "observe", explode)
    conductor.observe_reviewer_routing({})


@pytest.mark.parametrize(
    ("body", "closes"),
    [
        ("Closes #77", True),
        ("closes #77", True),
        ("Fixes #77", True),
        ("Resolves owner/repo#77", True),
        # The full URL closes an issue on GitHub just as the short forms do.
        ("Closes https://github.com/owner/repo/issues/77", True),
        ("fixed HTTPS://GITHUB.COM/owner/repo/issues/77", True),
        # Another repository's issue 77 is not this task's issue.
        ("Closes https://github.com/other/repo/issues/77", False),
        # A pull request reference is not an issue reference.
        ("Closes https://github.com/owner/repo/pull/77", False),
        ("See https://github.com/owner/repo/issues/77", False),
        ("Closes https://github.com/owner/repo/issues/770", False),
        ("Refs #77", False),
        ("Closes #770", False),
        ("Closes #7", False),
        ("", False),
        (None, False),
    ],
)
def test_close_keyword_recognises_every_keyword_github_acts_on(body, closes):
    assert conductor.closes_issue(body, "owner/repo", 77) is closes


def test_delivery_refuses_a_body_that_does_not_close_the_issue(monkeypatch):
    """The #3877 defect at its source: a merged PR that left the issue open."""
    task, runs = delivery(monkeypatch, body="Delivers the fix. Refs #77")
    with pytest.raises(conductor.DeliveryRefused) as refusal:
        conductor.verify_delivery(task, 3, runs, issue_number=77)
    assert refusal.value.code == "pr_missing_close_keyword"


def test_delivery_with_the_closing_line_still_verifies(monkeypatch):
    task, runs = delivery(monkeypatch)
    result = conductor.verify_delivery(task, 3, runs, issue_number=77)
    assert result["state"] == "ready_for_review"


def test_a_named_gate_refusal_reaches_the_planner_by_its_own_name(monkeypatch):
    """validation_failed says nothing the planner can act on; the code does."""
    recorded = {}
    monkeypatch.setattr(
        conductor,
        "_decision_processed",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        conductor,
        "_reject_decision",
        lambda task_id, cause, action, code, reason: recorded.update(
            code=code, reason=reason
        ),
    )

    def refuse(*_args, **_kwargs):
        raise conductor.DeliveryRefused("pr_missing_close_keyword", "no closing line")

    monkeypatch.setattr(conductor, "_apply_decision", refuse)
    conductor.apply_decision(
        {"id": "t-1"},
        {},
        {
            "node_key": "conductor_1",
            "attempt": 1,
            "outcome_json": '{"value": {"action": "finish"}}',
        },
        [],
    )
    assert recorded == {"code": "pr_missing_close_keyword", "reason": "no closing line"}


def test_the_delivery_boundary_demands_the_closing_line_on_every_update():
    task = {
        "id": "t-1",
        "repo": "owner/repo",
        "base_branch": "main",
        "issue_number": 77,
    }
    boundary = conductor._boundary(task)
    assert "Closes #77" in boundary
    assert "on every update" in boundary
    # A review node reads the same boundary, so the requirement it checks the
    # body against is the one the implementer was given.
    assert "Closes #77" in conductor._boundary(task, review=True)
    # A task with no receipt issue says nothing about closing keywords rather
    # than inventing a number.
    assert "Closes" not in conductor._boundary({**task, "issue_number": None})


@pytest.fixture
def lost_before_guest_factory(queued_factory, monkeypatch):
    """An invoked attempt whose replica died before any guest was bound (#6025).

    The shape the not-invoked proof deliberately does not cover: the turn was
    dispatched, the executor was cancelled mid-invoke, and recovery recorded
    the ordinary unknown outcome. The session is terminal with no binding and
    the permit is still uncertain, so stop supervision has nothing whose
    cessation it could prove and the lane slot stays held.
    """
    from sqlmodel import Session, SQLModel, select
    from agent_sessions import admission, store
    from agent_sessions.constants import UNKNOWN_INVOCATION
    from agent_sessions.models import (
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from swarm import factory_controls as controls
    from swarm import factory_supervision, node_workflows

    s = queued_factory
    SQLModel.metadata.create_all(s.engine, tables=[AgentResultReceipt.__table__])
    for module in (admission, store, controls):
        monkeypatch.setattr(module, "get_engine", lambda: s.engine)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "false")
    monkeypatch.setenv("FACTORY_LOST_BEFORE_GUEST_SETTLEMENT_ENABLED", "true")

    def unexpected(*_args, **_kwargs):
        pytest.fail("lost-before-guest settlement must not invoke or clean up")

    monkeypatch.setattr(factory_supervision, "_http", unexpected)
    monkeypatch.setattr(node_workflows, "_cleanup_node", unexpected)
    monkeypatch.setattr(node_workflows, "_read_reconciliation_head", unexpected)
    owner = "original-executor"
    assert store.claim_pending_message_for_session_sync(s.sid, owner) == 1
    assert admission.recheck(s.sid, 1, owner, "claude-runtime")
    assert store.release_pending_message_claim_sync(
        s.sid, 1, owner, "executor_cancelled"
    )
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        turn = db.exec(select(AgentTurn)).one()
        permit = db.exec(select(AgentCapacityReservation)).one()
        assert agent.status == "failed"
        assert agent.ember_session_id is None and agent.cli_session_id is None
        assert turn.stop_reason == UNKNOWN_INVOCATION and turn.cost_usd is None
        assert permit.state == "uncertain" and permit.outcome == "executor_cancelled"
        assert db.exec(select(PendingMessage)).first() is None
    s.owner = owner
    s.result = {
        "status": "uncertain",
        "reason": "node workflow CANCELLED",
        "session_id": s.sid,
        "cost_usd": None,
        "cost_basis": "unknown",
    }

    def state(workflow):
        assert workflow == s.run["pin"]["workflow_id"]
        return SimpleNamespace(status="SUCCESS")

    s.dbos = SimpleNamespace(
        get_workflow_status=state,
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: s.result),
        start_workflow=unexpected,
        cancel_workflow=unexpected,
    )

    def native_snapshot():
        with Session(s.engine) as db:
            return {
                model.__tablename__: [
                    row.model_dump() for row in db.exec(select(model))
                ]
                for model in (
                    AgentSession,
                    AgentTurn,
                    PendingMessage,
                    AgentCapacityReservation,
                    AgentResultReceipt,
                )
            }

    s.native_snapshot = native_snapshot
    return s


def _persist_uncertain_lost_before_guest(s):
    import json
    from swarm import factory_controls as controls

    # The incident shape: both ledgers already carry the uncertain outcome with
    # the session bound, which is why #6001 is not what blocks these attempts.
    assert conductor.graph.record_dispatch(
        s.task["id"], s.run["node_key"], 1, s.sid, None
    ).ok
    assert conductor.graph.record_outcome(
        s.task["id"],
        s.run["node_key"],
        1,
        "uncertain",
        None,
        None,
        json.dumps(s.result),
    ).ok
    assert controls.record_start_outcome(
        s.task["id"],
        s.run["dispatch_key"],
        "uncertain",
        "original-reconciler",
        session_id=s.sid,
    )["ok"]
    s.run = conductor.graph.node_runs(s.task["id"])[0]


def _stop_events(s, action="stop_settled"):
    import json
    from sqlmodel import Session, select
    from swarm.factory_models import FactoryAudit

    with Session(s.engine) as db:
        return [
            json.loads(row.detail_json)
            for row in db.exec(
                select(FactoryAudit).where(FactoryAudit.action == action)
            ).all()
        ]


def test_lost_before_guest_proof_reads_the_exact_dispatch_identity(
    lost_before_guest_factory,
):
    from sqlmodel import Session
    from agent_sessions.api import read_lost_before_guest_factory_attempt

    s = lost_before_guest_factory
    with Session(s.engine) as db:
        proof = read_lost_before_guest_factory_attempt(db, s.run["pin"], s.sid)
    assert proof["session_id"] == s.sid
    assert proof["local_session_id"] == f"factory:{s.task['id']}:{s.run['node_key']}:1"
    assert proof["workflow_id"] == s.run["dispatch_key"]
    assert proof["seq"] == 1 and proof["dispatch_count"] == 1
    assert proof["claim_owner"] == s.owner
    assert proof["permit_outcome"] == "executor_cancelled"
    assert proof["invocation_phase"] == "lost_before_guest"
    assert proof["cost_usd"] == 0.0
    # Reading proves nothing and settles nothing.
    assert s.native_snapshot() == s.native_snapshot()


@pytest.mark.parametrize("historical", [False, True])
def test_lost_before_guest_settles_failed_and_refunds_the_reservation(
    lost_before_guest_factory, monkeypatch, historical
):
    import json
    from sqlmodel import Session, select
    from agent_sessions.models import AgentCapacityReservation, AgentSession, AgentTurn
    from swarm import factory_controls as controls

    s = lost_before_guest_factory
    if historical:
        _persist_uncertain_lost_before_guest(s)
    before = controls.task_snapshot(s.task["id"])
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    run = conductor.graph.node_runs(s.task["id"])[0]
    current = controls.task_snapshot(s.task["id"])
    result = json.loads(run["outcome_json"])
    assert run["status"] == current["starts"][0]["status"] == "failed"
    assert run["finished_at"] is not None and run["pin"] == s.run["pin"]
    assert run["cost_usd"] == current["starts"][0]["cost_usd"] == 0.0
    # Nothing ran, so the reservation is refunded rather than consumed. The
    # not-invoked path charges its full ceiling here; this one charges nothing.
    assert run["accounted_cost_usd"] == current["committed_cost_usd"] == 0.0
    assert run["accounting_basis"] == "reported"
    assert current["state"] == "admitted" and current["unresolved_starts"] == 0
    assert current["deadline_at"] == before["deadline_at"]
    assert result["cost_basis"] == "unknown"
    assert result["accounting"] == "unknown_cost"
    assert result["reason"].startswith("lost_before_guest:")
    assert result["lost_before_guest"]["claim_owner"] == s.owner
    # The permit is released, so the lane slot comes back.
    with Session(s.engine) as db:
        permit = db.exec(select(AgentCapacityReservation)).one()
        agent = db.get(AgentSession, s.sid)
        turn = db.exec(select(AgentTurn)).one()
        assert permit.state == "settled" and permit.outcome == "lost_before_guest"
        assert permit.settled_at is not None
        # The failure record itself is immutable, and no guest was invented.
        assert agent.status == "failed" and agent.ember_session_id is None
        assert turn.stop_reason == "invocation_outcome_unknown"
        assert turn.cost_usd is None
    events = _stop_events(s)
    assert len(events) == 1
    assert events[0]["reason"] == "lost_before_guest"
    assert events[0]["session_id"] == s.sid
    assert events[0]["workflow_id"] == s.run["dispatch_key"]
    assert events[0]["cessation_confirmed"] is True
    assert events[0]["intervention_required"] is False
    # A second tick neither re-settles nor writes a second stop event.
    monkeypatch.setattr(
        conductor, "github_get", lambda *_: {"object": {"sha": "a" * 40}}
    )
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert conductor.graph.node_runs(s.task["id"])[0] == run
    assert len(_stop_events(s)) == 1


def test_lost_before_guest_settlement_is_inert_while_the_flag_is_off(
    lost_before_guest_factory, monkeypatch
):
    from sqlmodel import Session, select
    from agent_sessions.models import AgentCapacityReservation
    from swarm import factory_controls as controls

    s = lost_before_guest_factory
    monkeypatch.setenv("FACTORY_LOST_BEFORE_GUEST_SETTLEMENT_ENABLED", "false")
    _persist_uncertain_lost_before_guest(s)
    before = controls.task_snapshot(s.task["id"])
    native = s.native_snapshot()
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert conductor.graph.node_runs(s.task["id"])[0]["status"] == "uncertain"
    current = controls.task_snapshot(s.task["id"])
    assert current["starts"] == before["starts"]
    assert current["state"] == "uncertain" and current["unresolved_starts"] == 1
    assert _stop_events(s) == []
    assert s.native_snapshot() == native
    with Session(s.engine) as db:
        assert db.exec(select(AgentCapacityReservation)).one().state == "uncertain"


@pytest.mark.parametrize(
    "case",
    [
        "bound_guest",
        "cleared_binding",
        "cli_session",
        "cleanup",
        "fence",
        "receipt",
        "workspace_loss",
        "status_warn",
        "turn_completed",
        "turn_priced",
        "turn_artifact",
        "new_turn",
        "new_pending",
        "partial_text",
        "activities",
        "dispatch_missing",
        "dispatch_zero",
        "owner_mismatch",
        "cause_mismatch",
        "permit_settled",
        "permit_outcome",
        "permit_routine_job",
        "model",
    ],
)
def test_lost_before_guest_refuses_conflicting_or_insufficient_proof(
    lost_before_guest_factory, case
):
    import json
    from datetime import datetime, timedelta, timezone
    from sqlmodel import Session, select
    from agent_sessions.api import inspect_lost_before_guest_factory_attempt
    from agent_sessions.models import (
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from swarm import factory_controls as controls

    s = lost_before_guest_factory
    _persist_uncertain_lost_before_guest(s)
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        turn = db.exec(select(AgentTurn)).one()
        permit = db.exec(select(AgentCapacityReservation)).one()
        usage = json.loads(turn.usage_json)
        if case == "bound_guest":
            agent.ember_session_id = "guest-that-was-bound"
        elif case == "cleared_binding":
            # The binding existed and was cleared, which is a different claim.
            agent.prior_ember_lineage_id = "lineage-from-a-real-guest"
        elif case == "cli_session":
            agent.cli_session_id = "cli-transcript"
        elif case == "cleanup":
            agent.guest_cleanup_id = "a" * 32
        elif case == "fence":
            agent.result_receipt_fence_id = "a" * 32
        elif case == "workspace_loss":
            agent.recovery_workspace_loss = True
        elif case == "status_warn":
            agent.status = "warn"
        elif case == "receipt":
            now = datetime.now(timezone.utc)
            db.add(
                AgentResultReceipt(
                    id="a" * 32,
                    token_sha256="b" * 64,
                    session_id=s.sid,
                    local_session_id=agent.local_session_id,
                    seq=1,
                    dispatch_count=1,
                    claim_owner=permit.owner,
                    guest_id="guest-the-receipt-names",
                    request_sha256="c" * 64,
                    created_at=now,
                    accept_until=now + timedelta(hours=13),
                    retain_until=now + timedelta(days=7),
                )
            )
        elif case == "turn_completed":
            turn.stop_reason = "end_turn"
        elif case == "turn_priced":
            turn.cost_usd = 0.25
        elif case == "turn_artifact":
            turn.artifact_blob = b"{}"
        elif case == "new_turn":
            db.add(AgentTurn(session_id=s.sid, seq=2, prompt="new", result_text="new"))
        elif case == "new_pending":
            db.add(PendingMessage(session_id=s.sid, seq=2, message_text="new work"))
        elif case == "partial_text":
            usage["recovery"]["partial_text"] = "the guest streamed this"
        elif case == "activities":
            usage["activities"] = [{"tool": "bash"}]
        elif case == "dispatch_missing":
            usage["recovery"].pop("last_dispatch_at")
        elif case == "dispatch_zero":
            usage["recovery"]["dispatch_count"] = 0
        elif case == "owner_mismatch":
            usage["recovery"]["claim_owner"] = "another-executor"
        elif case == "cause_mismatch":
            usage["recovery"]["cause"] = "lease_expired"
        elif case == "permit_settled":
            permit.state = "settled"
        elif case == "permit_outcome":
            permit.outcome = "guest_cessation_confirmed"
        elif case == "permit_routine_job":
            permit.routine_job_name = "drainer-worker"
        elif case == "model":
            turn.model = "luna"
        turn.usage_json = json.dumps(usage)
        db.add_all([agent, turn, permit])
        db.commit()
    # Each case must fail the proof on its own, not merely leave the tick inert.
    with Session(s.engine) as db:
        proof, refusal = inspect_lost_before_guest_factory_attempt(
            db, s.run["pin"], s.sid
        )
    assert proof is None and refusal
    before = controls.task_snapshot(s.task["id"])
    native = s.native_snapshot()
    runs = conductor.graph.node_runs(s.task["id"])
    conductor.reconcile_task(s.task["id"], s.policy, s.dbos)
    assert conductor.graph.node_runs(s.task["id"]) == runs
    assert controls.task_snapshot(s.task["id"])["starts"] == before["starts"]
    assert controls.task_snapshot(s.task["id"])["state"] == "uncertain"
    assert _stop_events(s) == []
    assert s.native_snapshot() == native


def test_operator_settle_lost_attempt_releases_one_attempt(
    lost_before_guest_factory, monkeypatch
):
    from sqlmodel import Session, select
    from agent_sessions.models import AgentCapacityReservation
    from swarm import factory_controls as controls

    s = lost_before_guest_factory
    # The operator path is deliberately usable while the reconciler flag is off.
    monkeypatch.setenv("FACTORY_LOST_BEFORE_GUEST_SETTLEMENT_ENABLED", "false")
    _persist_uncertain_lost_before_guest(s)
    settled = controls.settle_lost_attempt(
        s.task["id"], s.run["node_key"], 1, "operator"
    )
    assert settled["ok"] and settled["session_id"] == s.sid
    run = conductor.graph.node_runs(s.task["id"])[0]
    current = controls.task_snapshot(s.task["id"])
    assert run["status"] == "failed" and run["cost_usd"] == 0.0
    assert current["unresolved_starts"] == 0 and current["committed_cost_usd"] == 0.0
    with Session(s.engine) as db:
        permit = db.exec(select(AgentCapacityReservation)).one()
        assert permit.state == "settled" and permit.outcome == "lost_before_guest"
    events = _stop_events(s)
    assert len(events) == 1 and events[0]["reason"] == "lost_before_guest"
    # The receipt is untouched, so every other attempt on the task survives.
    assert current["state"] == "admitted"
    with pytest.raises(ValueError, match="attempt_not_active"):
        controls.settle_lost_attempt(s.task["id"], s.run["node_key"], 1, "operator")


@pytest.mark.parametrize(
    "case,expected",
    [
        ("bound_guest", "prior_binding_evidence"),
        ("cleared_binding", "prior_binding_evidence"),
        ("permit_settled", "permit_not_uncertain"),
        ("turn_completed", "turn_not_lost_before_guest"),
        ("status_warn", "session_not_terminal"),
    ],
)
def test_operator_settle_lost_attempt_names_the_failed_condition(
    lost_before_guest_factory, case, expected
):
    from sqlmodel import Session, select
    from agent_sessions.models import AgentCapacityReservation, AgentSession, AgentTurn
    from swarm import factory_controls as controls

    s = lost_before_guest_factory
    _persist_uncertain_lost_before_guest(s)
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        turn = db.exec(select(AgentTurn)).one()
        permit = db.exec(select(AgentCapacityReservation)).one()
        if case == "bound_guest":
            agent.ember_session_id = "guest-that-was-bound"
        elif case == "cleared_binding":
            agent.prior_cli_session_id = "cli-from-a-real-guest"
        elif case == "permit_settled":
            permit.state = "settled"
        elif case == "turn_completed":
            turn.stop_reason = "end_turn"
        elif case == "status_warn":
            agent.status = "warn"
        db.add_all([agent, turn, permit])
        db.commit()
    native = s.native_snapshot()
    runs = conductor.graph.node_runs(s.task["id"])
    with pytest.raises(ValueError, match=expected):
        controls.settle_lost_attempt(s.task["id"], s.run["node_key"], 1, "operator")
    assert conductor.graph.node_runs(s.task["id"]) == runs
    assert controls.task_snapshot(s.task["id"])["state"] == "uncertain"
    assert _stop_events(s) == []
    assert s.native_snapshot() == native


def test_lost_before_guest_proof_refuses_or_raises_on_ownership(
    lost_before_guest_factory,
):
    from sqlmodel import Session
    from agent_sessions.api import inspect_lost_before_guest_factory_attempt

    s = lost_before_guest_factory
    pin = s.run["pin"]
    with Session(s.engine) as db:
        # No session was ever created under that identity: nothing to settle,
        # and an absent owner is never read as proof that nothing ran.
        missing = {**pin, "attempt": 2}
        assert inspect_lost_before_guest_factory_attempt(db, missing, None) == (
            None,
            "missing_factory_owner",
        )
        # The identity exists but the pin describes different work. Adopting it
        # would settle another attempt's reservation, so this raises.
        for conflicting in (
            {**pin, "model": "luna"},
            {**pin, "workflow_id": "factory-node:another:conductor_1:1"},
            {**pin, "repo": "owner/other"},
        ):
            with pytest.raises(ValueError, match="ownership conflict"):
                inspect_lost_before_guest_factory_attempt(db, conflicting, s.sid)
        # A session id that is not this attempt's owner is refused outright.
        with pytest.raises(ValueError, match="ownership conflict"):
            inspect_lost_before_guest_factory_attempt(db, pin, s.sid + 1)
        for invalid in (0, -1, "1", True):
            with pytest.raises(ValueError, match="invalid factory session identity"):
                inspect_lost_before_guest_factory_attempt(db, pin, invalid)
    assert s.native_snapshot() == s.native_snapshot()
