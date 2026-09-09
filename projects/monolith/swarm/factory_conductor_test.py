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


def delivery(monkeypatch, *, review_head=None, draft=False, check_state="success"):
    monkeypatch.setattr(conductor, "_budget_evidence", lambda _task: {})
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
            "session_id": 11,
            "head_sha": head,
            "pin": {"model": "opus"},
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
    for value in (
        review["artifact"]["summary"],
        context["decision_feedback"][0]["reason"],
    ):
        assert value.endswith(" [text omitted]")
        assert len(json.dumps(value)) <= conductor.PLANNER_TEXT_CHARS


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
        and projection["max_turns_per_task"] == policy["max_turns_per_task"]
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
    assert current["turns_used"] == original["turns_used"] == 1
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
def uncertain_factory(queued_factory, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import copy
    import json
    from sqlmodel import Session
    from agent_sessions import admission, store
    from agent_sessions.models import AgentSession
    from swarm import factory_controls as controls
    from swarm import factory_supervision as supervisor

    s = queued_factory
    for module in (controls, admission, store):
        monkeypatch.setattr(module, "get_engine", lambda: s.engine)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "true")
    owner = "original-factory-executor"
    assert store.claim_pending_message_for_session_sync(s.sid, owner) == 1
    assert admission.recheck(s.sid, 1, owner)
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.sid)
        agent.ember_session_id = "s-exact-factory"
        agent.ember_lineage_id = "lineage-preserved"
        agent.cli_session_id = "cli-preserved"
        pending = store.get_pending_message(db, s.sid, 1)
        pending.last_dispatch_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        pending.partial_text = "Unpublished partial implementation"
        db.add_all([agent, pending])
        db.commit()
    store.finish_unknown_pending_sync(s.sid, 1, owner, 1, "executor_cancelled")
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
        "invoke_started_at": 100,
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
        "invoke_started_at": 100,
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
                "requested_at_unix_ms": 150,
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
            "completed_at_unix_ms": 200,
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
    with Session(s.engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == "stop_settled")
                ).all()
            )
            == 1
        )


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
    assert receipt["turns_used"] == 1
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
