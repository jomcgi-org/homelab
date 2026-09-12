"""Single-node refine admission and server-side settlement regressions."""

from __future__ import annotations

from datetime import datetime, timedelta
import json

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from swarm import factory_conductor as conductor
from swarm import factory_controls as controls
from swarm import factory_landing as landing
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
    # Refine is advisory work, and the advisory lane is opt-in with a ceiling
    # that has to hold both lanes at once.
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    yield engine
    engine.dispose()


def make_task(task_class="refine"):
    policy = {
        "repo": "owner/repo",
        "issue_numbers": [7],
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 1},
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


def settle_attempt(task, status, value=None, *, cost_usd=0.1, outcome_evidence=None):
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
    outcome = {"value": value or {}, **(outcome_evidence or {})}
    assert graph.record_outcome(
        task["id"],
        refine.NODE_KEY,
        attempt,
        status,
        cost_usd,
        None,
        json.dumps(outcome),
    ).ok
    assert controls.record_start_outcome(
        task["id"],
        key,
        status,
        "worker",
        cost_usd=cost_usd,
        # Derived from the outcome exactly as the conductor derives it, so the
        # start ledger in these tests books what the real one books.
        accounting_basis=graph.settled_zero_basis(outcome)
        if status in graph.TERMINAL_RUN_STATUSES
        else None,
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


def audit_rows(db):
    with Session(db) as session:
        return list(session.exec(select(FactoryAudit).order_by(FactoryAudit.id)).all())


def audit_actions(db):
    return [row.action for row in audit_rows(db)]


def options(first="split"):
    """A valid option list whose head matches ``first``'s recommendation."""
    head = {
        "deliver": {
            "key": "deliver",
            "label": "Deliver the /invoke path first",
            "effect": "agent-ready",
            "detail": {"scope": "Only the /invoke path."},
        },
        "close": {
            "key": "close",
            "label": "Close as superseded by #5656",
            "effect": "close",
            "detail": {"reason": "not_planned", "comment": "Superseded."},
        },
        "split": {
            "key": "split",
            "label": "Split the console out of the API",
            "effect": "split",
            "detail": {"children": [{"title": "Console", "body": "The console."}]},
        },
        "defer": {
            "key": "defer",
            "label": "Defer until the hub migration lands",
            "effect": "defer",
            "detail": {"comment": "After the hub migration."},
        },
    }[first]
    return [head, {"key": "hold", "label": "Leave it open", "effect": "hold"}]


def human_artifact(comment_url, recommendation="split", **extra):
    artifact = {
        "outcome": "needs-human",
        "comment_url": comment_url,
        "question": "Which compatibility target is required?",
        "recommendation": recommendation,
        "summary": "The scope is two features wearing one issue number.",
        "options": options(recommendation),
    }
    artifact.update(extra)
    return artifact


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


def test_verified_defer_settles_without_escalation_or_notification(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-thought")
    monkeypatch.setattr(
        refine,
        "_notify_once",
        lambda *_args, **_kwargs: pytest.fail("defer notified a human"),
    )
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "defer",
            "comment_url": comment["html_url"],
            "evidence": "The author must choose the storage model.",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "succeeded"
    assert snapshot["evidence"]["state"] == "refine_deferred"
    assert snapshot["escalation"] is None
    assert "refine_needs_human_notified" not in audit_actions(db)
    settled = [row for row in audit_rows(db) if row.action == "refine_settled"]
    assert json.loads(settled[0].detail_json)["outcome"] == "defer"


def test_defer_without_evidence_is_refused(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-thought")
    run = settle_attempt(
        task,
        "succeeded",
        {"outcome": "defer", "comment_url": comment["html_url"]},
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["evidence"]["state"] == "refine_unverified"
    assert "defer cites no evidence" in snapshot["evidence"]["reason"]


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
        human_artifact(comment["html_url"]),
    )
    nodes = graph.load_graph(task["id"])
    refine.reconcile(task, policy, nodes, [run], 1)
    refine.reconcile(task, policy, nodes, [run], 1)
    assert len(sent) == 1 and sent[0][1] == "warn"
    assert "recommend: split" in sent[0][0]
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
        human_artifact(comment["html_url"], "split", question="Choose one target"),
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
    assert "attempt limit" in snapshot["evidence"]["reason"]
    assert "refine_failed" in audit_actions(db)


def test_unpriced_failed_attempt_that_exhausts_cost_finishes_on_next_reconcile(db):
    task, policy = make_task()
    node = add_refine_node(task, policy)
    run = settle_attempt(task, "failed", cost_usd=None)
    assert run["accounted_cost_usd"] == node["max_cost_usd"]
    assert len(graph.node_runs(task["id"])) < node["max_attempts"]

    refine.reconcile(
        task,
        policy,
        [node],
        [run],
        graph.current_version(task["id"]),
    )

    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "failed"
    assert snapshot["evidence"]["state"] == "refine_failed"
    assert "cost limit" in snapshot["evidence"]["reason"]
    assert "1 of 2 attempts" in snapshot["evidence"]["reason"]


def test_null_cost_not_invoked_attempt_stays_ready_for_retry(db):
    task, policy = make_task()
    node = add_refine_node(task, policy)
    run = settle_attempt(
        task,
        "failed",
        cost_usd=None,
        outcome_evidence={
            "not_invoked": {"invocation_phase": "not_invoked", "session_id": 101}
        },
    )
    assert run["accounted_cost_usd"] == 0.0
    assert conductor._ready_nodes([node], [run]) == [node]

    refine.reconcile(
        task,
        policy,
        [node],
        [run],
        graph.current_version(task["id"]),
    )
    assert controls.task_snapshot(task["id"])["state"] == "admitted"


def test_a_capacity_denied_attempt_does_not_spend_the_lane_a_retry(db):
    """EmberVM having no slot must not read as the refine node failing twice."""
    task, policy = make_task()
    node = add_refine_node(task, policy)
    denial = {
        "not_invoked": {"invocation_phase": "not_invoked", "session_id": 101},
        "capacity_denied": True,
    }
    runs = [
        settle_attempt(task, "failed", cost_usd=None, outcome_evidence=denial),
        settle_attempt(task, "failed", cost_usd=None, outcome_evidence=denial),
    ]
    assert len(runs) >= node["max_attempts"]
    assert graph.attempts_spent(runs, refine.NODE_KEY) == 0
    assert conductor._ready_nodes([node], runs) == [node]

    refine.reconcile(task, policy, [node], runs, graph.current_version(task["id"]))
    assert controls.task_snapshot(task["id"])["state"] == "admitted"
    assert "refine_failed" not in audit_actions(db)


CAPACITY_DENIAL = {
    "not_invoked": {"invocation_phase": "not_invoked", "session_id": 101},
    "capacity_denied": True,
}


def test_two_capacity_denials_still_leave_the_lane_a_start(db):
    """The turn ledger has to excuse what the attempt ledger excuses."""
    task, policy = make_task()
    node = add_refine_node(task, policy)
    for _ in range(node["max_attempts"]):
        settle_attempt(task, "failed", cost_usd=None, outcome_evidence=CAPACITY_DENIAL)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["turns_used"] == 0
    assert snapshot["committed_cost_usd"] == 0
    assert [start["accounting_basis"] for start in snapshot["starts"]] == [
        "capacity_denied",
        "capacity_denied",
    ]

    third = controls.authorize_start(
        task["id"],
        f"factory-node:{task['id']}:{refine.NODE_KEY}:3",
        "worker",
        model=policy["worker_model"],
        max_cost_usd=node["max_cost_usd"],
    )
    assert third["ok"] and not third["replayed"]


def test_a_start_that_never_reached_a_model_commits_no_budget(db):
    """The graph books the attempt at zero; the start ledger has to agree."""
    task, policy = make_task()
    node = add_refine_node(task, policy)
    run = settle_attempt(
        task,
        "failed",
        cost_usd=None,
        outcome_evidence={"not_invoked": {"invocation_phase": "not_invoked"}},
    )
    snapshot = controls.task_snapshot(task["id"])
    assert run["accounted_cost_usd"] == 0.0
    assert snapshot["committed_cost_usd"] == 0
    assert snapshot["starts"][0]["accounting_basis"] == "no_model_post"
    # It reached no model, but it is still an attempt the node made, so it
    # spends a turn where a refused slot does not.
    assert snapshot["turns_used"] == 1

    retry = controls.authorize_start(
        task["id"],
        f"factory-node:{task['id']}:{refine.NODE_KEY}:2",
        "worker",
        model=policy["worker_model"],
        max_cost_usd=node["max_cost_usd"],
    )
    assert retry["ok"]
    # Nothing of the task budget was spent on an attempt that never ran, so
    # the retry's own ceiling is all that is committed against it.
    assert (
        controls.task_snapshot(task["id"])["committed_cost_usd"] == node["max_cost_usd"]
    )


def test_uncertain_refine_attempt_is_not_finished(db):
    task, policy = make_task()
    node = add_refine_node(task, policy)
    run = settle_attempt(task, "uncertain", cost_usd=None)
    refine.reconcile(
        task,
        policy,
        [node],
        [run],
        graph.current_version(task["id"]),
    )
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "uncertain"
    assert snapshot["evidence"] is None


def test_the_brief_is_read_from_the_comments_written_since_admission(db, monkeypatch):
    """A busy issue must not hide the brief behind a first page of history."""
    task, policy = make_task()
    add_refine_node(task, policy)
    admitted = controls.task_snapshot(task["id"])["admitted_at"]
    brief = {
        "body": "## Agent brief\n### Outcome\nReady",
        "created_at": (
            datetime.fromisoformat(admitted) + timedelta(seconds=1)
        ).isoformat(),
        "html_url": "https://github.com/owner/repo/issues/7#issuecomment-9",
        "user": {"login": "factory-bot"},
    }
    pages = [
        [dict(brief, body="chatter", html_url=f"c{n}") for n in range(100)],
        [brief],
    ]
    suffixes = []

    def github_list(_repo, suffix):
        suffixes.append(suffix)
        return pages.pop(0) if pages else []

    monkeypatch.setattr(
        refine, "github_get", lambda *_args: {"labels": [{"name": "agent-ready"}]}
    )
    monkeypatch.setattr(refine, "github_list", github_list)
    run = settle_attempt(
        task,
        "succeeded",
        {"outcome": "agent-ready", "comment_url": brief["html_url"]},
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert all("since=" in suffix for suffix in suffixes)
    assert len(suffixes) == 2
    assert controls.task_snapshot(task["id"])["state"] == "succeeded"


def test_a_recorded_mismatch_settles_without_re_reading_github(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    monkeypatch.setattr(
        refine, "github_get", lambda *_args: {"labels": [{"name": "needs-human"}]}
    )
    monkeypatch.setattr(refine, "github_list", lambda *_args: [])
    run = settle_attempt(
        task, "succeeded", {"outcome": "agent-ready", "comment_url": "u"}
    )
    nodes = graph.load_graph(task["id"])
    refine.reconcile(task, policy, nodes, [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_unverified"
    )
    # A second pass must neither re-audit the verdict nor spend another read.
    monkeypatch.setattr(
        refine, "github_get", lambda *_args: pytest.fail("re-read after a verdict")
    )
    monkeypatch.setattr(
        refine, "github_list", lambda *_args: pytest.fail("re-read after a verdict")
    )
    refine.reconcile(task, policy, nodes, [run], 1)
    assert audit_actions(db).count("refine_mismatch") == 1


def closing_task(monkeypatch, *, close_enabled=True, max_closes_per_day=3, **overrides):
    """A refine task whose pinned policy permits closing."""
    task, policy = make_task()
    policy = dict(
        policy,
        intake={
            "enabled": True,
            "refine_enabled": True,
            "close_enabled": close_enabled,
            "max_closes_per_day": max_closes_per_day,
            **overrides,
        },
    )
    return task, policy


def closed_github(monkeypatch, task, label, *, state="closed", **issue_overrides):
    admitted = controls.task_snapshot(task["id"])["admitted_at"]
    comment = {
        "body": "## Agent brief\n### Outcome\nNo\n### Why not\nSuperseded",
        "created_at": (
            datetime.fromisoformat(admitted) + timedelta(seconds=1)
        ).isoformat(),
        "html_url": "https://github.com/owner/repo/issues/7#issuecomment-2",
        "user": {"login": "factory-bot"},
    }
    issue = {"labels": [{"name": label}], "state": state}
    issue.update(issue_overrides)
    monkeypatch.setattr(refine, "github_get", lambda *_args: issue)
    monkeypatch.setattr(refine, "github_list", lambda *_args: [comment])
    return comment


def test_a_verified_reject_closes_and_audits_its_evidence(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    comment = closed_github(monkeypatch, task, "wontfix")
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "contradicts platform/ARCHITECTURE.md section 4",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "succeeded"
    assert snapshot["evidence"]["state"] == "refine_rejected"
    closes = [row for row in audit_rows(db) if row.action == "intake_closed"]
    assert len(closes) == 1
    assert "ARCHITECTURE.md" in json.loads(closes[0].detail_json)["evidence"]


@pytest.mark.parametrize(
    ("artifact_extra", "reason"),
    [
        (
            {"supersedes": 8},
            "reject carries invalid superseded issue numbers",
        ),
        (
            {"in_favour_of": "20"},
            "reject carries an invalid issue in favour",
        ),
        ({"supersedes": [8]}, "supersedes names no issue in favour"),
        (
            {"supersedes": [8, 20], "in_favour_of": 20},
            "supersedes includes the issue in favour",
        ),
        (
            {"supersedes": [7, 8], "in_favour_of": 20},
            "supersedes includes the briefed issue",
        ),
        (
            {"supersedes": [8], "in_favour_of": 7},
            "issue in favour is the briefed issue",
        ),
    ],
)
def test_reject_refuses_invalid_supersede_relationships_before_primary_checks(
    db, monkeypatch, artifact_extra, reason
):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    reads = []
    writes = []
    monkeypatch.setattr(
        refine, "github_get", lambda *_args: reads.append(_args) or pytest.fail("read")
    )
    monkeypatch.setattr(refine, "github_list", lambda *_args: pytest.fail("read"))
    monkeypatch.setattr(
        landing,
        "github_write",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": "https://github.com/owner/repo/issues/7#issuecomment-2",
            "evidence": "KG: duplicate work (verified)",
            **artifact_extra,
        },
    )

    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)

    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["evidence"]["state"] == "refine_unverified"
    assert reason in snapshot["evidence"]["reason"]
    assert "refine_mismatch" in audit_actions(db)
    assert reads == []
    assert writes == []


@pytest.mark.parametrize("favoured", ["closed", "pull_request", "missing"])
def test_reject_refuses_a_favoured_target_that_is_not_an_open_issue(
    db, monkeypatch, favoured
):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    reads = []

    def get(_repo, suffix):
        reads.append(suffix)
        if favoured == "missing":
            request = httpx.Request("GET", "https://api.github.test/issues/20")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("missing", request=request, response=response)
        issue = {"state": "closed" if favoured == "closed" else "open"}
        if favoured == "pull_request":
            issue["pull_request"] = {"url": "https://api.github.test/pulls/20"}
        return issue

    monkeypatch.setattr(refine, "github_get", get)
    monkeypatch.setattr(refine, "github_list", lambda *_args: pytest.fail("read"))
    monkeypatch.setattr(
        landing, "github_write", lambda *_args, **_kwargs: pytest.fail("write")
    )
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": "https://github.com/owner/repo/issues/7#issuecomment-2",
            "evidence": "KG: duplicate work (verified)",
            "supersedes": [8],
            "in_favour_of": 20,
        },
    )

    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)

    snapshot = controls.task_snapshot(task["id"])
    assert "issue in favour is not an open issue" in snapshot["evidence"]["reason"]
    assert "refine_mismatch" in audit_actions(db)
    assert reads == ["issues/20"]


def test_reject_retries_a_transient_favoured_issue_read(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    request = httpx.Request("GET", "https://api.github.test/issues/20")
    response = httpx.Response(503, request=request)

    def get(_repo, suffix):
        assert suffix == "issues/20"
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    monkeypatch.setattr(refine, "github_get", get)
    monkeypatch.setattr(refine, "github_list", lambda *_args: pytest.fail("read"))
    monkeypatch.setattr(
        landing, "github_write", lambda *_args, **_kwargs: pytest.fail("write")
    )
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": "https://github.com/owner/repo/issues/7#issuecomment-2",
            "evidence": "KG: duplicate work (verified)",
            "supersedes": [8],
            "in_favour_of": 20,
        },
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)

    assert exc_info.value.response.status_code == 503
    assert "refine_mismatch" not in audit_actions(db)


def test_reject_closes_eligible_siblings_and_audits_skips(db, monkeypatch):
    task, policy = closing_task(monkeypatch, max_closes_per_day=2)
    add_refine_node(task, policy)
    admitted = controls.task_snapshot(task["id"])["admitted_at"]
    comment = {
        "body": "## Agent brief\n### Outcome\nNo\n### Why not\nDuplicate",
        "created_at": (
            datetime.fromisoformat(admitted) + timedelta(seconds=1)
        ).isoformat(),
        "html_url": "https://github.com/owner/repo/issues/7#issuecomment-2",
        "user": {"login": "factory-bot"},
    }
    issues = {
        7: {"state": "closed", "labels": [{"name": "wontfix"}]},
        8: {"state": "open", "labels": [], "assignees": []},
        9: {"state": "open", "labels": [{"name": "critical"}]},
        10: {"state": "open", "labels": [], "milestone": {"number": 1}},
        11: {"state": "open", "labels": []},
        12: {"state": "open", "labels": []},
        13: {"state": "open", "labels": [], "assignees": [{"login": "joe"}]},
        14: {"state": "open", "labels": []},
        15: {"state": "closed", "labels": [], "pull_request": {}},
        20: {"state": "open", "labels": []},
    }

    def get(_repo, suffix):
        return issues[int(suffix.rsplit("/", 1)[1])]

    def listing(_repo, suffix):
        return [comment] if suffix.startswith("issues/7/comments?") else []

    writes = []

    def write(_repo, suffix, payload, *, method="POST"):
        writes.append((method, suffix, payload))
        return {}

    monkeypatch.setattr(refine, "github_get", get)
    monkeypatch.setattr(refine, "github_list", listing)
    monkeypatch.setattr(landing, "github_write", write)
    with Session(db) as session:
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=11,
                generation=9,
                title="Sibling being decided",
                body="",
                url="https://github.com/owner/repo/issues/11",
                actor="factory:intake",
                task_class="refine",
                state=controls.ESCALATED,
            )
        )
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=14,
                generation=9,
                title="Sibling queued for a brief",
                body="",
                url="https://github.com/owner/repo/issues/14",
                actor="factory:intake",
                task_class="refine",
                state="queued",
            )
        )
        session.commit()

    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "KG: duplicate work (verified)",
            "in_favour_of": 20,
            "supersedes": [8, 9, 10, 11, 13, 14, 15, 12],
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)

    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_rejected"
    )
    assert any(
        suffix == "issues/8/comments" and "factory-supersede:" in payload["body"]
        for _method, suffix, payload in writes
    )
    assert ("POST", "issues/8/labels", {"labels": ["wontfix"]}) in writes
    assert (
        "PATCH",
        "issues/8",
        {"state": "closed", "state_reason": "not_planned"},
    ) in writes
    closes = [
        json.loads(row.detail_json)
        for row in audit_rows(db)
        if row.action == "intake_closed"
    ]
    assert any(
        detail.get("issue_number") == 8 and detail.get("superseded_by") == 20
        for detail in closes
    )
    skipped = {
        detail["sibling"]: detail["reason"]
        for detail in (
            json.loads(row.detail_json)
            for row in audit_rows(db)
            if row.action == "refine_supersede_skipped"
        )
    }
    assert skipped == {
        9: "protected_label",
        10: "milestone",
        11: "active_receipt",
        12: "close_cap",
        13: "assigned",
        14: "active_receipt",
        15: "pull_request",
    }


def test_superseded_retry_closes_and_comments_once_without_a_skip(db, monkeypatch):
    task, policy = closing_task(monkeypatch, max_closes_per_day=2)
    add_refine_node(task, policy)
    admitted = controls.task_snapshot(task["id"])["admitted_at"]
    brief = {
        "body": "## Agent brief\n### Outcome\nNo\n### Why not\nDuplicate",
        "created_at": (
            datetime.fromisoformat(admitted) + timedelta(seconds=1)
        ).isoformat(),
        "html_url": "https://github.com/owner/repo/issues/7#issuecomment-2",
        "user": {"login": "factory-bot"},
    }
    issues = {
        7: {"state": "closed", "labels": [{"name": "wontfix"}]},
        8: {"state": "open", "labels": []},
        20: {"state": "open", "labels": []},
    }
    sibling_comments = []
    writes = []

    def get(_repo, suffix):
        return issues[int(suffix.rsplit("/", 1)[1])]

    def listing(_repo, suffix):
        if suffix.startswith("issues/7/comments?"):
            return [brief]
        if suffix.startswith("issues/8/comments?"):
            return list(sibling_comments)
        return []

    def write(_repo, suffix, payload, *, method="POST"):
        writes.append((method, suffix, payload))
        if suffix == "issues/8/comments":
            sibling_comments.append({"body": payload["body"]})
        if suffix == "issues/8" and method == "PATCH":
            issues[8]["state"] = "closed"
        return {}

    real_finish = refine.finish_task
    real_audit_once = refine._audit_once
    finish_calls = 0
    dropped_sibling_audit = False

    def defer_once(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            return {"ok": False, "reason": "start_unresolved"}
        return real_finish(*args, **kwargs)

    def miss_sibling_audit_once(task_id, cause, action, detail):
        nonlocal dropped_sibling_audit
        if (
            not dropped_sibling_audit
            and action == "intake_closed"
            and detail.get("issue_number") == 8
        ):
            dropped_sibling_audit = True
            return None
        return real_audit_once(task_id, cause, action, detail)

    monkeypatch.setattr(refine, "github_get", get)
    monkeypatch.setattr(refine, "github_list", listing)
    monkeypatch.setattr(landing, "github_write", write)
    monkeypatch.setattr(refine, "finish_task", defer_once)
    monkeypatch.setattr(refine, "_audit_once", miss_sibling_audit_once)
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": brief["html_url"],
            "evidence": "KG: duplicate work (verified)",
            "in_favour_of": 20,
            "supersedes": [8],
        },
    )
    nodes = graph.load_graph(task["id"])

    refine.reconcile(task, policy, nodes, [run], 1)
    refine.reconcile(task, policy, nodes, [run], 1)

    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_rejected"
    )
    assert sum(suffix == "issues/8/comments" for _, suffix, _ in writes) == 1
    assert (
        sum(method == "PATCH" and suffix == "issues/8" for method, suffix, _ in writes)
        == 1
    )
    sibling_closes = [
        row
        for row in audit_rows(db)
        if row.action == "intake_closed"
        and json.loads(row.detail_json).get("issue_number") == 8
    ]
    assert len(sibling_closes) == 1
    assert "refine_supersede_skipped" not in audit_actions(db)
    assert "refine_close_downgraded" not in audit_actions(db)


def test_superseded_retry_finishes_a_close_after_the_label_write_failed(
    db, monkeypatch
):
    task, policy = closing_task(monkeypatch, max_closes_per_day=2)
    add_refine_node(task, policy)
    admitted = controls.task_snapshot(task["id"])["admitted_at"]
    brief = {
        "body": "## Agent brief\n### Outcome\nNo\n### Why not\nDuplicate",
        "created_at": (
            datetime.fromisoformat(admitted) + timedelta(seconds=1)
        ).isoformat(),
        "html_url": "https://github.com/owner/repo/issues/7#issuecomment-2",
        "user": {"login": "factory-bot"},
    }
    issues = {
        7: {"state": "closed", "labels": [{"name": "wontfix"}]},
        8: {"state": "open", "labels": []},
        20: {"state": "open", "labels": []},
    }
    sibling_comments = []
    writes = []
    reads = []
    label_attempts = 0

    def get(_repo, suffix):
        reads.append(suffix)
        return issues[int(suffix.rsplit("/", 1)[1])]

    def listing(_repo, suffix):
        if suffix.startswith("issues/7/comments?"):
            return [brief]
        if suffix.startswith("issues/8/comments?"):
            return list(sibling_comments)
        return []

    def write(_repo, suffix, payload, *, method="POST"):
        nonlocal label_attempts
        writes.append((method, suffix, payload))
        if suffix == "issues/8/comments":
            sibling_comments.append({"body": payload["body"]})
        if suffix == "issues/8/labels":
            label_attempts += 1
            if label_attempts == 1:
                raise RuntimeError("GitHub unavailable")
            issues[8]["labels"] = [{"name": "wontfix"}]
        if suffix == "issues/8" and method == "PATCH":
            issues[8]["state"] = "closed"
        return {}

    real_finish = refine.finish_task
    finish_calls = 0

    def defer_once(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            return {"ok": False, "reason": "start_unresolved"}
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(refine, "github_get", get)
    monkeypatch.setattr(refine, "github_list", listing)
    monkeypatch.setattr(landing, "github_write", write)
    monkeypatch.setattr(refine, "finish_task", defer_once)
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": brief["html_url"],
            "evidence": "KG: duplicate work (verified)",
            "in_favour_of": 20,
            "supersedes": [8],
        },
    )
    nodes = graph.load_graph(task["id"])

    refine.reconcile(task, policy, nodes, [run], 1)
    assert issues[8]["state"] == "open"
    refine.reconcile(task, policy, nodes, [run], 1)

    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_rejected"
    )
    assert issues[8]["state"] == "closed"
    assert issues[8]["labels"] == [{"name": "wontfix"}]
    assert reads.count("issues/8") == 3
    assert sum(suffix == "issues/8/comments" for _, suffix, _ in writes) == 1
    assert label_attempts == 2
    assert (
        sum(method == "PATCH" and suffix == "issues/8" for method, suffix, _ in writes)
        == 1
    )
    sibling_closes = [
        row
        for row in audit_rows(db)
        if row.action == "intake_closed"
        and json.loads(row.detail_json).get("issue_number") == 8
    ]
    assert len(sibling_closes) == 1


def test_a_failing_sibling_is_skipped_without_failing_settlement(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    comment = closed_github(monkeypatch, task, "wontfix")
    monkeypatch.setattr(
        refine,
        "github_get",
        lambda _repo, suffix: (
            {"state": "closed", "labels": [{"name": "wontfix"}]}
            if suffix == "issues/7"
            else {"state": "open", "labels": []}
        ),
    )

    def write(_repo, suffix, _payload, *, method="POST"):
        if suffix == "issues/8/labels":
            raise RuntimeError("GitHub unavailable")
        return {}

    monkeypatch.setattr(landing, "github_write", write)
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "git: abc123",
            "in_favour_of": 20,
            "supersedes": [8],
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["state"] == "succeeded"
    skipped = [
        json.loads(row.detail_json)
        for row in audit_rows(db)
        if row.action == "refine_supersede_skipped"
    ]
    assert skipped[0]["reason"] == "RuntimeError"


def test_a_verified_stale_closes_under_its_own_label(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    comment = closed_github(monkeypatch, task, "stale")
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "stale",
            "comment_url": comment["html_url"],
            "evidence": "projects/foo/flag.py no longer exists",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == "refine_stale"


def test_a_close_that_left_the_issue_open_is_refused(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    comment = closed_github(monkeypatch, task, "wontfix", state="open")
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "duplicate of #12",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_unverified"
    )
    assert "refine_mismatch" in audit_actions(db)


def test_a_close_verdict_is_downgraded_while_closing_is_off(db, monkeypatch):
    """The deploy stays inert: the flag defaults off and the lane escalates."""
    from agent import notify as notify_module

    task, policy = closing_task(monkeypatch, close_enabled=False)
    add_refine_node(task, policy)
    comment = closed_github(monkeypatch, task, "needs-human", state="open")
    sent = []

    async def notify(text, *, level):
        sent.append((text, level))

    monkeypatch.setattr(notify_module, "notify", notify)
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "duplicate of #12",
            "recommendation": "close",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "succeeded"
    assert snapshot["evidence"]["state"] == "refine_needs_human"
    assert "refine_close_downgraded" in audit_actions(db)
    assert "intake_closed" not in audit_actions(db)
    assert len(sent) == 1 and "recommend: close" in sent[0][0]


def test_a_protected_issue_is_never_closed(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    comment = closed_github(
        monkeypatch,
        task,
        "needs-human",
        state="open",
        labels=[{"name": "needs-human"}, {"name": "critical"}],
    )
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "low value",
            "recommendation": "close",
            "in_favour_of": 20,
            "supersedes": [8],
        },
    )
    primary = {
        "state": "open",
        "labels": [{"name": "needs-human"}, {"name": "critical"}],
    }
    monkeypatch.setattr(
        refine,
        "github_get",
        lambda _repo, suffix: (
            {"state": "open", "labels": []} if suffix == "issues/20" else primary
        ),
    )
    writes = []
    monkeypatch.setattr(
        landing,
        "github_write",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_needs_human"
    )
    downgrade = [
        row for row in audit_rows(db) if row.action == "refine_close_downgraded"
    ]
    assert json.loads(downgrade[0].detail_json)["reason"] == "protected_issue"
    assert writes == []
    assert "refine_supersede_skipped" not in audit_actions(db)


def test_a_milestone_protects_an_issue_from_closing(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    comment = closed_github(
        monkeypatch, task, "needs-human", state="open", milestone={"number": 3}
    )
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "stale",
            "comment_url": comment["html_url"],
            "evidence": "gone",
            "recommendation": "close",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_needs_human"
    )


def test_the_daily_close_cap_downgrades_the_next_close(db, monkeypatch):
    task, policy = closing_task(monkeypatch, max_closes_per_day=1)
    add_refine_node(task, policy)
    with Session(db) as session:
        session.add(
            FactoryAudit(
                actor="factory:refine", action="intake_closed", detail_json="{}"
            )
        )
        session.commit()
    comment = closed_github(monkeypatch, task, "needs-human", state="open")
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "duplicate",
            "recommendation": "close",
            "in_favour_of": 20,
            "supersedes": [8],
        },
    )
    primary = {"state": "open", "labels": [{"name": "needs-human"}]}
    monkeypatch.setattr(
        refine,
        "github_get",
        lambda _repo, suffix: (
            {"state": "open", "labels": []} if suffix == "issues/20" else primary
        ),
    )
    writes = []
    monkeypatch.setattr(
        landing,
        "github_write",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    downgrade = [
        row for row in audit_rows(db) if row.action == "refine_close_downgraded"
    ]
    assert json.loads(downgrade[0].detail_json)["reason"] == "close_cap"
    assert writes == []
    assert "refine_supersede_skipped" not in audit_actions(db)


def test_a_close_verdict_without_evidence_is_refused(db, monkeypatch):
    task, policy = closing_task(monkeypatch)
    add_refine_node(task, policy)
    comment = closed_github(monkeypatch, task, "wontfix")
    run = settle_attempt(
        task, "succeeded", {"outcome": "reject", "comment_url": comment["html_url"]}
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_unverified"
    )


def test_needs_human_without_a_recommendation_is_refused(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "needs-human",
            "comment_url": comment["html_url"],
            "question": "Which target?",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_unverified"
    )


def test_needs_human_may_not_recommend_defer(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    run = settle_attempt(
        task,
        "succeeded",
        human_artifact(comment["html_url"], "defer"),
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["evidence"]["state"] == "refine_unverified"
    assert "no recommendation" in snapshot["evidence"]["reason"]


def test_close_recommendation_accepts_a_supersede_option():
    artifact = {
        "options": [
            {
                "key": "supersede",
                "label": "Close the duplicates in favour of #20",
                "effect": "supersede",
                "detail": {"closes": [7, 8], "in_favour_of": 20},
            },
            {"key": "hold", "label": "Leave them open", "effect": "hold"},
        ]
    }
    assert refine._verify_options(artifact, "close") is None


def test_the_prompt_offers_closing_only_when_it_is_available():
    task = {"id": "t-1", "repo": "owner/repo"}
    receipt = {"issue_number": 7, "url": "u", "title": "t", "body": "b"}
    open_lane = refine.refine_prompt(task, receipt, closing=True)
    assert "`reject` when the issue clearly should not be done" in open_lane
    assert "not_planned" in open_lane
    assert "critical" in open_lane
    assert "search_knowledge" in open_lane
    assert "report_knowledge" in open_lane
    assert "decision a person can make in about a minute" in open_lane
    assert "`defer` when the issue is worth doing" in open_lane
    shut = refine.refine_prompt(task, receipt, closing=False)
    assert "Closing is switched off for this run" in shut
    assert "recommend: close" in shut
    assert "supersedes" not in shut
    assert "### Supersedes" not in shut


def test_needs_human_stores_the_options_on_the_receipt(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    run = settle_attempt(task, "succeeded", human_artifact(comment["html_url"]))
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    escalation = controls.task_snapshot(task["id"])["escalation"]
    assert escalation["recommendation"] == "split"
    assert escalation["summary"].startswith("The scope is two features")
    assert [option["key"] for option in escalation["options"]] == ["split", "hold"]
    assert escalation["resolved"] is None
    settled = [row for row in audit_rows(db) if row.action == "refine_settled"]
    assert json.loads(settled[0].detail_json)["options"] == [
        {"key": "split", "effect": "split"},
        {"key": "hold", "effect": "hold"},
    ]


def test_the_notification_links_the_escalations_view(db, monkeypatch):
    from agent import notify as notify_module

    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    sent = []

    async def notify(text, *, level):
        sent.append(text)

    monkeypatch.setattr(notify_module, "notify", notify)
    run = settle_attempt(task, "succeeded", human_artifact(comment["html_url"]))
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert refine.ESCALATIONS_URL in sent[0]


def test_needs_human_without_options_is_refused(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    artifact = human_artifact(comment["html_url"])
    del artifact["options"]
    run = settle_attempt(task, "succeeded", artifact)
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_unverified"
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda opts: opts[:1], "two to four"),
        (lambda opts: opts * 3, "two to four"),
        (lambda opts: [opts[1], opts[0]], "not what"),
        (lambda opts: [dict(opts[0]), dict(opts[0])], "repeats an option key"),
        (lambda opts: [{**opts[0], "key": "NOT A KEY"}, opts[1]], "no usable key"),
        (lambda opts: [{**opts[0], "label": ""}, opts[1]], "no usable label"),
        (lambda opts: [{**opts[0], "effect": "delete"}, opts[1]], "no known effect"),
        (
            lambda opts: [
                {"key": "c", "label": "Close it", "effect": "close", "detail": {}},
                opts[1],
            ],
            "closes with no reason",
        ),
        (
            lambda opts: [
                {"key": "d", "label": "Wait", "effect": "defer", "detail": {}},
                opts[1],
            ],
            "no wait condition",
        ),
        (
            lambda opts: [{**opts[0], "detail": {"children": []}}, opts[1]],
            "splits into no children",
        ),
    ],
)
def test_the_server_refuses_a_malformed_option_list(db, monkeypatch, mutate, reason):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    artifact = human_artifact(comment["html_url"])
    artifact["options"] = mutate(artifact["options"])
    # The close head above is still what `recommend:` names, so that case
    # fails on the one thing it is testing rather than on order.
    if artifact["options"] and artifact["options"][0]["effect"] == "close":
        artifact["recommendation"] = "close"
    run = settle_attempt(task, "succeeded", artifact)
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    evidence = controls.task_snapshot(task["id"])["evidence"]
    assert evidence["state"] == "refine_unverified"
    assert reason in evidence["reason"]


def test_a_downgraded_close_still_carries_decidable_options(db, monkeypatch):
    task, policy = closing_task(monkeypatch, close_enabled=False)
    add_refine_node(task, policy)
    comment = closed_github(monkeypatch, task, "needs-human", state="open")
    run = settle_attempt(
        task,
        "succeeded",
        {
            "outcome": "reject",
            "comment_url": comment["html_url"],
            "evidence": "duplicate of #12",
            "recommendation": "close",
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    escalation = controls.task_snapshot(task["id"])["escalation"]
    assert escalation["downgraded"] is True
    assert [option["effect"] for option in escalation["options"]] == ["close", "hold"]
    assert escalation["options"][0]["detail"]["reason"] == "not_planned"


def test_the_prompt_asks_for_concrete_option_labels():
    task = {"id": "t-1", "repo": "owner/repo"}
    receipt = {"issue_number": 7, "url": "u", "title": "t", "body": "b"}
    text = refine.refine_prompt(task, receipt, closing=True)
    assert "Close as superseded by #5656" in text
    assert "numbered list" in text
    assert "Never a bare verb" in text


def test_an_operator_question_reaches_the_next_prompt():
    task = {"id": "t-1", "repo": "owner/repo"}
    receipt = {
        "issue_number": 7,
        "url": "u",
        "title": "t",
        "body": "b",
        "escalation": {"chat": [{"note": "Does this cover the friends tier too?"}]},
    }
    text = refine.refine_prompt(task, receipt, closing=True)
    assert "Does this cover the friends tier too?" in text
    assert "asked for more before deciding" in text


def test_a_re_brief_replaces_the_options_the_operator_sent_back(db, monkeypatch):
    """The second brief's options must be the ones the page offers.

    The receipt is re-briefed in place, so keeping the first document left the
    page showing options written before the operator's question was answered,
    and pressing 1 applied the stale first one.
    """
    from swarm import factory_decisions as decisions

    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    run = settle_attempt(task, "succeeded", human_artifact(comment["html_url"]))
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    first = controls.task_snapshot(task["id"])["escalation"]
    assert [option["key"] for option in first["options"]] == ["split", "hold"]

    receipt_id = next(row.id for row in Session(db).exec(select(FactoryReceipt)).all())
    monkeypatch.setattr(
        "swarm.factory_conductor.github_list", lambda *_args: [], raising=False
    )
    monkeypatch.setattr(
        "swarm.factory_landing.github_write",
        lambda *_args, **_kwargs: {"id": 1},
        raising=False,
    )
    assert decisions.request_chat(receipt_id, "Which tier?", "joe@example.test")["ok"]

    # The lane briefs it again and settles onto the same receipt.
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    second_task = conductor._task(admitted["task_id"])
    add_refine_node(second_task, admitted["policy"])
    second_comment = verified_github(monkeypatch, second_task, "needs-human")
    second_run = settle_attempt(
        second_task,
        "succeeded",
        human_artifact(
            second_comment["html_url"],
            "deliver",
            question="The friends tier is out of scope.",
            summary="Answered: only the private tier.",
        ),
    )
    refine.reconcile(
        second_task,
        admitted["policy"],
        graph.load_graph(second_task["id"]),
        [second_run],
        1,
    )
    second = controls.task_snapshot(second_task["id"])["escalation"]
    assert [option["key"] for option in second["options"]] == ["deliver", "hold"]
    assert second["question"] == "The friends tier is out of scope."
    assert second["recommendation"] == "deliver"
    # The chat history is the record of what was asked, so it survives.
    assert [entry["note"] for entry in second["chat"]] == ["Which tier?"]


def test_a_dismissed_escalation_is_replaced_by_the_next_brief(db, monkeypatch):
    """A dismiss cleared the card. It did not decide anything.

    It wrote nothing to GitHub, so an operator who cleared a card and then
    asked for another brief has to get the card back carrying the answer.
    Every other resolution still survives, which is the test below.
    """
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    run = settle_attempt(task, "succeeded", human_artifact(comment["html_url"]))
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        document = json.loads(row.escalation_json)
        document["resolved"] = {
            "option_key": "escape:dismiss",
            "effect": "escape-dismiss",
            "actor": "joe",
        }
        row.escalation_json = json.dumps(document)
        session.add(row)
        session.commit()

    refine._record_escalation(
        task["id"],
        human_artifact(comment["html_url"], "deliver"),
        comment["html_url"],
        downgraded=False,
    )

    fresh = controls.task_snapshot(task["id"])["escalation"]
    assert fresh["resolved"] is None
    assert fresh["recommendation"] == "deliver"


def test_a_resolved_escalation_is_never_overwritten(db, monkeypatch):
    task, policy = make_task()
    add_refine_node(task, policy)
    comment = verified_github(monkeypatch, task, "needs-human")
    run = settle_attempt(task, "succeeded", human_artifact(comment["html_url"]))
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).one()
        document = json.loads(row.escalation_json)
        document["resolved"] = {"option_key": "hold", "actor": "joe"}
        row.escalation_json = json.dumps(document)
        session.add(row)
        session.commit()
    # Settlement is reached again on every tick until it takes; a rewrite here
    # would discard a decision already recorded against it.
    refine._record_escalation(
        task["id"],
        human_artifact(comment["html_url"], "deliver"),
        comment["html_url"],
        downgraded=False,
    )
    assert controls.task_snapshot(task["id"])["escalation"]["resolved"] == {
        "option_key": "hold",
        "actor": "joe",
    }
