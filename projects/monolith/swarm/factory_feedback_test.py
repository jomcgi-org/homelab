"""Hermetic verdict-window and feedback-routing tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from swarm import factory_controls as controls
from swarm import factory_feedback as feedback
from swarm.factory_intake import admit_next, receive_issue
from swarm.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
)
from swarm.models import SwarmNodeRun, SwarmTask


@pytest.fixture
def db(tmp_path, monkeypatch):
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
        SwarmNodeRun,
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
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    yield engine
    engine.dispose()


def _sample(
    engine,
    number: int,
    verdict: str,
    *,
    task_class: str = "bug-fix",
    reviewed_at: datetime | None = None,
    routing_tier: str | None = None,
) -> None:
    task_id = f"t-sample-{task_class}-{number}"
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
                routing_tier=routing_tier,
                state="succeeded",
                task_id=task_id,
            )
        )
        run = SwarmNodeRun(
            task_id=task_id,
            node_key="review_initial",
            attempt=1,
            status="succeeded",
            outcome_json="{}",
        )
        session.add(run)
        session.flush()
        session.add(
            FactoryReviewVerdict(
                task_id=task_id,
                review_run_id=run.id,
                task_class=task_class,
                verdict=verdict,
                summary=f"{task_class} {verdict} {number}",
                reviewed_at=reviewed_at
                or datetime(2026, 9, 1, tzinfo=timezone.utc)
                + timedelta(minutes=number),
            )
        )
        session.commit()


def _task_with_runs(engine):
    task_id = "t-first-pass"
    values = (
        ("review_initial", 1, "failed", {}),
        (
            "review_initial",
            2,
            "succeeded",
            {
                "verdict": "changes_requested",
                "summary": "Add the missing boundary test.",
                "head_sha": "a" * 40,
            },
        ),
        (
            "review_1",
            1,
            "succeeded",
            {
                "verdict": "approve",
                "summary": "Correction approved.",
                "head_sha": "b" * 40,
            },
        ),
    )
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
                issue_number=500,
                generation=0,
                title="task",
                body="",
                url="https://github.com/owner/repo/issues/500",
                actor="test",
                task_class="bug-fix",
                state="admitted",
                task_id=task_id,
            )
        )
        session.flush()
        runs = []
        for node_key, attempt, status, artifact in values:
            row = SwarmNodeRun(
                task_id=task_id,
                node_key=node_key,
                attempt=attempt,
                status=status,
                outcome_json=json.dumps({"value": artifact}),
            )
            session.add(row)
            session.flush()
            runs.append(
                {
                    "id": row.id,
                    "node_key": node_key,
                    "attempt": attempt,
                    "status": status,
                    "outcome_json": row.outcome_json,
                    "created_at": row.created_at,
                    "finished_at": row.created_at,
                }
            )
        session.commit()
    return task_id, runs


def test_first_valid_review_is_recorded_once_across_retries_and_reconciliation(db):
    task_id, runs = _task_with_runs(db)
    first = feedback.record_first_pass(task_id, runs)
    replay = feedback.record_first_pass(task_id, list(reversed(runs)))
    assert first == replay
    assert first["verdict"] == "changes_requested"
    assert first["summary"] == "Add the missing boundary test."
    with Session(db) as session:
        verdicts = session.exec(select(FactoryReviewVerdict)).all()
        audits = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "first_pass_verdict_recorded"
            )
        ).all()
    assert len(verdicts) == 1
    assert len(audits) == 1


def test_window_is_latest_twenty_delivery_tasks_and_classes_are_isolated(db):
    for number in range(1, 22):
        _sample(db, number, "changes_requested" if number == 1 else "approve")
    for number in range(101, 121):
        _sample(db, number, "changes_requested", task_class="docs")

    bugs = feedback.feedback_for_class("bug-fix")
    docs = feedback.feedback_for_class("docs")
    assert bugs["sample_count"] == 20
    assert bugs["approval_count"] == 20
    assert bugs["rejection_count"] == 0
    assert bugs["tier"] == "delivery"
    assert docs["sample_count"] == 20
    assert docs["approval_rate"] == 0
    assert docs["tier"] == "advisory"


def test_floor_holds_at_sixty_and_recovers_only_above_it(db):
    # Oldest first: two rejections are deliberately the first samples that the
    # next approvals push out of the count-based window.
    verdicts = ["changes_requested"] * 9 + ["approve"] * 11
    for number, verdict in enumerate(verdicts, start=1):
        _sample(db, number, verdict)
    assert feedback.route_for_class("bug-fix")["approval_rate"] == 0.55
    assert feedback.route_for_class("bug-fix")["tier"] == "advisory"

    # Pin the prior demotion, as admission does.
    with Session(db) as session:
        session.add(
            FactoryClassTier(
                task_class="bug-fix",
                routing_tier="advisory",
                updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )
        )
        session.commit()

    _sample(db, 21, "approve")
    at_floor = feedback.route_for_class("bug-fix")
    assert at_floor["approval_rate"] == 0.60
    assert at_floor["decision"] == "at_floor_hold"
    assert at_floor["tier"] == "advisory"

    _sample(db, 22, "approve")
    recovered = feedback.route_for_class("bug-fix")
    assert recovered["approval_rate"] == 0.65
    assert recovered["decision"] == "above_floor"
    assert recovered["tier"] == "delivery"


def test_sample_and_tier_limits_preserve_backward_compatible_defaults(db):
    for number in range(1, 20):
        _sample(db, number, "changes_requested")
    insufficient = feedback.route_for_class("bug-fix")
    assert insufficient["sample_count"] == 19
    assert insufficient["tier"] == "delivery"
    assert feedback.route_for_class("advisory-diagnosis")["tier"] == "advisory"

    for number in range(101, 121):
        _sample(db, number, "changes_requested", task_class="judgment-analysis")
    judgment = feedback.route_for_class("judgment-analysis")
    assert judgment["tier"] == "advisory"
    assert judgment["task_class"] == "judgment-analysis"


def test_admission_uses_feedback_route_and_pins_it_on_the_receipt(db):
    for number in range(1, 21):
        verdict = "approve" if number <= 11 else "changes_requested"
        _sample(db, number, verdict)
    policy = {
        "repo": "owner/repo",
        "issue_numbers": [999],
        "generation": 0,
        "max_tasks": {"delivery": 1, "advisory": 1},
        "max_turns_per_task": 4,
        "task_budget_usd": 5.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "max_attempts": 2,
        "task_timeout_seconds": 3600,
    }
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
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
    with Session(db) as session:
        transition = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "class_tier_demoted")
        ).one()
    detail = json.loads(transition.detail_json)
    assert detail["sample_count"] == 20
    assert detail["approval_rate"] == 0.55


def test_advisory_comment_requires_the_recipe_sections(monkeypatch):
    from swarm import factory_conductor as conductor

    task = {"id": "t-one", "repo": "owner/repo", "issue_number": 9}
    url = "https://github.com/owner/repo/issues/9#issuecomment-1"
    monkeypatch.setattr(
        conductor,
        "github_list",
        lambda *_args: [
            {
                "html_url": url,
                "body": "## Factory advisory\n<!-- factory-feedback-advisory:t-one -->",
            }
        ],
    )
    assert feedback._comment(task, url) is None
