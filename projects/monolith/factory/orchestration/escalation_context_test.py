"""Hermetic escalation context evidence and endpoint tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from factory.orchestration import factory_controls, work_items
from factory.orchestration.escalation_context import escalation_context
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'escalation-context.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = (
        SwarmTask,
        SwarmNodeRun,
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        FactoryAudit,
        FactoryReviewVerdict,
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="test"))
        session.commit()
    monkeypatch.setattr(factory_controls, "get_engine", lambda: engine)
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def _policy() -> dict:
    return {
        "repo": "owner/repo",
        "issue_numbers": [20],
        "generation": 1,
        "max_tasks": 2,
        "max_turns_per_task": 3,
        "task_budget_usd": 10.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "max_attempts": 2,
        "task_timeout_seconds": 3600,
    }


def _work_item(number: int, title: str) -> WorkItem:
    return WorkItem(
        title=title,
        state="open",
        source_kind="github",
        source_ref=f"https://github.com/owner/repo/issues/{number}",
        trust="trusted",
        authority="github",
        github_repo="owner/repo",
        github_issue_number=number,
    )


def test_context_contains_all_five_evidence_sections(db):
    now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
    with Session(db) as session:
        parent = _work_item(12, "Parent")
        blocker = _work_item(2, "Blocker")
        current = _work_item(20, "Current")
        session.add_all([parent, blocker, current])
        session.flush()
        session.add_all(
            [
                WorkItemEdge(from_id=parent.id, to_id=current.id, kind="parent"),
                WorkItemEdge(from_id=blocker.id, to_id=current.id, kind="blocks"),
            ]
        )
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=20,
                generation=0,
                title="Earlier pass",
                body="",
                url="https://github.com/owner/repo/issues/20",
                actor="test",
                task_class="refine",
                state="failed",
                work_item_id=current.id,
                created_at=now,
            )
        )
        task = SwarmTask(
            id="task-context",
            task_text="work",
            repo="owner/repo",
            base_branch="main",
            conductor_model="opus",
            budget_usd=10.0,
            workflow_id="factory:task-context",
            start_state="factory",
        )
        session.add(task)
        session.flush()
        receipt = FactoryReceipt(
            repo="owner/repo",
            issue_number=20,
            generation=1,
            title="Choose delivery scope",
            body="  First line.\n\nSecond   line.  ",
            url="https://github.com/owner/repo/issues/20",
            actor="test",
            task_class="refine",
            routing_tier="advisory",
            state="escalated",
            task_id=task.id,
            policy_json=json.dumps(_policy()),
            work_item_id=current.id,
            escalation_json=json.dumps(
                {
                    "recommendation": "split",
                    "question": "Which scope should ship?",
                    "summary": "The issue contains two deliverables.",
                    "reason": "Scope is ambiguous",
                    "branch": "feat/context",
                    "downgraded": False,
                    "resolved": None,
                    "options": [
                        {
                            "key": "split",
                            "label": "Split the work",
                            "effect": "split",
                            "detail": {
                                "children": [{"title": "Child", "body": "body"}]
                            },
                        }
                    ],
                }
            ),
            created_at=now,
        )
        session.add(receipt)
        session.flush()
        runs = [
            SwarmNodeRun(
                task_id=task.id,
                node_key=f"node-{index}",
                attempt=1,
                status="completed",
                head_sha="a" * 40,
            )
            for index in (1, 2)
        ]
        session.add_all(runs)
        session.flush()
        session.add(
            FactoryReviewVerdict(
                task_id=task.id,
                review_run_id=runs[-1].id,
                task_class="refine",
                sample_kind="advisory",
                verdict="changes_requested",
                summary="Narrow the API surface",
                head_sha="a" * 40,
                reviewed_at=now,
            )
        )
        session.add_all(
            [
                FactoryStart(
                    task_id=task.id,
                    start_key="turn:node-1:1",
                    actor="test",
                    model="luna",
                    max_cost_usd=2.0,
                    status="succeeded",
                    cost_usd=1.25,
                ),
                FactoryStart(
                    task_id=task.id,
                    start_key="turn:node-2:1",
                    actor="test",
                    model="luna",
                    max_cost_usd=2.0,
                    status="succeeded",
                    cost_usd=2.0,
                ),
                FactoryAudit(
                    actor="test",
                    action="finish_task",
                    task_id=task.id,
                    detail_json=json.dumps(
                        {
                            "evidence": {
                                "pr_url": "https://github.com/owner/repo/pull/44",
                                "state": "open",
                            }
                        }
                    ),
                ),
            ]
        )
        session.commit()
        receipt_id = receipt.id

    with Session(db) as session:
        document = escalation_context(session, receipt_id)
    assert document["lines"] == [
        "#20 Choose delivery scope",
        "Scope is ambiguous",
        "2 attempts, last review changes_requested: Narrow the API surface",
        "$3.25 of $200.00 objective ceiling across 1 task",
        "child of #12, blocked by 1 item, 1 prior receipt (refine, failed)",
    ]
    assert document["ask"]["body_head"] == "First line. Second line."
    assert document["stopped"] == {
        "line": "Scope is ambiguous",
        "kind": "advisory",
        "recommendation": "split",
        "question": "Which scope should ship?",
        "summary": "The issue contains two deliverables.",
        "reason": "Scope is ambiguous",
        "downgraded": False,
        "resolved": False,
        "open": True,
    }
    assert document["options"] == ["Split the work"]
    assert document["escape"] == [
        "Close the issue",
        "Defer it",
        "Dismiss the escalation",
    ]
    assert document["happened"]["attempts"] == 2
    assert document["happened"]["last_review"]["verdict"] == "changes_requested"
    assert document["happened"]["pr"]["number"] == 44
    assert document["happened"]["branch"] == "feat/context"
    assert document["cost"] == {
        "line": "$3.25 of $200.00 objective ceiling across 1 task",
        "committed": 3.25,
        "ceiling": 200.0,
        "num_tasks": 1,
    }
    assert document["lineage"]["parent"]["github_issue_number"] == 12
    assert document["lineage"]["blocked_by"][0]["github_issue_number"] == 2
    assert document["lineage"]["prior_receipts"][0]["state"] == "failed"


def test_context_without_task_escalation_or_work_item(db):
    with Session(db) as session:
        receipt = FactoryReceipt(
            repo="owner/repo",
            issue_number=30,
            title="Plain receipt",
            body="body",
            url="https://github.com/owner/repo/issues/30",
            actor="test",
        )
        session.add(receipt)
        session.commit()
        receipt_id = receipt.id

    with Session(db) as session:
        document = escalation_context(session, receipt_id)
    assert document["ask"]["line"] == "#30 Plain receipt"
    assert document["stopped"] is None
    assert document["cost"] is None
    assert document["lineage"] is None
    assert document["happened"]["line"] == "no attempts yet"
    assert len(document["lines"]) == 5


def test_context_two_tasks_same_objective(db):
    """Two tasks on the same issue share the objective ceiling."""
    now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
    with Session(db) as session:
        current = _work_item(20, "Current")
        session.add(current)
        session.flush()

        task1 = SwarmTask(
            id="task-1",
            task_text="work",
            repo="owner/repo",
            base_branch="main",
            conductor_model="opus",
            budget_usd=10.0,
            workflow_id="factory:task-1",
            start_state="factory",
        )
        session.add(task1)
        session.flush()

        receipt1 = FactoryReceipt(
            repo="owner/repo",
            issue_number=20,
            generation=0,
            title="First task",
            body="",
            url="https://github.com/owner/repo/issues/20",
            actor="test",
            task_class="refine",
            state="succeeded",
            task_id=task1.id,
            policy_json=json.dumps(_policy()),
            work_item_id=current.id,
            created_at=now,
        )
        session.add(receipt1)
        session.flush()

        task2 = SwarmTask(
            id="task-2",
            task_text="work",
            repo="owner/repo",
            base_branch="main",
            conductor_model="opus",
            budget_usd=10.0,
            workflow_id="factory:task-2",
            start_state="factory",
        )
        session.add(task2)
        session.flush()

        receipt2 = FactoryReceipt(
            repo="owner/repo",
            issue_number=20,
            generation=1,
            title="Second task",
            body="",
            url="https://github.com/owner/repo/issues/20",
            actor="test",
            task_class="refine",
            state="escalated",
            task_id=task2.id,
            policy_json=json.dumps(_policy()),
            work_item_id=current.id,
            created_at=now,
        )
        session.add(receipt2)
        session.flush()

        session.add_all(
            [
                FactoryStart(
                    task_id=task1.id,
                    start_key="turn:node-1:1",
                    actor="test",
                    model="luna",
                    max_cost_usd=2.0,
                    status="succeeded",
                    cost_usd=50.0,
                ),
                FactoryStart(
                    task_id=task2.id,
                    start_key="turn:node-1:1",
                    actor="test",
                    model="luna",
                    max_cost_usd=2.0,
                    status="succeeded",
                    cost_usd=75.0,
                ),
            ]
        )
        session.commit()
        receipt_id = receipt2.id

    with Session(db) as session:
        document = escalation_context(session, receipt_id)
    assert document["cost"]["num_tasks"] == 2
    assert document["cost"]["committed"] == 125.0
    assert document["cost"]["ceiling"] == 200.0
