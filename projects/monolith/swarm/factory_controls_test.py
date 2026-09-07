"""File-backed control, reservation and stop-ordering regressions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

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
        f"sqlite:///{tmp_path / 'controls.db'}",
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
        "base_branch": "main",
        "turn_timeout_seconds": 60,
        "max_attempts": 2,
        "worker_model": "luna",
        "task_timeout_seconds": 3600,
    }


def admitted(policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    receive_issue(
        "owner/repo",
        1,
        "issue",
        "body",
        "https://github.com/owner/repo/issues/1",
        "poller",
    )
    return admit_next("scheduler")["task_id"]


def grant(task_id, key="one", **kwargs):
    return controls.authorize_start(
        task_id,
        key,
        "scheduler",
        model=kwargs.get("model", "luna"),
        max_cost_usd=kwargs.get("cost", 2.0),
    )


def test_policy_is_operator_only_and_pinned_for_active_task(db, policy):
    task = admitted(policy)
    policy["task_budget_usd"] = 99
    result = controls.set_control("configure", "operator", policy=policy)
    assert result == {
        "ok": False,
        "reason": "active_task",
        "state": "enabled",
        "version": 2,
    }
    assert controls.task_snapshot(task)["policy"]["task_budget_usd"] == 5
    with Session(db) as session:
        audits = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "configure")
        ).all()
        assert len(audits) == 2 and all(a.actor == "operator" for a in audits)


def test_pause_admissions_allows_admitted_work_and_task_pause_fences(db, policy):
    task = admitted(policy)
    controls.set_control("pause_admissions", "operator")
    assert not admit_next("scheduler")["ok"]
    assert controls.can_start(task)["ok"]
    controls.set_control("pause_task", "operator", task_id=task)
    assert grant(task)["reason"] == "task_paused"
    controls.set_control("resume_task", "operator", task_id=task)
    assert grant(task)["ok"]


def test_stop_cannot_be_reset_and_retains_unconfirmed_descendants(db, policy):
    task = admitted(policy)
    assert grant(task)["ok"]
    assert controls.set_control("stop", "operator")["ok"]
    assert controls.set_control("stop", "operator")["ok"]
    assert controls.set_control("enable", "replayed-operator")["reason"] == "stopped"
    assert (
        controls.set_control("configure", "operator", policy=policy)["reason"]
        == "stopped"
    )
    assert controls.can_start(task)["reason"] == "stopped"
    assert grant(task)["reason"] == "stopped"
    snapshot = controls.task_snapshot(task)
    assert snapshot["cancellation_requested"]
    assert snapshot["starts"][0]["status"] == "reserved"
    assert (
        controls.finish_task(task, "cancelled", "operator")["reason"]
        == "unresolved_starts"
    )
    assert controls.record_start_outcome(
        task, "one", "cancelled", "reconciler", cost_usd=0
    )["ok"]
    assert controls.finish_task(task, "cancelled", "operator")["ok"]
    assert controls.status()["state"] == "stopped"


def test_idempotent_reservation_conflicting_pin_and_pending_start(db, policy):
    task = admitted(policy)
    first = grant(task)
    replay = grant(task)
    assert first["start"] == replay["start"] and replay["replayed"]
    assert grant(task, cost=1)["reason"] == "conflicting_start_pin"
    assert grant(task, model="opus")["reason"] == "conflicting_start_pin"
    assert grant(task, "two")["reason"] == "start_pending"
    assert controls.task_snapshot(task)["turns_used"] == 1


def test_unknown_outcome_holds_budget_wip_and_blocks_retries(db, policy):
    task = admitted(policy)
    grant(task)
    result = controls.record_start_outcome(
        task, "one", "uncertain", "worker", session_id=7
    )
    assert result["start"]["status"] == "uncertain"
    assert grant(task, "two")["reason"] == "uncertain_outcome"
    assert not admit_next("scheduler")["ok"]
    assert (
        controls.finish_task(task, "failed", "scheduler")["reason"]
        == "unresolved_starts"
    )
    assert controls.task_snapshot(task)["committed_cost_usd"] == 2
    assert (
        controls.record_start_outcome(
            task, "one", "succeeded", "worker", cost_usd=1, session_id=7
        )["reason"]
        == "reconciliation_required"
    )
    assert (
        controls.record_start_outcome(
            task,
            "one",
            "succeeded",
            "operator",
            cost_usd=1,
            session_id=8,
            reconciled=True,
        )["reason"]
        == "conflicting_session"
    )
    assert controls.record_start_outcome(
        task, "one", "succeeded", "operator", cost_usd=1, session_id=7, reconciled=True
    )["ok"]
    assert grant(task, "two")["ok"]


def test_known_completion_missing_cost_consumes_full_ceiling_and_settles(db, policy):
    task = admitted(policy)
    grant(task)
    assert (
        controls.record_start_outcome(task, "one", "succeeded", "worker")["start"][
            "status"
        ]
        == "succeeded"
    )
    snapshot = controls.task_snapshot(task)
    assert snapshot["committed_cost_usd"] == 2
    assert snapshot["unresolved_starts"] == 0
    assert controls.finish_task(task, "succeeded", "scheduler")["ok"]


def test_task_turn_and_cost_limits_are_cumulative(db, policy):
    policy["task_budget_usd"] = 3
    task = admitted(policy)
    grant(task)
    controls.record_start_outcome(task, "one", "failed", "worker", cost_usd=2)
    assert grant(task, "two")["reason"] == "budget_limit"
    assert grant(task, "two", cost=1)["ok"]
    controls.record_start_outcome(task, "two", "failed", "worker", cost_usd=0)
    assert grant(task, "three", cost=1)["ok"]
    controls.record_start_outcome(task, "three", "succeeded", "worker", cost_usd=0)
    assert grant(task, "four", cost=1)["reason"] == "turn_limit"


def test_actual_overrun_is_retained_and_blocks_later_budget(db, policy):
    task = admitted(policy)
    grant(task)
    controls.record_start_outcome(task, "one", "succeeded", "worker", cost_usd=9)
    assert controls.task_snapshot(task)["committed_cost_usd"] == 9
    assert grant(task, "two", cost=0.1)["reason"] == "budget_limit"


def test_terminal_outcome_replay_is_exact(db, policy):
    task = admitted(policy)
    grant(task)
    first = controls.record_start_outcome(task, "one", "failed", "worker", cost_usd=1)
    assert controls.record_start_outcome(task, "one", "failed", "worker", cost_usd=1)[
        "replayed"
    ]
    assert (
        controls.record_start_outcome(task, "one", "succeeded", "worker", cost_usd=1)[
            "reason"
        ]
        == "conflicting_outcome"
    )
    assert controls.task_snapshot(task)["starts"][0] == first["start"]


def test_stop_orders_after_inflight_creation_guard_and_before_next(db, policy):
    task = admitted(policy)
    entered, release, stopping = Event(), Event(), Event()

    def create_session():
        with controls.start_guard(task) as decision:
            assert decision["ok"]
            entered.set()
            assert release.wait(3)
            return "session-persisted"

    def stop():
        stopping.set()
        return controls.set_control("stop", "operator")

    with ThreadPoolExecutor(max_workers=2) as executor:
        creation = executor.submit(create_session)
        assert entered.wait(3)
        stopped = executor.submit(stop)
        assert stopping.wait(3)
        assert not stopped.done()
        release.set()
        assert creation.result(3) == "session-persisted"
        assert stopped.result(3)["ok"]
    with controls.start_guard(task) as decision:
        assert decision["reason"] == "stopped"


def test_supplied_session_can_rollback_reservation_with_caller_graph_changes(
    db, policy
):
    task = admitted(policy)
    with Session(db) as session:
        assert controls.authorize_start(
            task, "one", "scheduler", model="luna", max_cost_usd=2, session=session
        )["ok"]
        session.rollback()
    assert controls.task_snapshot(task)["turns_used"] == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_tasks", True),
        ("max_tasks", 0),
        ("max_turns_per_task", 0),
        ("turn_timeout_seconds", 0),
        ("turn_timeout_seconds", None),
        ("max_attempts", 0),
        ("task_budget_usd", float("nan")),
        ("turn_budget_usd", float("inf")),
        ("task_budget_usd", -1),
        ("issue_numbers", [True]),
        ("issue_numbers", []),
        ("allowed_models", []),
        ("conductor_model", "unlisted"),
        ("generation", -1),
        ("base_branch", ""),
    ],
)
def test_invalid_policy_refuses_before_any_control_mutation(db, policy, key, value):
    policy[key] = value
    with pytest.raises(ValueError):
        controls.set_control("configure", "operator", policy=policy)
    assert controls.status()["state"] == "disabled"
    assert controls.status()["version"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, 0])
def test_invalid_start_budget_cannot_reserve(db, policy, value):
    task = admitted(policy)
    with pytest.raises(ValueError):
        grant(task, cost=value)
    assert controls.task_snapshot(task)["turns_used"] == 0


def test_unlisted_model_and_untrusted_policy_field_are_refused(db, policy):
    policy["auto_merge"] = True
    with pytest.raises(ValueError):
        controls.set_control("configure", "operator", policy=policy)
    del policy["auto_merge"]
    task = admitted(policy)
    assert grant(task, model="issue-selected-model")["reason"] == "model_not_allowed"


def test_task_deadline_fences_new_starts_without_claiming_existing_ceased(
    db, policy, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    task = admitted(policy)
    grant(task)
    future = datetime.now(timezone.utc) + timedelta(
        seconds=policy["task_timeout_seconds"] + 1
    )
    monkeypatch.setattr(controls, "_now", lambda: future)
    assert controls.can_start(task)["reason"] == "task_deadline"
    assert grant(task, "two")["reason"] == "task_deadline"
    with controls.start_guard(task) as decision:
        assert decision["reason"] == "task_deadline"
    snapshot = controls.task_snapshot(task)
    assert snapshot["limits"]["deadline_expired"]
    assert snapshot["starts"][0]["status"] == "reserved"
    assert (
        controls.finish_task(task, "failed", "scheduler")["reason"]
        == "unresolved_starts"
    )


def test_delivery_evidence_is_durable_and_conflicting_replay_refused(db, policy):
    task = admitted(policy)
    evidence = {
        "pr_url": "https://github.com/owner/repo/pull/9",
        "head_sha": "a" * 40,
        "review_session_id": 7,
        "state": "ready_for_review",
    }
    assert controls.finish_task(task, "succeeded", "scheduler", evidence=evidence)["ok"]
    db.dispose()
    assert controls.task_snapshot(task)["evidence"] == evidence
    assert controls.finish_task(task, "succeeded", "scheduler", evidence=evidence)["ok"]
    assert (
        controls.finish_task(
            task, "succeeded", "scheduler", evidence={"state": "changed"}
        )["reason"]
        == "conflicting_outcome"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("worker_model", "unlisted"),
        ("task_timeout_seconds", 0),
        ("task_timeout_seconds", None),
        ("task_timeout_seconds", True),
    ],
)
def test_invalid_additional_policy_fields(db, policy, key, value):
    policy[key] = value
    with pytest.raises(ValueError):
        controls.set_control("configure", "operator", policy=policy)


def test_unknown_task_control_refusal_is_audited_without_invalid_fk(db):
    result = controls.set_control("pause_task", "operator", task_id="not-a-task")
    assert result["reason"] == "task_not_active"
    with Session(db) as session:
        audit = session.exec(select(FactoryAudit)).one()
        assert audit.task_id is None
        assert '"requested_task_id":"not-a-task"' in audit.detail_json


def test_supplied_session_cached_rows_cannot_bypass_new_pause_or_stop(db, policy):
    task = admitted(policy)
    with Session(db) as session:
        cached_control = session.get(FactoryControl, "factory")
        cached_receipt = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task)
        ).one()
        assert cached_control.state == "enabled" and not cached_receipt.task_paused
        controls.set_control("pause_task", "operator", task_id=task)
        assert controls.can_start(task, session=session)["reason"] == "task_paused"
        controls.set_control("stop", "operator")
        assert controls.can_start(task, session=session)["reason"] == "stopped"
