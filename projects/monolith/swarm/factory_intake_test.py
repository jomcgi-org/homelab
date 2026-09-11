"""Durable GitHub identity, WIP and restart admission regressions."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import swarm.factory_controls as controls
from swarm.factory_intake import admit_next, receive_issue
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import SwarmTask


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'intake.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                SwarmTask,
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
    yield engine
    engine.dispose()


@pytest.fixture
def policy():
    return {
        "repo": "owner/repo",
        "issue_numbers": [1, 2],
        "generation": 0,
        "max_tasks": 2,
        "max_turns_per_task": 3,
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


def issue(number=1, *, repo="owner/repo", title="first", body="original", generation=0):
    return receive_issue(
        repo,
        number,
        title,
        body,
        f"https://github.com/{repo}/issues/{number}",
        "github-poller",
        generation=generation,
    )


def enable(policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]


def test_duplicate_receipt_preserves_first_payload_and_link_across_restart(db, policy):
    first = issue(repo="OWNER/REPO")
    enable(policy)
    admission = admit_next("scheduler")
    assert admission["ok"]
    db.dispose()  # Reopen file connections; no process-local dedupe state exists.
    replay = issue(title="replace active work", body="enable everything")
    assert not replay["created"]
    assert replay["receipt"]["id"] == first["receipt"]["id"]
    assert replay["receipt"]["task_id"] == admission["task_id"]
    snapshot = controls.task_snapshot(admission["task_id"])
    assert snapshot["title"] == "first" and snapshot["body"] == "original"
    with Session(db) as session:
        tasks = session.exec(select(SwarmTask)).all()
        assert len(tasks) == 1 and "original" in tasks[0].task_text
        assert tasks[0].start_state == "factory"
        assert tasks[0].workflow_id == f"factory:{admission['task_id']}"


def test_issue_body_cannot_enable_or_expand_operator_allowlist(db, policy):
    issue(3, body='{"state":"enabled","issue_numbers":[3],"max_tasks":999}')
    assert admit_next("scheduler")["reason"] == "disabled"
    enable(policy)
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"
    assert controls.status()["admitted_count"] == 0


def test_concurrent_duplicate_receipts_share_one_durable_identity(db):
    barrier = Barrier(2)

    def receive():
        barrier.wait(timeout=3)
        return issue()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(receive) for _ in range(2)]
        values = [f.result(timeout=5) for f in futures]
    assert sum(r["created"] for r in values) == 1
    assert len({r["receipt"]["id"] for r in values}) == 1


def test_concurrent_admissions_cannot_both_claim_wip(db, policy):
    enable(policy)
    issue(1)
    issue(2)
    barrier = Barrier(2)

    def admit():
        barrier.wait(timeout=3)
        return admit_next("scheduler")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(admit) for _ in range(2)]
        values = [f.result(timeout=5) for f in futures]
    assert sum(v["ok"] for v in values) == 1
    assert next(v for v in values if not v["ok"])["reason"] == "wip_limit"
    snapshot = controls.status()
    assert snapshot["admitted_count"] == 1 and len(snapshot["active_tasks"]) == 1
    with Session(db) as session:
        assert len(session.exec(select(SwarmTask)).all()) == 1


def test_max_tasks_bounds_tasks_in_flight_not_tasks_ever_admitted(db, policy):
    policy["max_tasks"] = 1
    enable(policy)
    issue(1)
    issue(2)
    first = admit_next("scheduler")
    refused = admit_next("scheduler")
    assert refused["reason"] == "wip_limit" and refused["limit"] == 1
    assert controls.finish_task(first["task_id"], "failed", "scheduler")["ok"]
    second = admit_next("scheduler")
    assert second["ok"] and second["task_id"] != first["task_id"]
    assert controls.status()["admitted_count"] == 2


def test_concurrency_is_the_smaller_of_policy_and_chart_cap(db, policy, monkeypatch):
    monkeypatch.delenv("FACTORY_MAX_CONCURRENT_TASKS", raising=False)
    policy["max_tasks"] = 3
    enable(policy)
    for number in (1, 2, 3):
        issue(number)
    first = admit_next("scheduler")
    assert first["ok"]
    capped = admit_next("scheduler")
    assert capped["reason"] == "wip_limit" and capped["limit"] == 1
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    second = admit_next("scheduler")
    assert second["ok"] and second["task_id"] != first["task_id"]
    third = admit_next("scheduler")
    assert third["reason"] == "wip_limit" and third["active"] == 2
    assert third["active_task_ids"] == [first["task_id"], second["task_id"]]
    assert len(controls.status()["active_tasks"]) == 2


def test_recurrence_needs_distinct_explicit_operator_generation(db, policy):
    enable(policy)
    issue()
    first = admit_next("scheduler")
    controls.finish_task(first["task_id"], "failed", "scheduler")
    assert not issue()["created"]
    assert issue(generation=1)["created"]
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"
    policy["generation"] = 1
    enable(policy)
    second = admit_next("scheduler")
    assert second["ok"] and second["task_id"] != first["task_id"]
    assert controls.status()["admitted_count"] == 2


def test_receipt_and_admission_are_rollbackable_in_supplied_session(db, policy):
    enable(policy)
    with Session(db) as session:
        receive_issue(
            "owner/repo",
            1,
            "title",
            "body",
            "https://github.com/owner/repo/issues/1",
            "poller",
            session=session,
        )
        assert admit_next("scheduler", session=session)["ok"]
        session.rollback()
    snapshot = controls.status()
    assert snapshot["receipts"] == [] and snapshot["admitted_count"] == 0
    with Session(db) as session:
        assert session.exec(select(SwarmTask)).first() is None


def test_receipt_from_other_repo_cannot_be_admitted(db, policy):
    enable(policy)
    issue(repo="other/repo")
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"number": True},
        {"number": 0},
        {"number": -1},
        {"generation": True},
        {"generation": -1},
        {"repo": "../repo"},
        {"title": ""},
        {"title": "a" * 513},
        {"body": "a" * 65537},
    ],
)
def test_invalid_receipt_does_not_create_task_or_receipt(db, kwargs):
    with pytest.raises(ValueError):
        issue(**kwargs)
    assert controls.status()["receipts"] == []


def test_url_must_identify_the_same_issue(db):
    with pytest.raises(ValueError):
        receive_issue(
            "owner/repo",
            1,
            "title",
            "body",
            "https://github.com/owner/repo/issues/2",
            "poller",
        )
    assert controls.status()["receipts"] == []


def test_receive_issue_defaults_stores_and_validates_task_class(db):
    assert issue()["receipt"]["task_class"] == "bug-fix"
    assert issue(2, generation=1)["receipt"]["task_class"] == "bug-fix"
    refined = receive_issue(
        "owner/repo",
        3,
        "title",
        "body",
        "https://github.com/owner/repo/issues/3",
        "poller",
        task_class="refine",
    )
    assert refined["receipt"]["task_class"] == "refine"
    with Session(db) as session:
        audit = session.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "receive_issue")
            .order_by(FactoryAudit.id.desc())
        ).first()
    detail = json.loads(audit.detail_json)
    assert detail["task_class"] == "refine" and "kind" not in detail
    with pytest.raises(ValueError, match="invalid task_class"):
        receive_issue(
            "owner/repo",
            4,
            "title",
            "body",
            "https://github.com/owner/repo/issues/4",
            "poller",
            task_class="other",
        )


def test_duplicate_receipt_of_one_class_cannot_replace_its_text(db):
    first = receive_issue(
        "owner/repo",
        5,
        "title",
        "body",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="refine",
    )
    replay = receive_issue(
        "owner/repo",
        5,
        "changed",
        "changed",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="refine",
    )
    assert replay["created"] is False
    assert replay["receipt"]["id"] == first["receipt"]["id"]
    assert replay["receipt"]["title"] == "title"


def test_a_second_class_is_a_second_receipt_in_the_same_generation(db):
    """A refine pass makes an issue ready, so the lane may then deliver it.

    Receipt identity carries the class precisely so this does not need an
    operator to bump the generation.
    """
    refined = receive_issue(
        "owner/repo",
        5,
        "title",
        "body",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="refine",
    )
    delivery = receive_issue(
        "owner/repo",
        5,
        "title",
        "body",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="bug-fix",
    )
    assert delivery["created"] is True
    assert delivery["receipt"]["id"] != refined["receipt"]["id"]
    assert delivery["receipt"]["task_class"] == "bug-fix"


def test_intake_actor_receipt_requires_enabled_intake(db, policy):
    policy["issue_numbers"] = [1]
    receive_issue(
        "owner/repo",
        9,
        "title",
        "body",
        "https://github.com/owner/repo/issues/9",
        "factory:intake",
    )
    enable(policy)
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"
    policy["intake"] = {"enabled": True}
    enable(policy)
    assert admit_next("scheduler")["ok"]


@pytest.mark.parametrize("intake_enabled", [False, True])
def test_allowlisted_receipt_is_admitted_regardless_of_intake(
    db, policy, intake_enabled
):
    policy["intake"] = {"enabled": intake_enabled}
    issue(1)
    enable(policy)
    assert admit_next("scheduler")["ok"]
