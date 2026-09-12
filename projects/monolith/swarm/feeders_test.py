"""Focused producer, classification, routing and advisory boundary tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import swarm.factory_controls as controls
import swarm.factory_refine as refine
from swarm import feeders, graph
from swarm.factory_intake import admit_next
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
        f"sqlite:///{tmp_path / 'feeders.db'}",
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                SwarmPlanVersion,
                SwarmPlanNode,
                SwarmConductorCall,
                SwarmNodeRun,
                FactoryControl,
                FactoryReceipt,
                FactoryStart,
                FactoryAudit,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(graph, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def policy():
    return {
        "repo": "owner/repo",
        "issue_numbers": [99],
        "generation": 2,
        "max_tasks": {"delivery": 2, "advisory": 2},
        "max_turns_per_task": 5,
        "task_budget_usd": 8.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["luna", "opus"],
        "conductor_model": "opus",
        "reviewer_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "max_attempts": 2,
        "task_timeout_seconds": 3600,
        "intake": {"max_per_day": 5},
    }


def enable(policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]


def candidate(task_class="mechanical-refactor", source_key="event:one", number=7):
    return feeders.Candidate(
        feeder="test",
        source_key=source_key,
        issue_number=number,
        title="bounded producer task",
        body="event evidence",
        task_class=task_class,
    )


def test_all_five_inputs_are_classified_with_stable_identities():
    commit = {
        "sha": "a" * 40,
        "files": [
            {"filename": "docs/code-health/duplication.md", "patch": "+duplicate"},
            {"filename": "projects/embervm/noded/server.go"},
        ],
    }
    register = feeders.register_diff_candidates(commit, 11)
    ci = feeders.ci_failure_candidates(
        {"number": 12, "head": {"sha": "b" * 40}},
        {"check_runs": [{"id": 5, "name": "BuildBuddy", "conclusion": "failure"}]},
    )
    renovate = feeders.renovate_candidate(
        {
            "number": 13,
            "title": "chore(deps): update foo",
            "user": {"login": "renovate[bot]"},
            "head": {"sha": "c" * 40},
        }
    )
    stpa = feeders.stpa_candidate(commit, 11, "projects/embervm/STPA.md", "d" * 40)
    plan = feeders.plan_candidate(
        commit,
        11,
        "SPEC.md",
        "e" * 40,
        datetime.now(timezone.utc) - timedelta(days=31),
    )
    values = [register[0], ci[0], renovate, stpa, plan]
    assert [value.feeder for value in values] == [
        "register-diff",
        "ci-failure",
        "renovate",
        "stpa-staleness",
        "plan-staleness",
    ]
    assert [value.task_class for value in values] == [
        "mechanical-refactor",
        "advisory-diagnosis",
        "advisory-triage",
        "judgment-analysis",
        "judgment-analysis",
    ]
    assert len({value.source_key for value in values}) == 5


def test_discovery_wires_all_five_bounded_github_inputs(policy, monkeypatch):
    merge_sha = "a" * 40
    failed_sha = "b" * 40
    renovate_sha = "c" * 40

    def github_list(_repo, suffix):
        if suffix.startswith("pulls?"):
            return [
                {"number": 12, "title": "feature", "head": {"sha": failed_sha}},
                {
                    "number": 13,
                    "title": "chore(deps): update foo",
                    "user": {"login": "renovate[bot]"},
                    "head": {"sha": renovate_sha},
                },
            ]
        if suffix.startswith("commits?sha=main&per_page="):
            return [{"sha": merge_sha}]
        if suffix == f"commits/{merge_sha}/pulls?per_page=1":
            return [{"number": 11}]
        if "path=SPEC.md" in suffix:
            return [
                {
                    "sha": "e" * 40,
                    "commit": {
                        "committer": {
                            "date": (
                                datetime.now(timezone.utc) - timedelta(days=31)
                            ).isoformat()
                        }
                    },
                }
            ]
        raise AssertionError(suffix)

    def github_get(_repo, suffix):
        if suffix == f"commits/{failed_sha}/check-runs?per_page=20":
            return {
                "check_runs": [{"id": 5, "name": "BuildBuddy", "conclusion": "failure"}]
            }
        if suffix == f"commits/{renovate_sha}/check-runs?per_page=20":
            return {"check_runs": []}
        if suffix == f"commits/{merge_sha}":
            return {
                "sha": merge_sha,
                "files": [
                    {
                        "filename": "docs/code-health/duplication.md",
                        "patch": "+duplicate",
                    },
                    {"filename": "projects/embervm/noded/server.go"},
                ],
            }
        if suffix == "contents/projects/embervm/STPA.md?ref=main":
            return {"sha": "d" * 40}
        raise AssertionError(suffix)

    monkeypatch.setattr(feeders, "_github", lambda: (github_get, github_list))
    discovered = feeders.discover(policy)
    assert {item.feeder for item in discovered} == {
        "register-diff",
        "ci-failure",
        "renovate",
        "stpa-staleness",
        "plan-staleness",
    }


def test_disabled_switches_write_nothing_and_replay_is_idempotent(
    db, policy, monkeypatch
):
    denied = feeders.enqueue(candidate(), policy)
    assert denied == {"ok": False, "created": False, "reason": "disabled"}
    with Session(db) as session:
        assert session.exec(select(FactoryReceipt)).all() == []

    called = False

    def discover(_policy):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(feeders, "discover", discover)
    monkeypatch.delenv("SWARM_FEEDERS_ENABLED", raising=False)
    assert feeders.feeder_tick(policy) == []
    assert not called

    enable(policy)
    first = feeders.enqueue(candidate(), policy)
    replay = feeders.enqueue(candidate(), policy)
    assert first["created"] and not replay["created"]
    assert first["receipt"]["id"] == replay["receipt"]["id"]
    with Session(db) as session:
        assert len(session.exec(select(FactoryReceipt)).all()) == 1


def test_enabled_tick_invokes_the_real_producer_path_once(db, policy, monkeypatch):
    enable(policy)
    monkeypatch.setenv("SWARM_FEEDERS_ENABLED", "true")
    calls = []

    def discover(received_policy):
        calls.append(received_policy["repo"])
        return [candidate()]

    monkeypatch.setattr(feeders, "discover", discover)
    first = feeders.feeder_tick(policy)
    replay = feeders.feeder_tick(policy)
    assert calls == ["owner/repo"]
    assert len(first) == 1 and first[0]["created"]
    assert replay == []
    with Session(db) as session:
        assert len(session.exec(select(FactoryReceipt)).all()) == 1
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == "feeders_swept")
                ).all()
            )
            == 1
        )


def test_admission_persists_class_and_floor_and_dispatch_cannot_downgrade(
    db, policy, monkeypatch
):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    enable(policy)
    assert feeders.enqueue(candidate("judgment-analysis"), policy)["created"]
    admitted = admit_next("reconciler", lanes=("delivery",))
    assert admitted["ok"]
    with Session(db) as session:
        task = session.get(SwarmTask, admitted["task_id"])
        assert (task.task_class, task.capability_tier) == (
            "judgment-analysis",
            "opus",
        )
    added = graph.add_node(
        admitted["task_id"],
        node_key="implement_one",
        kind="work",
        prompt="do judgment work",
        model="opus",
        deps=[],
        max_cost_usd=1.0,
        side_effects=True,
        max_attempts=1,
        turn_timeout_seconds=60,
        author_kind="conductor",
        author="opus",
        cause_kind="test",
        cause_ref="one",
        stated_reason="test floor",
        expected_version=0,
    )
    assert added.ok
    downgraded = graph.admit_dispatch(
        admitted["task_id"], "implement_one", model="luna"
    )
    assert not downgraded.ok
    assert downgraded.refusal_code == "below_capability_floor"
    accepted = graph.admit_dispatch(admitted["task_id"], "implement_one", model="opus")
    assert accepted.ok and accepted.pin["model"] == "opus"


def test_advisory_path_is_comment_only_and_rejects_any_factory_pr(
    db, policy, monkeypatch
):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    enable(policy)
    assert feeders.enqueue(candidate("advisory-diagnosis", number=8), policy)["created"]
    admitted = admit_next("reconciler", lanes=("advisory",))
    task_id = admitted["task_id"]
    task = {"id": task_id, "repo": "owner/repo"}
    snapshot = controls.task_snapshot(task_id)
    prompt = refine.advisory_prompt(task, snapshot, "advisory-diagnosis")
    assert "Do not edit tracked files" in prompt
    assert "open a pull request" in prompt
    assert refine.ADVISORY_SCHEMA["properties"]["pr_number"] == {"type": "null"}

    comment = "https://github.com/owner/repo/pull/8#issuecomment-1"
    run = {
        "outcome_json": json.dumps(
            {"value": {"comment_url": comment, "pr_number": None}}
        )
    }
    monkeypatch.setattr(
        refine,
        "_comments_since",
        lambda *_args: [{"html_url": comment, "user": {"login": "agent"}}],
    )
    monkeypatch.setattr(refine, "github_list", lambda *_args: [{"number": 44}])
    refine._settle_advisory(task, run, "advisory-diagnosis")
    assert controls.task_snapshot(task_id)["state"] == "failed"


def test_advisory_verified_comment_settles_without_a_pr(db, policy, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    enable(policy)
    assert feeders.enqueue(candidate("advisory-triage", number=9), policy)["created"]
    admitted = admit_next("reconciler", lanes=("advisory",))
    task_id = admitted["task_id"]
    comment = "https://github.com/owner/repo/pull/9#issuecomment-2"
    monkeypatch.setattr(
        refine,
        "_comments_since",
        lambda *_args: [{"html_url": comment, "user": {"login": "agent"}}],
    )
    monkeypatch.setattr(refine, "github_list", lambda *_args: [])
    refine._settle_advisory(
        {"id": task_id, "repo": "owner/repo"},
        {
            "outcome_json": json.dumps(
                {"value": {"comment_url": comment, "pr_number": None}}
            )
        },
        "advisory-triage",
    )
    assert controls.task_snapshot(task_id)["state"] == "succeeded"
