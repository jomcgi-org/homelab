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


def audit_rows(db):
    with Session(db) as session:
        return list(session.exec(select(FactoryAudit).order_by(FactoryAudit.id)).all())


def audit_actions(db):
    return [row.action for row in audit_rows(db)]


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
            "recommendation": "split",
        },
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
        {
            "outcome": "needs-human",
            "comment_url": comment["html_url"],
            "question": "Choose one target",
            "recommendation": "defer",
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
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    assert controls.task_snapshot(task["id"])["evidence"]["state"] == (
        "refine_needs_human"
    )
    downgrade = [
        row for row in audit_rows(db) if row.action == "refine_close_downgraded"
    ]
    assert json.loads(downgrade[0].detail_json)["reason"] == "protected_issue"


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
        },
    )
    refine.reconcile(task, policy, graph.load_graph(task["id"]), [run], 1)
    downgrade = [
        row for row in audit_rows(db) if row.action == "refine_close_downgraded"
    ]
    assert json.loads(downgrade[0].detail_json)["reason"] == "close_cap"


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


def test_the_prompt_offers_closing_only_when_it_is_available():
    task = {"id": "t-1", "repo": "owner/repo"}
    receipt = {"issue_number": 7, "url": "u", "title": "t", "body": "b"}
    open_lane = refine.refine_prompt(task, receipt, closing=True)
    assert "`reject` when the issue clearly should not be done" in open_lane
    assert "not_planned" in open_lane
    assert "critical" in open_lane
    shut = refine.refine_prompt(task, receipt, closing=False)
    assert "Closing is switched off for this run" in shut
    assert "recommend: close" in shut
