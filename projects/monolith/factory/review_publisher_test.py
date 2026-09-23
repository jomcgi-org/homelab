from __future__ import annotations

import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import factory.orchestration.factory_controls as controls
import factory.orchestration.factory_landing as landing
import factory.review_publisher as publisher
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

TASK_ID = "t-review-publisher"
REPO = "owner/repo"
BRANCH = f"factory/{TASK_ID}"
HEAD = "a" * 40


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review-publisher.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
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
                SwarmNodeRun,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryAudit,
            )
        ],
    )
    with Session(engine) as session:
        session.add(
            FactoryControl(
                id="factory",
                actor="test",
                state="enabled",
                policy_json=json.dumps({"repo": REPO, "base_branch": "main"}),
                version=7,
            )
        )
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def review_outcome(verdict="approve", *, head=HEAD, pr_number=3, valid=True):
    value = {
        "verdict": verdict,
        "head_sha": head,
        "pr_number": pr_number,
        "summary": "Reviewed the exact head.",
    }
    return json.dumps(
        {
            "value": value,
            "artifact": {
                "status": "ok" if valid else "invalid",
                "value": value,
                "errors": [] if valid else ["schema mismatch"],
            },
        }
    )


def seed(db, *, review=True, review_status="succeeded", review_valid=True):
    with Session(db) as session:
        session.add(
            SwarmTask(
                id=TASK_ID,
                task_text="deliver",
                repo=REPO,
                base_branch="main",
                conductor_model="opus",
                workflow_id=f"factory:{TASK_ID}",
            )
        )
        session.flush()
        session.add(
            FactoryReceipt(
                repo=REPO,
                issue_number=3835,
                title="review gate",
                body="",
                url="https://github.com/owner/repo/issues/3835",
                actor="test",
                state="succeeded",
                task_id=TASK_ID,
                policy_json=json.dumps({"repo": REPO, "base_branch": "main"}),
            )
        )
        session.add(
            FactoryAudit(
                actor="factory:intake",
                action="admit_next",
                task_id=TASK_ID,
                detail_json=json.dumps({"policy_version": 7}),
            )
        )
        session.add(
            FactoryAudit(
                actor="factory:reconciler",
                action="finish_task",
                task_id=TASK_ID,
                detail_json=json.dumps(
                    {
                        "evidence": {
                            "pr_url": "https://github.com/owner/repo/pull/3",
                            "head_sha": HEAD,
                        }
                    }
                ),
            )
        )
        session.add(
            SwarmNodeRun(
                task_id=TASK_ID,
                node_key="implement_fix",
                attempt=1,
                session_id=10,
                status="succeeded",
                head_sha=HEAD,
            )
        )
        if review:
            session.add(
                SwarmNodeRun(
                    task_id=TASK_ID,
                    node_key="review_fix",
                    attempt=1,
                    session_id=11,
                    status=review_status,
                    head_sha=HEAD,
                    outcome_json=review_outcome(valid=review_valid),
                )
            )
        session.commit()


def current_pull(*, head=HEAD, branch=BRANCH, repo=REPO, base="main"):
    return {
        "number": 3,
        "state": "open",
        "draft": False,
        "head": {"sha": head, "ref": branch, "repo": {"full_name": repo}},
        "base": {"ref": base},
    }


def audit_details(db, action):
    with Session(db) as session:
        return [
            json.loads(row.detail_json)
            for row in session.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == action)
                .order_by(FactoryAudit.id)
            ).all()
        ]


def test_approved_independent_current_review_publishes_success(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(landing, "github_get", lambda *_args: current_pull())
    evidence = publisher.collect(TASK_ID)
    assert isinstance(evidence, publisher.ReviewEvidence)
    assert evidence.head_sha == HEAD
    assert evidence.policy_version == 7
    assert evidence.review_session_id == 11

    calls = []

    def post(repo, suffix, payload, token):
        calls.append((repo, suffix, payload, token))
        return {"id": 91}

    monkeypatch.setattr(publisher, "_github_post", post)
    result = publisher.publish(evidence, lambda: "app-installation-token")
    assert result == {
        "action": "published",
        "conclusion": "success",
        "check_id": 91,
        "head_sha": HEAD,
        "review_run_id": evidence.review_run_id,
    }
    assert calls == [
        (
            REPO,
            "check-runs",
            {
                "name": "factory/review",
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "details_url": "https://private.jomcgi.dev/agents/session/11",
                "external_id": str(evidence.review_run_id),
                "output": {
                    "title": "factory/review",
                    "summary": (
                        "Independent factory review approved this exact pull "
                        "request head."
                    ),
                },
            },
            "app-installation-token",
        )
    ]


def test_head_move_refuses_without_post(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(
        landing, "github_get", lambda *_args: current_pull(head="b" * 40)
    )
    evidence = publisher.collect(TASK_ID)
    assert evidence == publisher.Refusal(TASK_ID, "head_moved")
    monkeypatch.setattr(
        publisher, "_github_post", lambda *_args: pytest.fail("must not publish")
    )
    assert publisher.publish(evidence, lambda: "token") == {
        "action": "refused",
        "reason": "head_moved",
    }


def test_review_session_cannot_authorize_its_own_implementation(db, monkeypatch):
    seed(db)
    with Session(db) as session:
        review = session.exec(
            select(SwarmNodeRun).where(SwarmNodeRun.node_key == "review_fix")
        ).one()
        review.session_id = 10
        session.add(review)
        session.commit()
    monkeypatch.setattr(
        landing, "github_get", lambda *_args: pytest.fail("must fail before GitHub")
    )
    assert publisher.collect(TASK_ID) == publisher.Refusal(
        TASK_ID, "review_not_independent"
    )


@pytest.mark.parametrize("forgery", ["pr_number", "run_head"])
def test_review_claim_cannot_replace_control_plane_delivery(db, monkeypatch, forgery):
    seed(db)
    with Session(db) as session:
        review = session.exec(
            select(SwarmNodeRun).where(SwarmNodeRun.node_key == "review_fix")
        ).one()
        if forgery == "pr_number":
            review.outcome_json = review_outcome(pr_number=4)
        else:
            review.head_sha = "b" * 40
        session.add(review)
        session.commit()
    monkeypatch.setattr(
        landing, "github_get", lambda *_args: pytest.fail("must fail before GitHub")
    )
    assert publisher.collect(TASK_ID) == publisher.Refusal(
        TASK_ID, "review_identity_mismatch"
    )


def test_later_rejection_invalidates_prior_success_on_same_head(db, monkeypatch):
    seed(db)
    with Session(db) as session:
        first = session.exec(
            select(SwarmNodeRun).where(SwarmNodeRun.node_key == "review_fix")
        ).one()
        session.add(
            FactoryAudit(
                actor=publisher.ACTOR,
                action="review_published",
                task_id=TASK_ID,
                detail_json=json.dumps(
                    {
                        "head_sha": HEAD,
                        "review_run_id": first.id,
                        "check_id": 91,
                        "conclusion": "success",
                    }
                ),
            )
        )
        session.add(
            SwarmNodeRun(
                task_id=TASK_ID,
                node_key="review_1",
                attempt=1,
                session_id=12,
                status="succeeded",
                head_sha=HEAD,
                outcome_json=review_outcome("changes_requested"),
            )
        )
        session.commit()
    monkeypatch.setattr(landing, "github_get", lambda *_args: current_pull())
    evidence = publisher.collect(TASK_ID)
    assert isinstance(evidence, publisher.Refusal)
    assert evidence.reason == "review_superseded"
    calls = []
    monkeypatch.setattr(
        publisher,
        "_github_post",
        lambda repo, suffix, payload, token: calls.append(payload) or {"id": 92},
    )
    result = publisher.publish(evidence, lambda: "review-publisher-token")
    assert result["conclusion"] == "failure"
    assert calls[0]["head_sha"] == HEAD
    assert calls[0]["conclusion"] == "failure"
    assert calls[0]["external_id"] == str(evidence.invalidation.review_run_id)


@pytest.mark.parametrize(
    ("review", "status", "valid", "reason"),
    [
        (False, "succeeded", True, "review_missing"),
        (True, "failed", True, "review_not_succeeded"),
        (True, "cancelled", True, "review_not_succeeded"),
        (True, "succeeded", False, "review_artifact_invalid"),
    ],
)
def test_missing_failed_or_invalid_review_never_publishes(
    db, monkeypatch, review, status, valid, reason
):
    seed(db, review=review, review_status=status, review_valid=valid)
    monkeypatch.setattr(
        landing, "github_get", lambda *_args: pytest.fail("must fail before GitHub")
    )
    assert publisher.collect(TASK_ID) == publisher.Refusal(TASK_ID, reason)


@pytest.mark.parametrize(
    ("pull", "reason"),
    [
        (current_pull(branch="claude/untrusted"), "pr_identity_changed"),
        (current_pull(repo="attacker/fork"), "pr_identity_changed"),
        (current_pull(base="release"), "pr_identity_changed"),
    ],
)
def test_untrusted_git_identity_cannot_authorize_publication(
    db, monkeypatch, pull, reason
):
    seed(db)
    monkeypatch.setattr(landing, "github_get", lambda *_args: pull)
    assert publisher.collect(TASK_ID) == publisher.Refusal(TASK_ID, reason)


def test_missing_dedicated_token_skips_and_never_uses_general_pat(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(landing, "github_get", lambda *_args: current_pull())
    monkeypatch.delenv(publisher.PUBLISHER_TOKEN_ENV, raising=False)
    monkeypatch.setenv("GITHUB_API_TOKEN", "general-pat-must-not-be-used")
    monkeypatch.setattr(
        publisher, "_github_post", lambda *_args: pytest.fail("must not publish")
    )
    result = publisher.publish(publisher.collect(TASK_ID))
    assert result == {"action": "skipped", "reason": "publisher_token_missing"}
    assert audit_details(db, "review_publish_skipped") == [
        {"reason": "publisher_token_missing"}
    ]


def test_enablement_is_explicit_and_defaults_off(monkeypatch):
    monkeypatch.delenv(publisher.PUBLISH_ENABLED_ENV, raising=False)
    assert publisher.enabled() is False
    monkeypatch.setenv(publisher.PUBLISH_ENABLED_ENV, "false")
    assert publisher.enabled() is False
    monkeypatch.setenv(publisher.PUBLISH_ENABLED_ENV, "true")
    assert publisher.enabled() is True


def test_landing_delivery_can_publish_review_before_task_settles(db, monkeypatch):
    seed(db)
    with Session(db) as session:
        receipt = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == TASK_ID)
        ).one()
        receipt.state = "landing"
        session.add(receipt)
        audit = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == TASK_ID, FactoryAudit.action == "finish_task"
            )
        ).one()
        audit.action = "delivery_ready"
        session.add(audit)
        session.commit()
    monkeypatch.setattr(landing, "github_get", lambda *_args: current_pull())
    evidence = publisher.collect(TASK_ID)
    assert isinstance(evidence, publisher.ReviewEvidence)
    assert evidence.head_sha == HEAD
