from types import SimpleNamespace

import pytest

from swarm import factory_conductor as conductor
from swarm.turn_artifact import schema_errors


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


def delivery(monkeypatch, *, review_head=None, draft=False, check_state="success"):
    head = "a" * 40
    task = {
        "id": "t-1",
        "repo": "owner/repo",
        "base_branch": "main",
        "conductor_model": "opus",
    }
    pr = {
        "state": "open",
        "draft": draft,
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
            "outcome_json": json.dumps({"value": {"pr_number": 3, "head_sha": head}}),
        },
        {
            "id": 2,
            "node_key": "review_fix",
            "status": "succeeded",
            "session_id": 11,
            "head_sha": head,
            "pin": {"model": "opus"},
            "outcome_json": json.dumps(
                {
                    "value": {
                        "pr_number": 3,
                        "head_sha": review_head or head,
                        "verdict": "approve",
                    }
                }
            ),
        },
    ]
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
        "state": "ready_for_review",
    }


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


def test_stopped_factory_never_polls_or_admits(monkeypatch):
    import swarm.factory_controls as controls

    monkeypatch.setattr(
        controls, "status", lambda: {"state": "stopped", "active_tasks": []}
    )
    monkeypatch.setattr(conductor.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(conductor.runtime, "init_dbos", lambda: object())
    monkeypatch.setattr(
        conductor, "ingest_eligible", lambda _: pytest.fail("stopped admission")
    )
    conductor.tick()


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
    yield engine
    engine.dispose()


def feedback_task(*, max_turns=8):
    from swarm import factory_controls as controls
    from swarm.factory_intake import admit_next, receive_issue

    policy = {
        "repo": "owner/repo",
        "issue_numbers": [7],
        "generation": 0,
        "max_tasks": 1,
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
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        "owner/repo",
        7,
        "Fix issue",
        "Untrusted issue text",
        "https://github.com/owner/repo/issues/7",
        "poller",
    )
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    return conductor._task(admitted["task_id"]), policy


def complete_feedback_node(task, policy, node_key, value, *, head=None):
    import json
    from swarm import factory_controls as controls

    model = "luna" if node_key.startswith("implement_") else "opus"
    assert conductor._add(
        task,
        policy,
        node_key,
        "bounded work",
        [],
        model,
        f"test:{node_key}",
        "test fixture",
        review=node_key.startswith("review_"),
    ).ok
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
        "status": "succeeded",
        "session_id": session_id,
        "cost_usd": 0.25,
        "head_sha": head,
        "value": value,
        "artifact": {"status": "ok", "value": value},
        "cleanup": {"status": "completed"},
    }
    assert conductor.graph.record_dispatch(task["id"], node_key, 1, session_id, None).ok
    assert conductor.graph.record_outcome(
        task["id"], node_key, 1, "succeeded", 0.25, head, json.dumps(result)
    ).ok
    assert controls.record_start_outcome(
        task["id"],
        workflow,
        "succeeded",
        "worker",
        cost_usd=0.25,
        session_id=session_id,
    )["ok"]
    return next(
        r for r in conductor.graph.node_runs(task["id"]) if r["node_key"] == node_key
    )


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
            {"status": "complete", "pr_number": 3, "head_sha": head},
            head=head,
        )
        complete_feedback_node(
            task,
            policy,
            "review_fix",
            {"verdict": "approve", "pr_number": 3, "head_sha": head},
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
    run = complete_feedback_node(
        task,
        policy,
        "conductor_1",
        {
            "action": "add_node",
            "node_key": "conductor_forbidden",
            "role": "implement",
            "prompt": "fix",
            "deps": [],
            "reason": "needed",
        },
    )
    conductor.apply_decision(task, policy, run, conductor.graph.node_runs(task["id"]))
    conductor.reconcile_task(task["id"], policy, object())
    monkeypatch.setattr(
        conductor, "github_get", lambda *_args: {"object": {"sha": "a" * 40}}
    )
    conductor.reconcile_task(task["id"], policy, object())
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["turns_used"] == 1 and snapshot["committed_cost_usd"] == 0.25
    assert snapshot["task_paused"] and snapshot["policy"]["max_turns_per_task"] == 1
    assert len(conductor.graph.node_runs(task["id"])) == 1


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
            {"value": value, "artifact": value, "raw_detail": nested}
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
    assert len(prompt) < 16_000
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
    assert (
        "[text omitted]"
        in context["delivery_evidence"]["latest_review"]["artifact"]["summary"]
    )
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
    for value in (
        review["artifact"]["summary"],
        context["decision_feedback"][0]["reason"],
    ):
        assert value.endswith(" [text omitted]")
        assert len(json.dumps(value)) <= conductor.PLANNER_TEXT_CHARS
