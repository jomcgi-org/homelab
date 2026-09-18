"""Hermetic verdict-window, recovery, and admission-routing tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_feedback as feedback
from factory.orchestration.factory_intake import admit_next, receive_issue
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
)
from factory.orchestration.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    engine = create_engine(
        f"sqlite:///{tmp_path / 'feedback.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    models = (
        SwarmTask,
        SwarmPlanVersion,
        SwarmPlanNode,
        SwarmNodeRun,
        SwarmConductorCall,
        FactoryClassTier,
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        FactoryAudit,
        FactoryReviewVerdict,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in models])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    for module in (controls, conductor, conductor.graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setenv("FACTORY_BACKGROUND_RESERVE", "0")
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    yield engine
    engine.dispose()


BASE_TIME = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _sample(
    engine,
    number: int,
    verdict: str,
    *,
    task_class: str = "bug-fix",
    sample_kind: str = "delivery",
    reviewed_at: datetime | None = None,
    recipe: bool = True,
) -> None:
    task_id = f"t-sample-{task_class}-{sample_kind}-{number}"
    with Session(engine) as session:
        session.add(
            SwarmTask(
                id=task_id,
                task_text="sample",
                repo="owner/repo",
                base_branch="main",
                conductor_model="opus",
            )
        )
        session.flush()
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=number,
                generation=0,
                title="sample",
                body="",
                url=f"https://github.com/owner/repo/issues/{number}",
                actor="test",
                task_class=task_class,
                routing_tier=sample_kind,
                state="succeeded",
                task_id=task_id,
            )
        )
        recipe_run_id = None
        if recipe:
            recipe_run = SwarmNodeRun(
                task_id=task_id,
                node_key=(
                    feedback.ADVISORY_NODE_KEY
                    if sample_kind == "advisory"
                    else "conductor_1"
                ),
                attempt=1,
                status="succeeded",
                session_id=number * 10,
                outcome_json="{}",
            )
            session.add(recipe_run)
            session.flush()
            recipe_run_id = recipe_run.id
        review_run = SwarmNodeRun(
            task_id=task_id,
            node_key=(
                feedback.REVIEW_NODE_KEY
                if sample_kind == "advisory"
                else "review_initial"
            ),
            attempt=1,
            status="succeeded",
            session_id=number * 10 + 1,
            outcome_json="{}",
        )
        session.add(review_run)
        session.flush()
        session.add(
            FactoryReviewVerdict(
                task_id=task_id,
                review_run_id=review_run.id,
                recipe_run_id=recipe_run_id,
                task_class=task_class,
                sample_kind=sample_kind,
                verdict=verdict,
                summary=f"{task_class} {sample_kind} {verdict} {number}",
                reviewed_at=reviewed_at or BASE_TIME + timedelta(minutes=number),
            )
        )
        session.commit()


def _policy(issue_number: int = 999) -> dict:
    return {
        "repo": "owner/repo",
        "issue_numbers": [issue_number],
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 1},
        "max_turns_per_task": 18,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "max_attempts": 2,
        "task_timeout_seconds": 3600,
    }


def _configure(policy: dict) -> None:
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]


def test_window_is_latest_twenty_and_classes_are_isolated(db):
    for number in range(1, 22):
        _sample(db, number, "changes_requested" if number == 1 else "approve")
    for number in range(101, 121):
        _sample(db, number, "blocked", task_class="docs")

    bugs = feedback.feedback_for_class("bug-fix")
    docs = feedback.feedback_for_class("docs")
    assert bugs["sample_count"] == feedback.WINDOW_SIZE
    assert bugs["approval_count"] == feedback.WINDOW_SIZE
    assert bugs["tier"] == "delivery"
    assert docs["sample_count"] == feedback.WINDOW_SIZE
    assert docs["approval_rate"] == 0
    assert docs["rejection_rate"] == 1
    assert docs["tier"] == "advisory"


def test_minimum_samples_no_data_and_fixed_advisory_default(db):
    assert feedback.route_for_class("bug-fix")["approval_rate"] is None
    assert feedback.route_for_class("bug-fix")["tier"] == "delivery"
    for number in range(1, 20):
        _sample(db, number, "blocked")
    insufficient = feedback.route_for_class("bug-fix")
    assert insufficient["sample_count"] == 19
    assert insufficient["tier"] == "delivery"
    fixed = feedback.route_for_class("advisory-diagnosis")
    assert fixed["sample_count"] == 0
    assert fixed["tier"] == "advisory"
    assert fixed["decision"] == "class_floor"


def test_threshold_boundaries_hold_and_recovery_requires_above_floor(db):
    for number in range(1, 21):
        _sample(db, number, "approve" if number <= 12 else "changes_requested")
    delivery = feedback.route_for_class("bug-fix")
    assert delivery["approval_rate"] == 0.60
    assert delivery["decision"] == "at_floor_hold"
    assert delivery["tier"] == "delivery"

    transition = BASE_TIME + timedelta(days=1)
    with Session(db) as session:
        session.add(
            FactoryClassTier(
                task_class="docs",
                routing_tier="advisory",
                transitioned_at=transition,
                updated_at=transition,
            )
        )
        session.commit()
    for number in range(101, 121):
        _sample(
            db,
            number,
            "approve" if 102 <= number <= 113 else "changes_requested",
            task_class="docs",
            sample_kind="advisory",
            reviewed_at=transition + timedelta(minutes=number),
        )
    advisory = feedback.route_for_class("docs")
    assert advisory["approval_rate"] == 0.60
    assert advisory["tier"] == "advisory"
    _sample(
        db,
        121,
        "approve",
        task_class="docs",
        sample_kind="advisory",
        reviewed_at=transition + timedelta(minutes=121),
    )
    assert feedback.route_for_class("docs")["approval_rate"] == 0.65
    assert feedback.route_for_class("docs")["tier"] == "delivery"


def test_recovery_transition_resets_delivery_window_for_stability(db):
    transition = BASE_TIME
    with Session(db) as session:
        session.add(
            FactoryClassTier(
                task_class="bug-fix",
                routing_tier="advisory",
                transitioned_at=transition,
                updated_at=transition,
            )
        )
        session.commit()
    for number in range(1, 21):
        _sample(
            db,
            number,
            "approve" if number <= 13 else "changes_requested",
            sample_kind="advisory",
            reviewed_at=transition + timedelta(minutes=number),
        )
    recovered = feedback.route_for_class("bug-fix")
    assert recovered["tier"] == "delivery"
    with Session(db) as session:
        feedback.store_class_route("bug-fix", recovered, session=session)
        session.commit()
    fresh = feedback.route_for_class("bug-fix")
    assert fresh["tier"] == "delivery"
    assert fresh["sample_count"] == 0
    assert fresh["decision"] == "insufficient_samples"


def _first_pass_runs(engine, *, malformed: bool = False):
    task_id = "t-first-pass-malformed" if malformed else "t-first-pass"
    with Session(engine) as session:
        session.add(
            SwarmTask(
                id=task_id,
                task_text="task",
                repo="owner/repo",
                base_branch="main",
                conductor_model="opus",
            )
        )
        session.flush()
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=501 if malformed else 500,
                title="task",
                body="",
                url="https://github.com/owner/repo/issues/500",
                actor="test",
                task_class="bug-fix",
                routing_tier="delivery",
                state="admitted",
                task_id=task_id,
            )
        )
        values = (
            ("conductor_1", "succeeded", 10, {}, False),
            ("review_initial", "failed", None, {}, True),
            (
                "review_initial",
                "succeeded" if malformed else "failed",
                12,
                {} if malformed else {"summary": "review failed"},
                False,
            ),
            (
                "review_initial",
                "succeeded",
                13,
                {"verdict": "approve", "summary": "later retry approved"},
                False,
            ),
        )
        runs = []
        attempts = {}
        for node_key, status, session_id, artifact, denied in values:
            attempts[node_key] = attempts.get(node_key, 0) + 1
            row = SwarmNodeRun(
                task_id=task_id,
                node_key=node_key,
                attempt=attempts[node_key],
                status=status,
                session_id=session_id,
                outcome_json=json.dumps({"value": artifact}),
            )
            session.add(row)
            session.flush()
            runs.append(
                {
                    "id": row.id,
                    "node_key": node_key,
                    "attempt": row.attempt,
                    "status": status,
                    "session_id": session_id,
                    "capacity_denied": denied,
                    "outcome_json": row.outcome_json,
                    "created_at": row.created_at,
                    "finished_at": row.created_at,
                }
            )
        session.commit()
    return task_id, runs


@pytest.mark.parametrize(
    ("malformed", "expected"),
    [(False, "blocked"), (True, "unparseable")],
)
def test_first_completed_review_is_immutable_across_retries(db, malformed, expected):
    task_id, runs = _first_pass_runs(db, malformed=malformed)
    first = feedback.record_first_pass(task_id, runs)
    replay = feedback.record_first_pass(task_id, list(reversed(runs)))
    assert first == replay
    assert first["verdict"] == expected
    assert first["recipe_run_id"] == runs[0]["id"]
    with Session(db) as session:
        assert len(session.exec(select(FactoryReviewVerdict)).all()) == 1
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.action == "first_pass_verdict_recorded"
                    )
                ).all()
            )
            == 1
        )


def test_admission_demotes_at_actual_routing_boundary(db):
    for number in range(1, 21):
        _sample(db, number, "approve" if number <= 11 else "changes_requested")
    _configure(_policy())
    receive_issue(
        "owner/repo",
        999,
        "new bug",
        "body",
        "https://github.com/owner/repo/issues/999",
        "operator",
    )

    admitted = admit_next("scheduler")
    assert admitted["ok"]
    assert admitted["lane"] == "advisory"
    assert admitted["receipt"]["task_class"] == "bug-fix"
    assert admitted["receipt"]["routing_tier"] == "advisory"
    separated = feedback.feedback_for_class("bug-fix")
    assert separated["sample_count"] == 0
    assert separated["recovery_window"]["sample_count"] == 0
    assert separated["delivery_window"]["sample_count"] == 20
    assert separated["delivery_window"]["approval_rate"] == 0.55
    with Session(db) as session:
        transition = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "class_tier_demoted")
        ).one()
    detail = json.loads(transition.detail_json)
    assert detail["sample_count"] == 20
    assert detail["approval_rate"] == 0.55


def test_planner_recipe_receives_attributed_class_rejections(db, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    _sample(db, 1, "changes_requested")
    monkeypatch.setattr(
        conductor,
        "_budget_evidence",
        lambda _task_id: {"graph_revision": 0},
    )
    monkeypatch.setattr(conductor, "_decision_evidence", lambda _task_id: [])
    task = {
        "id": "t-planner",
        "repo": "owner/repo",
        "base_branch": "main",
        "conductor_model": "opus",
        "budget_usd": 30.0,
        "task_text": "Fix the bounded defect.",
    }

    prompt = conductor.planner_prompt(task, [], [], task_class="bug-fix")
    context = json.loads(prompt.rsplit("\n", 1)[1])
    rejection = context["class_feedback"]["recent_rejections"][0]
    assert rejection["task_class"] == "bug-fix"
    assert rejection["verdict"] == "changes_requested"
    assert rejection["recipe_run_id"] is not None
    assert "Use class_feedback" in prompt


def test_advisory_rejection_does_not_enter_delivery_correction_loop():
    from factory.orchestration import factory_conductor as conductor

    run = {
        "id": 1,
        "node_key": feedback.REVIEW_NODE_KEY,
        "status": "succeeded",
        "outcome_json": json.dumps(
            {"value": {"verdict": "changes_requested", "summary": "revise recipe"}}
        ),
    }
    assert conductor._pending_correction([], [run]) is None


def test_full_advisory_window_restores_delivery_with_no_inflight_delivery(db):
    transition = BASE_TIME
    with Session(db) as session:
        session.add(
            FactoryClassTier(
                task_class="bug-fix",
                routing_tier="advisory",
                transitioned_at=transition,
                updated_at=transition,
            )
        )
        session.commit()
    for number in range(1, 21):
        _sample(
            db,
            number,
            "approve" if number <= 13 else "changes_requested",
            sample_kind="advisory",
            reviewed_at=transition + timedelta(minutes=number),
        )
    _configure(_policy())
    receive_issue(
        "owner/repo",
        999,
        "recovered bug class",
        "body",
        "https://github.com/owner/repo/issues/999",
        "operator",
    )

    admitted = admit_next("scheduler")
    assert admitted["ok"]
    assert admitted["lane"] == "delivery"
    assert admitted["receipt"]["routing_tier"] == "delivery"
    assert feedback.feedback_for_class("bug-fix")["sample_count"] == 0
    with Session(db) as session:
        assert session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "class_tier_restored")
        ).one()


def _run_node(task: dict, node_key: str, value: dict, session_id: int) -> None:
    from factory.orchestration import factory_conductor as conductor

    workflow = f"factory-node:{task['id']}:{node_key}:1"
    context = {
        "repo": task["repo"],
        "branch": f"factory/{task['id']}",
        "workflow_id": workflow,
        "artifact_path": f".factory/{task['id']}/{node_key}-1.json",
        "artifact_schema": conductor._schema(node_key),
        "hydration_branch": "main",
        "retry_context": "[]",
    }
    assert conductor.reserve_node(task["id"], node_key, workflow, context)
    result = {
        "status": "succeeded",
        "session_id": session_id,
        "cost_usd": 0.25,
        "head_sha": None,
        "value": value,
        "artifact": {"status": "ok", "value": value},
        "cleanup": {"status": "completed"},
    }
    assert conductor.graph.record_dispatch(task["id"], node_key, 1, session_id, None).ok
    assert conductor.graph.record_outcome(
        task["id"], node_key, 1, "succeeded", 0.25, None, json.dumps(result)
    ).ok


def test_demoted_task_runs_comment_and_independent_review_end_to_end(db, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    for number in range(1, 21):
        _sample(db, number, "approve" if number <= 11 else "changes_requested")
    policy = _policy(999)
    _configure(policy)
    receive_issue(
        "owner/repo",
        999,
        "new bug",
        "body",
        "https://github.com/owner/repo/issues/999",
        "operator",
    )
    admitted = admit_next("scheduler")
    task = conductor._task(admitted["task_id"])

    conductor.reconcile_task(task["id"], policy, object())
    assert [node["node_key"] for node in conductor.graph.load_graph(task["id"])] == [
        feedback.ADVISORY_NODE_KEY
    ]
    url = "https://github.com/owner/repo/issues/999#issuecomment-1"
    _run_node(
        task,
        feedback.ADVISORY_NODE_KEY,
        {"status": "complete", "summary": "Safer recipe", "comment_url": url},
        100,
    )
    conductor.reconcile_task(task["id"], policy, object())
    assert [node["node_key"] for node in conductor.graph.load_graph(task["id"])] == [
        feedback.ADVISORY_NODE_KEY,
        feedback.REVIEW_NODE_KEY,
    ]
    _run_node(
        task,
        feedback.REVIEW_NODE_KEY,
        {"verdict": "approve", "summary": "Recipe is usable", "comment_url": url},
        101,
    )
    monkeypatch.setattr(
        conductor,
        "github_list",
        lambda *_args: [
            {
                "html_url": url,
                "body": (
                    "## Factory advisory\n\n### Why delivery is paused\n\n"
                    "Below the floor.\n\n### Suggested recipe\n\n"
                    "Investigate, implement, test, and review.\n\n"
                    "### Evidence\n\nRecorded outcomes.\n\n"
                    f"<!-- factory-feedback-advisory:{task['id']} -->"
                ),
            }
        ],
    )
    conductor.reconcile_task(task["id"], policy, object())

    snapshot = controls.task_snapshot(task["id"])
    assert snapshot["state"] == "succeeded"
    with Session(db) as session:
        sample = session.exec(
            select(FactoryReviewVerdict).where(
                FactoryReviewVerdict.task_id == task["id"]
            )
        ).one()
        assert sample.sample_kind == "advisory"
        assert sample.verdict == "approve"
        assert sample.recipe_run_id is not None
        assert sample.review_run_id != sample.recipe_run_id


def test_unverified_advisory_comment_cannot_approve_recovery(db, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    for number in range(1, 21):
        _sample(db, number, "approve" if number <= 11 else "changes_requested")
    policy = _policy(999)
    _configure(policy)
    receive_issue(
        "owner/repo",
        999,
        "new bug",
        "body",
        "https://github.com/owner/repo/issues/999",
        "operator",
    )
    admitted = admit_next("scheduler")
    task = conductor._task(admitted["task_id"])

    conductor.reconcile_task(task["id"], policy, object())
    url = "https://github.com/owner/repo/issues/999#issuecomment-1"
    _run_node(
        task,
        feedback.ADVISORY_NODE_KEY,
        {"status": "complete", "summary": "Safer recipe", "comment_url": url},
        100,
    )
    conductor.reconcile_task(task["id"], policy, object())
    _run_node(
        task,
        feedback.REVIEW_NODE_KEY,
        {"verdict": "approve", "summary": "Recipe is usable", "comment_url": url},
        101,
    )
    monkeypatch.setattr(conductor, "github_list", lambda *_args: [])
    conductor.reconcile_task(task["id"], policy, object())

    assert controls.task_snapshot(task["id"])["state"] == "failed"
    recovery = feedback.feedback_for_class("bug-fix")["recovery_window"]
    assert recovery["sample_count"] == 1
    assert recovery["approval_count"] == 0
    assert recovery["rejection_count"] == 1
    with Session(db) as session:
        sample = session.exec(
            select(FactoryReviewVerdict).where(
                FactoryReviewVerdict.task_id == task["id"]
            )
        ).one()
        assert sample.verdict == "unparseable"
