"""File-backed control, reservation and stop-ordering regressions."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import factory.orchestration.factory_controls as controls
from factory.orchestration.factory_intake import admit_next, receive_issue
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
    FactoryReviewVerdict,
    FactoryStart,
)
from factory.orchestration.models import SwarmTask


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
                FactoryClassTier,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryReviewVerdict,
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


def test_escalation_view_carries_work_item_id_from_receipt_snapshot(db):
    with Session(db) as session:
        item = WorkItem(
            title="linked",
            state="open",
            source_kind="factory",
            trust="trusted",
        )
        session.add(item)
        session.flush()
        receipt = FactoryReceipt(
            repo="owner/repo",
            issue_number=42,
            title="needs a decision",
            body="body",
            url="https://github.com/owner/repo/issues/42",
            actor="test",
            work_item_id=item.id,
            escalation_json=json.dumps(
                {
                    "recommendation": "hold",
                    "question": "Wait?",
                    "options": [],
                }
            ),
        )
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        view = controls.escalation_view(controls._snapshot(session, receipt))

        assert view is not None
        assert view["work_item_id"] == item.id


def test_issue_body_hash_ignores_whitespace_only_changes():
    original = "First line\n\nSecond\tline"
    whitespace_only = "  First line   Second line\n"
    assert controls.issue_body_hash(original) == controls.issue_body_hash(
        whitespace_only
    )
    assert controls.issue_body_hash(original) != controls.issue_body_hash(
        "First line, changed Second line"
    )


def test_policy_is_operator_only_and_pinned_for_active_task(db, policy):
    task = admitted(policy)
    before = controls.task_snapshot(task)
    policy.update(task_budget_usd=99, generation=1)
    result = controls.set_control("configure", "operator", policy=policy)
    assert result == {
        "ok": True,
        "reason": None,
        "state": "enabled",
        "version": 3,
    }
    assert controls.task_snapshot(task) == before
    assert controls.can_start(task)["ok"]
    assert controls.task_snapshot(task)["policy"]["task_budget_usd"] == 5
    with Session(db) as session:
        audits = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "configure")
        ).all()
        assert len(audits) == 2 and all(a.actor == "operator" for a in audits)
        assert (
            json.loads(audits[-1].detail_json)["active_tasks_on_previous_policy"] == 1
        )


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


def test_start_guard_publishes_only_its_live_transaction(db, policy):
    task = admitted(policy)
    assert controls.active_start_session() is None
    with controls.start_guard(task) as decision:
        active = controls.active_start_session()
        assert decision["ok"]
        assert isinstance(active, Session)
        assert active.in_transaction()
    assert controls.active_start_session() is None


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
        ("max_planner_turns", 0),
        ("max_planner_turns", 101),
        ("max_planner_turns", True),
        ("max_planner_turns", 1.0),
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
        ("model_pools", {}),
        ("model_pools", {"review": ["opus"]}),
        ("model_pools", {"worker": ["luna", "unlisted"]}),
        ("model_pools", {"worker": ["opus", "luna"]}),
        ("model_pools", {"conductor": ["opus", "opus"]}),
        ("model_pools", {"worker": []}),
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
    policy["deploy_on_merge"] = True
    with pytest.raises(ValueError):
        controls.set_control("configure", "operator", policy=policy)
    del policy["deploy_on_merge"]
    task = admitted(policy)
    assert grant(task, model="issue-selected-model")["reason"] == "model_not_allowed"


def test_reviewer_model_defaults_to_conductor_and_is_independent(db, policy):
    normalized = controls.validate_policy(policy)
    assert normalized["reviewer_model"] == "opus"
    policy["reviewer_model"] = "luna"
    assert controls.validate_policy(policy)["reviewer_model"] == "luna"
    policy["reviewer_model"] = "disabled"
    with pytest.raises(ValueError, match="reviewer model"):
        controls.validate_policy(policy)


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


def test_hour_scale_turn_timeout_is_admitted_within_task_bound(db, policy):
    policy["turn_timeout_seconds"] = 43200
    policy["task_timeout_seconds"] = 86400
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.status()["policy"]["turn_timeout_seconds"] == 43200
    assert controls.status()["policy"]["task_timeout_seconds"] == 86400


def test_hour_scale_turn_timeout_still_bounded_by_task_timeout(db, policy):
    policy["task_timeout_seconds"] = 86400
    policy["turn_timeout_seconds"] = 43201
    with pytest.raises(ValueError):
        controls.set_control("configure", "operator", policy=policy)
    policy["turn_timeout_seconds"] = 43200
    policy["task_timeout_seconds"] = 43199
    with pytest.raises(ValueError):
        controls.set_control("configure", "operator", policy=policy)


def test_escalation_requires_exact_uncertain_reconciliation_and_is_not_task_outcome(
    db, policy
):
    task = admitted(policy)
    assert grant(task)["ok"]
    assert controls.record_start_outcome(
        task, "one", "uncertain", "worker", session_id=7
    )["ok"]
    assert (
        controls.record_start_outcome(task, "one", "escalated", "worker", session_id=7)[
            "reason"
        ]
        == "reconciliation_required"
    )
    assert (
        controls.record_start_outcome(
            task, "one", "escalated", "worker", session_id=8, reconciled=True
        )["reason"]
        == "conflicting_session"
    )
    assert (
        controls.record_start_outcome(
            task, "one", "escalated", "worker", session_id=7, reconciled=True
        )["start"]["status"]
        == "failed"
    )
    # The node-run status settles the START as failed and never the task. The
    # receipt state of the same name is a different thing entirely, a task
    # whose planner asked a person for a decision, and it is reached only by
    # an explicit settlement the reconciler makes.
    assert controls.task_snapshot(task)["state"] == "admitted"
    assert controls.finish_task(task, "escalated", "scheduler")["ok"]
    assert controls.task_snapshot(task)["state"] == "escalated"
    with pytest.raises(ValueError, match="invalid task outcome"):
        controls.finish_task(task, "reserved", "scheduler")


def test_model_pools_are_normalised_and_stored_in_policy(db, policy):
    policy["model_pools"] = {"conductor": ["opus", "luna"]}
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    stored = controls.status()["policy"]
    assert stored["model_pools"] == {"conductor": ["opus", "luna"]}
    assert stored["conductor_model"] == "opus"


def test_policy_without_model_pools_stays_unchanged(db, policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert "model_pools" not in controls.status()["policy"]


def test_policy_without_intake_gains_disabled_defaults(policy):
    normalized = controls.validate_policy(policy)
    assert normalized["intake"] == {
        **controls.DEFAULT_INTAKE,
        "exclude_labels": ["needs-human", "security-finding", "wontfix"],
    }
    assert normalized["intake"] is not controls.DEFAULT_INTAKE


@pytest.mark.parametrize("task_class", controls.TASK_CLASSES)
def test_validate_task_class_accepts_vocabulary(task_class):
    assert controls.validate_task_class(task_class) == task_class


@pytest.mark.parametrize("task_class", ["unknown", None, 7])
def test_validate_task_class_rejects_values_outside_vocabulary(task_class):
    with pytest.raises(ValueError, match="invalid task_class"):
        controls.validate_task_class(task_class)


def test_receipt_task_class_defaults_null_and_preserves_stored_value():
    assert controls.receipt_task_class(FactoryReceipt(task_class=None)) == "bug-fix"
    assert controls.receipt_task_class(FactoryReceipt(task_class="docs")) == "docs"


def test_is_advisory_matches_exactly_the_advisory_classes():
    assert {
        task_class
        for task_class in controls.TASK_CLASSES
        if controls.is_advisory(task_class)
    } == set(controls.ADVISORY_CLASSES)


@pytest.mark.parametrize("key", ["enabled", "refine_enabled"])
@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_intake_flags_require_bools(policy, key, value):
    policy["intake"] = {key: value}
    with pytest.raises(ValueError, match=key):
        controls.validate_policy(policy)


@pytest.mark.parametrize(
    "key,value,accepted",
    [
        ("max_per_day", 0, False),
        ("max_per_day", 1, True),
        ("max_per_day", 10000, True),
        ("max_per_day", 10001, False),
        ("cooldown_hours", 0, False),
        ("cooldown_hours", 1, True),
        ("cooldown_hours", 168, True),
        ("cooldown_hours", 169, False),
    ],
)
def test_intake_numeric_bounds(policy, key, value, accepted):
    policy["intake"] = {key: value}
    if accepted:
        assert controls.validate_policy(policy)["intake"][key] == value
    else:
        with pytest.raises(ValueError, match=key):
            controls.validate_policy(policy)


def test_intake_labels_are_bounded_deduplicated_sorted_and_disjoint(policy):
    policy["intake"] = {"labels": ["z", "a", "z"], "exclude_labels": []}
    assert controls.validate_policy(policy)["intake"]["labels"] == ["a", "z"]
    policy["intake"] = {"labels": [1]}
    with pytest.raises(ValueError, match="label"):
        controls.validate_policy(policy)
    policy["intake"] = {"labels": ["same"], "exclude_labels": ["same"]}
    with pytest.raises(ValueError, match="overlap"):
        controls.validate_policy(policy)


def test_intake_unknown_key_is_refused(policy):
    policy["intake"] = {"surprise": True}
    with pytest.raises(ValueError, match="invalid intake"):
        controls.validate_policy(policy)


def test_status_defaults_intake_for_empty_control_policy(db):
    assert controls.status()["intake"] == {
        "policy": {
            **controls.DEFAULT_INTAKE,
            "exclude_labels": ["needs-human", "security-finding", "wontfix"],
        },
        "admitted_today": 0,
        "max_per_day": 5,
        "last_admitted": None,
        "last_idle": None,
    }


def node_key(task_id, node, attempt=1):
    return f"factory-node:{task_id}:{node}:{attempt}"


def test_planner_starts_cost_money_but_do_not_consume_the_work_turn_cap(db, policy):
    task = admitted(policy)
    for index, node in enumerate(("conductor_1", "implement_fix", "conductor_2")):
        key = node_key(task, node)
        assert grant(task, key, cost=1.0)["ok"]
        assert controls.record_start_outcome(
            task, key, "succeeded", "worker", cost_usd=0.5, session_id=index + 1
        )["ok"]
    snapshot = controls.task_snapshot(task)
    assert snapshot["turns_used"] == 1
    assert snapshot["planner_turns_used"] == 2
    assert snapshot["committed_cost_usd"] == 1.5
    assert snapshot["limits"]["turn_limit_reached"] is False


def test_work_starts_alone_reach_the_turn_limit_and_fence_admission(db, policy):
    policy["max_turns_per_task"] = 2
    policy["max_planner_turns"] = 3
    task = admitted(policy)
    for index, node in enumerate(
        ("conductor_1", "implement_one", "conductor_2", "implement_two")
    ):
        key = node_key(task, node)
        assert grant(task, key, cost=1.0)["ok"]
        assert controls.record_start_outcome(
            task, key, "succeeded", "worker", cost_usd=0.1, session_id=index + 1
        )["ok"]
    snapshot = controls.task_snapshot(task)
    assert snapshot["turns_used"] == 2 and snapshot["planner_turns_used"] == 2
    assert snapshot["limits"]["turn_limit_reached"] is True
    assert grant(task, node_key(task, "implement_three"), cost=1.0) == {
        "ok": False,
        "reason": "turn_limit",
    }
    # A further planning round is still admissible, and still costs money.
    assert grant(task, node_key(task, "conductor_3"), cost=1.0)["ok"]


@pytest.mark.parametrize(
    "key",
    [
        "one",
        "factory-node:t-1:conductor_1",
        "conductor_1",
        "factory-node:t-1:conductor_1:x",
    ],
)
def test_unparsable_start_keys_count_as_work_turns(db, policy, key):
    task = admitted(policy)
    assert grant(task, key)["ok"]
    snapshot = controls.task_snapshot(task)
    assert snapshot["turns_used"] == 1 and snapshot["planner_turns_used"] == 0


def test_planner_cap_falls_back_for_a_policy_pinned_before_the_field():
    assert controls.planner_turn_cap({"max_turns_per_task": 7}) == 7
    assert (
        controls.planner_turn_cap({"max_turns_per_task": 7, "max_planner_turns": 2})
        == 2
    )


def test_absent_planner_cap_inherits_the_work_cap(db, policy):
    assert "max_planner_turns" not in policy
    policy["max_turns_per_task"] = 1
    task = admitted(policy)
    assert controls.task_snapshot(task)["policy"]["max_planner_turns"] == 1
    key = node_key(task, "conductor_1")
    assert grant(task, key, cost=0.5)["ok"]
    assert controls.record_start_outcome(
        task, key, "succeeded", "worker", cost_usd=0.1, session_id=1
    )["ok"]
    assert grant(task, node_key(task, "conductor_2"), cost=0.5) == {
        "ok": False,
        "reason": "planner_turn_limit",
    }


def test_planner_rounds_meet_their_own_cap_while_work_still_admits(db, policy):
    policy["max_turns_per_task"] = 3
    policy["max_planner_turns"] = 1
    task = admitted(policy)
    key = node_key(task, "conductor_1")
    assert grant(task, key, cost=0.5)["ok"]
    assert controls.record_start_outcome(
        task, key, "succeeded", "worker", cost_usd=0.1, session_id=1
    )["ok"]
    limits = controls.task_snapshot(task)["limits"]
    assert limits["planner_turn_limit_reached"] is True
    assert limits["turn_limit_reached"] is False
    assert grant(task, node_key(task, "conductor_2"), cost=0.5) == {
        "ok": False,
        "reason": "planner_turn_limit",
    }
    # The planning cap does not fence delivery.
    assert grant(task, node_key(task, "implement_fix"), cost=0.5)["ok"]


def test_planner_starts_answer_to_the_task_budget(db, policy):
    # A refused decision can mint a planner every tick, so the budget has to
    # fence deliberation even while the planning cap has room left.
    policy["max_planner_turns"] = 50
    task = admitted(policy)
    for index, node in enumerate(("conductor_1", "conductor_2")):
        key = node_key(task, node)
        assert grant(task, key, cost=2.0)["ok"]
        assert controls.record_start_outcome(
            task, key, "succeeded", "worker", cost_usd=2.0, session_id=index + 1
        )["ok"]
    snapshot = controls.task_snapshot(task)
    assert snapshot["committed_cost_usd"] == 4.0
    assert snapshot["planner_turns_used"] == 2
    assert snapshot["limits"]["planner_turn_limit_reached"] is False
    assert grant(task, node_key(task, "conductor_3"), cost=2.0) == {
        "ok": False,
        "reason": "budget_limit",
    }


def test_review_rounds_default_and_bounds_are_server_owned(db, policy):
    assert "max_review_rounds" not in policy
    # An operator policy posted before this field existed gains the default,
    # so the engine bound applies without a re-post.
    assert (
        controls.validate_policy(policy)["max_review_rounds"]
        == controls.DEFAULT_MAX_REVIEW_ROUNDS
    )
    policy["max_review_rounds"] = 0
    assert controls.validate_policy(policy)["max_review_rounds"] == 0
    policy["max_review_rounds"] = 10
    assert controls.validate_policy(policy)["max_review_rounds"] == 10
    for invalid in (11, -1, 2.0, True, "2", None):
        policy["max_review_rounds"] = invalid
        with pytest.raises(ValueError, match="max_review_rounds"):
            controls.validate_policy(policy)


def test_review_rounds_are_pinned_with_the_rest_of_the_policy(db, policy):
    policy["max_review_rounds"] = 1
    task = admitted(policy)
    assert controls.task_snapshot(task)["policy"]["max_review_rounds"] == 1


def graph_node(node_key, *, deps=(), max_attempts=2, max_cost_usd=2.0):
    return {
        "node_key": node_key,
        "deps": list(deps),
        "max_attempts": max_attempts,
        "max_cost_usd": max_cost_usd,
    }


def graph_run(node_key, *, status="succeeded", cost=0.5):
    return {"node_key": node_key, "status": status, "accounted_cost_usd": cost}


def test_a_three_node_plan_sizes_its_own_task(policy):
    plan = [
        graph_node("conductor_1", max_cost_usd=2.0),
        graph_node("investigate_scope"),
        graph_node("implement_fix", deps=["investigate_scope"]),
        graph_node("review_check", deps=["implement_fix"]),
    ]
    allowance = controls.allowance_from_graph(
        plan, [], policy, review_rounds_remaining=2, graph_revision=4
    )
    # Three work nodes of two attempts each, plus the next review round only, a
    # correction and a re-review at one attempt each, which is what the engine
    # will really insert. The round behind it is not reserved until this one is
    # spent. The planner node costs money but never a work turn.
    assert allowance["turns"] == 3 * 2 + 2
    assert allowance["review_rounds_reserved"] == 1
    assert allowance["usd"] == 4 * 2.0 + policy["turn_budget_usd"] + 8.0
    assert allowance["graph_revision"] == 4 and allowance["derived"] is True


def test_a_nine_turn_envelope_holds_a_three_node_plan_and_its_next_round(policy):
    policy = controls.validate_policy(
        {**policy, "max_turns_per_task": 9, "task_budget_usd": 30.0}
    )
    plan = [
        graph_node("conductor_1", max_cost_usd=2.0),
        graph_node("investigate_scope"),
        graph_node("implement_fix", deps=["investigate_scope"]),
        graph_node("review_check", deps=["implement_fix"]),
    ]
    allowance = controls.allowance_from_graph(
        plan, [], policy, review_rounds_remaining=2, graph_revision=4
    )
    # The defect this arithmetic fixes: eight turns of eager reserve on top of
    # six of plan needed fourteen, so the envelope refused the plan that sized
    # it. Three nodes and the next round fit inside nine.
    assert allowance["turns"] == 8
    assert controls.envelope_excess(allowance, policy) is None


def test_only_the_next_review_round_is_ever_reserved(policy):
    plan = [
        graph_node("implement_fix"),
        graph_node("review_fix", deps=["implement_fix"]),
    ]
    for remaining in (1, 2, 5):
        allowance = controls.allowance_from_graph(
            plan, [], policy, review_rounds_remaining=remaining, graph_revision=1
        )
        assert allowance["review_rounds_reserved"] == 1
        assert allowance["turns"] == 2 * 2 + 2
    spent = controls.allowance_from_graph(
        plan, [], policy, review_rounds_remaining=0, graph_revision=1
    )
    assert spent["review_rounds_reserved"] == 0 and spent["turns"] == 2 * 2


def test_a_plan_with_no_review_node_reserves_no_review_round(policy):
    allowance = controls.allowance_from_graph(
        [graph_node("implement_fix")],
        [],
        policy,
        review_rounds_remaining=2,
        graph_revision=1,
    )
    assert allowance["turns"] == 2 and allowance["review_rounds_reserved"] == 0


def test_a_fan_in_is_reserved_as_the_node_the_engine_will_insert(policy):
    plan = [graph_node("implement_alpha"), graph_node("implement_beta")]
    allowance = controls.allowance_from_graph(
        plan,
        [],
        policy,
        review_rounds_remaining=0,
        fan_ins_remaining=1,
        graph_revision=2,
    )
    assert allowance["fan_ins_reserved"] == 1
    assert allowance["turns"] == 2 * 2 + policy["max_attempts"]
    assert allowance["usd"] == 2 * 2.0 + policy["turn_budget_usd"]


def test_a_review_round_grows_the_allowance_by_exactly_one_round(policy):
    plan = [
        graph_node("implement_fix"),
        graph_node("review_fix", deps=["implement_fix"]),
    ]
    before = controls.allowance_from_graph(
        plan, [], policy, review_rounds_remaining=2, graph_revision=2
    )
    opened = controls.allowance_from_graph(
        plan
        + [
            graph_node("correct_1", deps=["review_fix"], max_attempts=1),
            graph_node("review_1", deps=["correct_1"], max_attempts=1),
        ],
        [],
        policy,
        review_rounds_remaining=1,
        graph_revision=4,
    )
    # The reserve became two real nodes and the round behind it took its place,
    # so the task grew by one round rather than by every round at once.
    assert opened["turns"] == before["turns"] + 2
    last = controls.allowance_from_graph(
        plan
        + [
            graph_node("correct_1", deps=["review_fix"], max_attempts=1),
            graph_node("review_1", deps=["correct_1"], max_attempts=1),
            graph_node("correct_2", deps=["review_1"], max_attempts=1),
            graph_node("review_2", deps=["correct_2"], max_attempts=1),
        ],
        [],
        policy,
        review_rounds_remaining=0,
        graph_revision=6,
    )
    # The last round's two nodes take the place of the two turns reserved for
    # it and nothing is reserved behind it, so opening it moves nothing. One
    # round of headroom is what the task ever carries unspent.
    assert last["turns"] == opened["turns"]


def test_a_discarded_node_never_refunds_a_consumed_turn(policy):
    runs = [
        graph_run("implement_fix", status="failed"),
        graph_run("implement_fix", status="failed"),
    ]
    live = controls.allowance_from_graph(
        [graph_node("implement_fix"), graph_node("implement_other")],
        runs,
        policy,
        review_rounds_remaining=0,
        graph_revision=2,
    )
    discarded = controls.allowance_from_graph(
        [graph_node("implement_other")],
        runs,
        policy,
        review_rounds_remaining=0,
        graph_revision=3,
    )
    # Both attempts stay charged as history. Discarding only drops the two
    # remaining slots of the other node's exhausted sibling, which were zero.
    assert live["turns"] == discarded["turns"] == 2 + 2
    assert discarded["usd"] == 1.0 + 2.0


def test_history_is_counted_once_when_a_node_still_has_an_attempt_left(policy):
    allowance = controls.allowance_from_graph(
        [graph_node("implement_fix")],
        [graph_run("implement_fix", status="failed")],
        policy,
        review_rounds_remaining=0,
        graph_revision=1,
    )
    assert allowance["turns"] == 2


def test_the_envelope_names_both_excesses(policy):
    policy = controls.validate_policy(policy)
    fits = {"turns": 3, "usd": 5.0}
    assert controls.envelope_excess(fits, policy) is None
    excess = controls.envelope_excess({"turns": 9, "usd": 11.0}, policy)
    assert excess == {
        "turns": {"needed": 9, "allowed": 3},
        "usd": {"needed": 11.0, "allowed": 5.0},
    }


def test_a_refusal_names_the_turns_and_dollars_that_would_still_fit(policy):
    policy = controls.validate_policy(policy)
    excess = controls.envelope_excess(
        {"turns": 9, "usd": 1.0}, policy, accounted={"turns": 1, "usd": 1.5}
    )
    # Three turns allowed against one already accounted leaves two, and five
    # dollars against one and a half leaves three and a half, either of which
    # the planner can size its next edit against.
    assert excess["spare_turns"] == 2
    assert excess["spare_usd"] == 3.5
    at_the_wall = controls.envelope_excess(
        {"turns": 9, "usd": 1.0}, policy, accounted={"turns": 4, "usd": 7.0}
    )
    assert at_the_wall["spare_turns"] == 0 and at_the_wall["spare_usd"] == 0.0


def test_a_dollar_only_refusal_still_names_both_spare_figures(policy):
    policy = controls.validate_policy(policy)
    # Turns fit and money does not, which is the case that reported a spare
    # turn count and left the binding figure unnamed.
    excess = controls.envelope_excess(
        {"turns": 2, "usd": 11.0}, policy, accounted={"turns": 1, "usd": 4.0}
    )
    assert excess["turns"]["needed"] <= excess["turns"]["allowed"]
    assert excess["spare_turns"] == 2 and excess["spare_usd"] == 1.0


def test_a_reserve_can_be_imposed_on_a_graph_that_has_no_review_node(policy):
    """What a refused edit implies, not what the live graph holds on its own."""
    live = [graph_node("implement_fix")]
    assert (
        controls.allowance_from_graph(
            live, [], policy, review_rounds_remaining=2, graph_revision=1
        )["turns"]
        == 2
    )
    implied = controls.allowance_from_graph(
        live, [], policy, review_rounds_remaining=2, graph_revision=1, reviewable=True
    )
    assert implied["turns"] == 2 + 2 and implied["review_rounds_reserved"] == 1


def test_an_old_policy_reads_its_fixed_cap_as_the_envelope(policy):
    resolved = controls.validate_policy(policy)
    assert "max_task_turns_hard" not in policy
    assert resolved["max_task_turns_hard"] == 3
    assert controls.task_turn_ceiling(resolved) == 3
    assert controls.parallel_limit(resolved) == 1
    assert controls.planner_turn_cap(resolved) == 3


def test_an_envelope_policy_needs_no_fixed_cap(policy):
    policy.pop("max_turns_per_task")
    policy["max_task_turns_hard"] = 40
    resolved = controls.validate_policy(policy)
    assert "max_turns_per_task" not in resolved
    assert controls.task_turn_ceiling(resolved) == 40
    assert controls.planner_turn_cap(resolved) == 40


def test_a_policy_with_neither_turn_bound_is_refused(policy):
    policy.pop("max_turns_per_task")
    with pytest.raises(ValueError, match="max_task_turns_hard"):
        controls.validate_policy(policy)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_task_turns_hard", 0),
        ("max_task_turns_hard", 501),
        ("max_task_turns_hard", 1.0),
        ("max_task_turns_hard", True),
        ("max_parallel_nodes", 0),
        ("max_parallel_nodes", 9),
        ("max_parallel_nodes", True),
    ],
)
def test_envelope_fields_are_bounded(policy, key, value):
    policy[key] = value
    with pytest.raises(ValueError):
        controls.validate_policy(policy)


def test_work_starts_meet_the_derived_allowance_not_the_envelope(db, policy):
    policy["max_task_turns_hard"] = 50
    task = admitted(policy)
    # A plan that sizes the task to one work turn binds admission at one, even
    # though the envelope would fund fifty. The graph tables live in the
    # conductor suite, so the derived value is written here directly.
    with Session(db) as session:
        row = session.exec(select(FactoryReceipt)).first()
        row.allowance_json = json.dumps(
            {"turns": 1, "usd": 5.0, "graph_revision": 2, "review_rounds_reserved": 0}
        )
        session.add(row)
        session.commit()
    assert controls.task_snapshot(task)["allowance"]["turns"] == 1
    key = node_key(task, "implement_one")
    assert grant(task, key, cost=1.0)["ok"]
    assert controls.record_start_outcome(
        task, key, "succeeded", "worker", cost_usd=0.1, session_id=1
    )["ok"]
    assert controls.task_snapshot(task)["limits"]["turn_limit_reached"] is True
    assert grant(task, node_key(task, "implement_two"), cost=1.0) == {
        "ok": False,
        "reason": "turn_limit",
    }


def test_a_task_with_no_accepted_plan_falls_back_to_the_envelope(db, policy):
    task = admitted(policy)
    snapshot = controls.task_snapshot(task)
    assert snapshot["allowance"] == {
        "turns": 3,
        "usd": 5.0,
        "graph_revision": None,
        "review_rounds_reserved": 0,
        "fan_ins_reserved": 0,
        "derived": False,
    }


def test_the_close_flags_default_off_and_bound_the_cap():
    from factory.orchestration.factory_controls import DEFAULT_INTAKE, intake_policy

    block = intake_policy({})
    assert block["close_enabled"] is False
    assert block["max_closes_per_day"] == DEFAULT_INTAKE["max_closes_per_day"] == 3
    assert intake_policy({"intake": {"close_enabled": True}})["close_enabled"] is True
    for bad in (0, 51, "3", True, None):
        with pytest.raises(ValueError):
            intake_policy({"intake": {"max_closes_per_day": bad}})
    with pytest.raises(ValueError):
        intake_policy({"intake": {"close_enabled": "yes"}})


def test_an_integer_max_tasks_reads_as_the_delivery_lane(policy):
    policy["max_tasks"] = 3
    validated = controls.validate_policy(policy)
    assert validated["max_tasks"] == {"delivery": 3, "advisory": 0}


def test_per_lane_max_tasks_round_trips(policy):
    policy["max_tasks"] = {"delivery": 2, "advisory": 5}
    validated = controls.validate_policy(policy)
    assert validated["max_tasks"] == {"delivery": 2, "advisory": 5}


def test_a_lane_map_defaults_the_lane_it_omits(policy):
    policy["max_tasks"] = {"advisory": 4}
    assert controls.validate_policy(policy)["max_tasks"] == {
        "delivery": 1,
        "advisory": 4,
    }


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"delivery": 0},
        {"delivery": 1, "advisory": -1},
        {"delivery": 1, "review": 1},
        {"delivery": True},
        {"advisory": 1.5},
    ],
)
def test_an_unusable_lane_map_is_refused(policy, value):
    policy["max_tasks"] = value
    with pytest.raises(ValueError):
        controls.validate_policy(policy)


@pytest.mark.parametrize(
    ("task_class", "lane"),
    [
        ("bug-fix", "delivery"),
        ("mechanical-refactor", "delivery"),
        ("docs", "delivery"),
        ("judgment-analysis", "delivery"),
        ("refine", "advisory"),
        ("advisory-diagnosis", "advisory"),
        ("advisory-triage", "advisory"),
    ],
)
def test_lane_follows_class(task_class, lane):
    assert controls.lane_for(task_class) == lane


def test_the_chart_ceiling_bounds_the_sum_of_both_lanes(monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "3")
    assert controls.lane_limits({"max_tasks": {"delivery": 2, "advisory": 4}}) == {
        "delivery": 2,
        "advisory": 1,
    }


def test_a_ceiling_of_one_still_leaves_delivery_a_slot(monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "1")
    assert controls.lane_limits({"max_tasks": {"delivery": 5, "advisory": 5}}) == {
        "delivery": 1,
        "advisory": 0,
    }


def test_lane_usage_counts_in_flight_and_waiting_receipts(monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    receipts = [
        {"state": "admitted", "task_class": "bug-fix", "generation": 0},
        {"state": "uncertain", "task_class": "docs", "generation": 0},
        {"state": "queued", "task_class": "judgment-analysis", "generation": 0},
        {"state": "admitted", "task_class": "refine", "generation": 0},
        {"state": "succeeded", "task_class": "refine", "generation": 0},
        # A receipt written before classes existed reads as delivery.
        {"state": "queued", "task_class": None, "generation": 0},
    ]
    usage = controls.lane_usage({"max_tasks": {"delivery": 2, "advisory": 2}}, receipts)
    assert usage == {
        "delivery": {"limit": 2, "active": 2, "queued": 2},
        "advisory": {"limit": 2, "active": 1, "queued": 0},
    }


def test_status_reports_the_lane_view(db, policy, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    policy["max_tasks"] = {"delivery": 2, "advisory": 1}
    admitted(policy)
    lanes = controls.status()["lanes"]
    assert lanes["delivery"] == {"limit": 2, "active": 1, "queued": 0}
    assert lanes["advisory"] == {"limit": 1, "active": 0, "queued": 0}


def test_class_pools_need_no_head_model(policy):
    policy["allowed_models"] = ["opus", "luna", "sonnet"]
    policy["model_pools"] = {
        "implement": ["sonnet", "luna"],
        "refine": ["luna", "sonnet"],
    }
    validated = controls.validate_policy(policy)
    assert validated["model_pools"]["implement"] == ["sonnet", "luna"]
    assert validated["model_pools"]["refine"] == ["luna", "sonnet"]


def test_a_role_pool_still_needs_its_own_model_at_the_head(policy):
    policy["model_pools"] = {"worker": ["opus", "luna"]}
    with pytest.raises(ValueError):
        controls.validate_policy(policy)


@pytest.mark.parametrize(
    "pools",
    [
        {"implement": ["astra"]},
        {"refine": ["spark"]},
        {"implement": ["luna", "luna"]},
        {"planner": ["luna"]},
        {"implement": []},
    ],
)
def test_an_unusable_class_pool_is_refused(policy, pools):
    policy["model_pools"] = pools
    with pytest.raises(ValueError):
        controls.validate_policy(policy)


def test_a_policy_without_a_quota_guard_gains_the_defaults(policy):
    validated = controls.validate_policy(policy)
    assert validated["quota_guard"] == {
        "claude_7d_pause_percent": 85,
        "claude_7d_resume_percent": 75,
        "claude_7d_imminent_reset_minutes": 120,
    }


def test_the_quota_guard_thresholds_are_operator_owned(policy):
    policy["quota_guard"] = {
        "claude_7d_pause_percent": 95,
        "claude_7d_resume_percent": 60,
    }
    assert controls.validate_policy(policy)["quota_guard"] == {
        "claude_7d_pause_percent": 95,
        "claude_7d_resume_percent": 60,
        "claude_7d_imminent_reset_minutes": 120,
    }


def test_the_imminent_reset_horizon_is_operator_owned(policy):
    policy["quota_guard"] = {"claude_7d_imminent_reset_minutes": 240}
    assert controls.validate_policy(policy)["quota_guard"] == {
        "claude_7d_pause_percent": 85,
        "claude_7d_resume_percent": 75,
        "claude_7d_imminent_reset_minutes": 240,
    }


def test_zero_disables_the_imminent_reset_horizon(policy):
    policy["quota_guard"] = {"claude_7d_imminent_reset_minutes": 0}
    assert (
        controls.validate_policy(policy)["quota_guard"][
            "claude_7d_imminent_reset_minutes"
        ]
        == 0
    )


def test_a_policy_with_only_the_existing_quota_keys_still_validates(policy):
    policy["quota_guard"] = {
        "claude_7d_pause_percent": 90,
        "claude_7d_resume_percent": 80,
    }
    assert controls.validate_policy(policy)["quota_guard"] == {
        "claude_7d_pause_percent": 90,
        "claude_7d_resume_percent": 80,
        "claude_7d_imminent_reset_minutes": 120,
    }


def test_one_named_threshold_defaults_the_other(policy):
    policy["quota_guard"] = {"claude_7d_resume_percent": 50}
    assert controls.validate_policy(policy)["quota_guard"] == {
        "claude_7d_pause_percent": 85,
        "claude_7d_resume_percent": 50,
        "claude_7d_imminent_reset_minutes": 120,
    }


def test_lowering_the_pause_below_the_default_resume_is_refused(policy):
    """Naming one threshold cannot silently invert the pair."""
    policy["quota_guard"] = {"claude_7d_pause_percent": 60}
    with pytest.raises(ValueError):
        controls.validate_policy(policy)


@pytest.mark.parametrize(
    "block",
    [
        {"claude_7d_pause_percent": 70, "claude_7d_resume_percent": 70},
        {"claude_7d_pause_percent": 70, "claude_7d_resume_percent": 80},
        {"claude_7d_pause_percent": 0},
        {"claude_7d_pause_percent": 101},
        {"claude_7d_pause_percent": 85.5},
        {"claude_7d_imminent_reset_minutes": -1},
        {"claude_7d_imminent_reset_minutes": 10081},
        {"unknown": 1},
        [],
    ],
)
def test_an_unusable_quota_guard_is_refused(policy, block):
    policy["quota_guard"] = block
    with pytest.raises(ValueError):
        controls.validate_policy(policy)


def test_status_carries_the_review_routing_view(db, policy):
    """Read from the ledger, never from the broker: the board must not wait."""
    admitted(policy)
    routing = controls.status()["review_routing"]
    assert routing["action"] is None and routing["window_high"] is False
    assert routing["pause_percent"] == 85 and routing["resume_percent"] == 75
    assert routing["model"] is None and routing["last_action"] is None
    with Session(db) as session:
        controls._audit(
            session,
            "factory:quota-guard",
            "reviewer_fallback",
            window_high=True,
            used_percent=91.0,
            model="astra",
        )
        session.commit()
    routing = controls.status()["review_routing"]
    assert routing["action"] == "reviewer_fallback" and routing["model"] == "astra"
    assert routing["window_high"] is True and routing["used_percent"] == 91.0
    assert routing["last_action"] == "reviewer_fallback"


def test_delivery_evidence_records_the_approving_reviewer(db, policy):
    task = admitted(policy)
    evidence = {
        "pr_url": "https://github.com/owner/repo/pull/3",
        "head_sha": "a" * 40,
        "review_session_id": 11,
        "reviewer_model": "astra",
        "state": "ready_for_review",
    }
    assert controls.finish_task(task, "succeeded", "scheduler", evidence=evidence)["ok"]
    assert controls.task_snapshot(task)["evidence"]["reviewer_model"] == "astra"


def test_unsupported_evidence_keys_are_still_refused(db, policy):
    task = admitted(policy)
    with pytest.raises(ValueError, match="unsupported task evidence"):
        controls.finish_task(
            task, "succeeded", "scheduler", evidence={"merged_by": "nobody"}
        )


def test_lane_usage_counts_old_active_tasks_but_not_old_queues(monkeypatch):
    """Reconfiguration leaves old tasks running while their queued peers are inert."""
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    receipts = [
        {"state": "admitted", "task_class": "bug-fix", "generation": 2},
        {"state": "queued", "task_class": "refine", "generation": 2},
        {"state": "admitted", "task_class": "bug-fix", "generation": 1},
        {"state": "queued", "task_class": "refine", "generation": 0},
    ]
    usage = controls.lane_usage(
        {"generation": 2, "max_tasks": {"delivery": 2, "advisory": 2}}, receipts
    )
    assert usage == {
        "delivery": {"limit": 2, "active": 2, "queued": 0},
        "advisory": {"limit": 2, "active": 0, "queued": 1},
    }


def test_auto_merge_defaults_off_and_only_true_enables_landing(policy):
    """Landing writes to the repository, so nothing but True opts in."""
    assert controls.validate_policy(policy)["auto_merge"] is False
    assert controls.auto_merge_enabled(controls.validate_policy(policy)) is False
    armed = controls.validate_policy({**policy, "auto_merge": True})
    assert armed["auto_merge"] is True
    assert controls.auto_merge_enabled(armed) is True
    # A policy stored before the flag existed carries no key at all, and a
    # truthy non-boolean is not an opt-in.
    assert controls.auto_merge_enabled({}) is False
    assert controls.auto_merge_enabled({"auto_merge": "true"}) is False


@pytest.mark.parametrize("value", ["true", 1, None, {}])
def test_auto_merge_refuses_a_non_boolean(policy, value):
    with pytest.raises(ValueError, match="auto_merge"):
        controls.validate_policy({**policy, "auto_merge": value})


@pytest.mark.parametrize("generation", [0, 1])
def test_active_reconfiguration_requires_a_new_generation(db, policy, generation):
    task = admitted(policy)
    assert controls.set_control(
        "configure", "operator", policy={**policy, "generation": 1}
    )["ok"]
    before = controls.task_snapshot(task)
    changed = {**policy, "generation": generation, "task_budget_usd": 99}
    result = controls.set_control("configure", "operator", policy=changed)
    assert result["reason"] == "generation_not_advanced"
    assert controls.task_snapshot(task) == before
    assert controls.status()["version"] == 3


def test_identical_configure_retry_does_not_require_a_new_generation(db, policy):
    task = admitted(policy)
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.can_start(task)["ok"]


def test_generation_bump_retires_only_stranded_receipts_atomically(db, policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    escalation = json.dumps(
        {
            "question": "Which scope?",
            "options": [],
            "resolved": None,
        }
    )
    with Session(db) as session:
        rows = {
            "queued": FactoryReceipt(
                repo="owner/repo",
                issue_number=10,
                generation=0,
                title="queued",
                body="body",
                url="https://github.com/owner/repo/issues/10",
                actor="test",
                state="queued",
            ),
            "escalated": FactoryReceipt(
                repo="owner/repo",
                issue_number=11,
                generation=0,
                title="escalated",
                body="body",
                url="https://github.com/owner/repo/issues/11",
                actor="test",
                state="escalated",
                escalation_json=escalation,
            ),
            "active": FactoryReceipt(
                repo="owner/repo",
                issue_number=12,
                generation=0,
                title="active",
                body="body",
                url="https://github.com/owner/repo/issues/12",
                actor="test",
                state="admitted",
            ),
            "terminal": FactoryReceipt(
                repo="owner/repo",
                issue_number=13,
                generation=0,
                title="terminal",
                body="body",
                url="https://github.com/owner/repo/issues/13",
                actor="test",
                state="failed",
            ),
            "current": FactoryReceipt(
                repo="owner/repo",
                issue_number=14,
                generation=1,
                title="current",
                body="body",
                url="https://github.com/owner/repo/issues/14",
                actor="test",
                state="queued",
            ),
        }
        session.add_all(rows.values())
        session.commit()
        ids = {key: row.id for key, row in rows.items()}

    changed = {**policy, "generation": 1, "task_budget_usd": 10}
    assert controls.set_control("configure", "operator", policy=changed)["ok"]

    with Session(db) as session:
        persisted = {
            key: session.get(FactoryReceipt, value) for key, value in ids.items()
        }
        assert persisted["queued"].state == "cancelled"
        assert persisted["escalated"].state == "cancelled"
        assert persisted["active"].state == "admitted"
        assert persisted["terminal"].state == "failed"
        assert persisted["current"].state == "queued"
        resolution = json.loads(persisted["escalated"].escalation_json)["resolved"]
        assert resolution["effect"] == "escape-dismiss"
        assert resolution["effects"] == {
            "dismissed": True,
            "receipt_retired": True,
        }
        assert "from 0 to 1" in resolution["note"]
        retirements = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "generation_stale_receipt_retired"
            )
        ).all()
        assert len(retirements) == 2
    assert controls.status()["policy"]["generation"] == 1


def test_identical_and_rejected_configure_do_not_retire_queues(db, policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    with Session(db) as session:
        queued = FactoryReceipt(
            repo="owner/repo",
            issue_number=10,
            generation=0,
            title="queued",
            body="body",
            url="https://github.com/owner/repo/issues/10",
            actor="test",
        )
        active = FactoryReceipt(
            repo="owner/repo",
            issue_number=11,
            generation=0,
            title="active",
            body="body",
            url="https://github.com/owner/repo/issues/11",
            actor="test",
            state="admitted",
        )
        session.add_all([queued, active])
        session.commit()
        queued_id = queued.id

    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    refused = controls.set_control(
        "configure", "operator", policy={**policy, "task_budget_usd": 99}
    )
    assert refused["reason"] == "generation_not_advanced"
    with Session(db) as session:
        assert session.get(FactoryReceipt, queued_id).state == "queued"
        assert not session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "generation_stale_receipt_retired"
            )
        ).all()


def test_multiple_generation_bumps_retire_each_old_queue_once(db, policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    receipt_ids = []
    for generation in (0, 1):
        with Session(db) as session:
            row = FactoryReceipt(
                repo="owner/repo",
                issue_number=10 + generation,
                generation=generation,
                title=f"generation {generation}",
                body="body",
                url=f"https://github.com/owner/repo/issues/{10 + generation}",
                actor="test",
            )
            session.add(row)
            session.commit()
            receipt_ids.append(row.id)
        assert controls.set_control(
            "configure",
            "operator",
            policy={**policy, "generation": generation + 1},
        )["ok"]

    with Session(db) as session:
        states = [session.get(FactoryReceipt, row_id).state for row_id in receipt_ids]
        assert states == [
            "cancelled",
            "cancelled",
        ]
        audits = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "generation_stale_receipt_retired"
            )
        ).all()
        assert len(audits) == 2


@pytest.mark.parametrize("historical_value", ["omitted", None])
def test_inert_problem_issues_default_does_not_advance_active_generation(
    db, policy, historical_value
):
    task = admitted(policy)
    with Session(db) as session:
        control = session.get(FactoryControl, "factory")
        stored = json.loads(control.policy_json)
        if historical_value == "omitted":
            stored.pop("problem_issues")
        else:
            stored["problem_issues"] = historical_value
        control.policy_json = json.dumps(stored)
        session.add(control)
        session.commit()

    result = controls.set_control("configure", "operator", policy=policy)

    assert result["ok"] and result["reason"] is None
    assert controls.can_start(task)["ok"]
    assert controls.status()["policy"]["problem_issues"] == (
        controls.DEFAULT_PROBLEM_ISSUES
    )


def test_configure_preserves_paused_admissions_and_uncertain_accounting(db, policy):
    task = admitted(policy)
    grant(task)
    controls.record_start_outcome(task, "one", "uncertain", "worker", session_id=7)
    controls.set_control("pause_admissions", "operator")
    before = controls.task_snapshot(task)
    result = controls.set_control(
        "configure", "operator", policy={**policy, "generation": 1}
    )
    assert result["ok"] and result["state"] == "paused"
    assert controls.task_snapshot(task) == before
    assert controls.can_start(task)["reason"] == "uncertain_outcome"
    assert admit_next("scheduler")["reason"] == "paused"


def test_new_policy_does_not_change_active_task_model_authority(db, policy):
    task = admitted(policy)
    changed = {
        **policy,
        "generation": 1,
        "allowed_models": ["opus"],
        "worker_model": "opus",
    }
    assert controls.set_control("configure", "operator", policy=changed)["ok"]
    assert grant(task, model="luna")["ok"]


def test_review_recovery_is_opt_in_bounded_and_pinned(db, policy):
    assert controls.validate_policy(policy)["max_review_recovery_rounds"] == 0
    policy["max_review_recovery_rounds"] = 2
    task = admitted(policy)
    assert controls.task_snapshot(task)["policy"]["max_review_recovery_rounds"] == 2
    for invalid in (-1, 3, 2.0, True, "2", None):
        policy["max_review_recovery_rounds"] = invalid
        with pytest.raises(ValueError, match="max_review_recovery_rounds"):
            controls.validate_policy(policy)


@pytest.mark.parametrize(
    "denials",
    [
        0,
        1,
        controls.MAX_CAPACITY_DENIED_ATTEMPTS,
        controls.MAX_CAPACITY_DENIED_ATTEMPTS + 1,
    ],
)
def test_plan_allowance_excuses_only_bounded_capacity_denials(policy, denials):
    plan = [graph_node("implement_fix", max_attempts=1)]
    runs = [
        dict(graph_run("implement_fix", status="failed", cost=0), capacity_denied=True)
        for _ in range(denials)
    ]
    allowance = controls.allowance_from_graph(
        plan, runs, policy, review_rounds_remaining=0, graph_revision=1
    )
    assert allowance["turns"] == max(1, denials - controls.MAX_CAPACITY_DENIED_ATTEMPTS)
    succeeded = controls.allowance_from_graph(
        plan,
        runs + [graph_run("implement_fix")],
        policy,
        review_rounds_remaining=0,
        graph_revision=1,
    )
    assert succeeded["turns"] == 1 + max(
        0, denials - controls.MAX_CAPACITY_DENIED_ATTEMPTS
    )


def test_continuation_admission_excuses_capacity_denial_but_not_real_retry(db, policy):
    task = admitted(policy)
    with controls._locked_session() as (session, _):
        controls._audit(
            session,
            "test",
            "continuation_granted",
            task_id=task,
            node_keys=["correct_1", "review_1"],
            work_turn_ceiling=3,
        )
    first = f"factory-node:{task}:correct_1:1"
    second = f"factory-node:{task}:correct_1:2"
    assert controls.authorize_start(task, first, "test", model="luna", max_cost_usd=1)[
        "ok"
    ]
    assert controls.record_start_outcome(
        task, first, "failed", "test", cost_usd=0, accounting_basis="capacity_denied"
    )["ok"]
    assert controls.authorize_start(task, second, "test", model="luna", max_cost_usd=1)[
        "ok"
    ]
    assert controls.record_start_outcome(
        task, second, "succeeded", "test", cost_usd=0.5
    )["ok"]
    denied = controls.authorize_start(
        task, f"factory-node:{task}:correct_1:3", "test", model="luna", max_cost_usd=1
    )
    assert denied["reason"] == "continuation_scope_exhausted"


def test_objective_budget_keeps_all_readmissions_beyond_display_history(
    db, policy, monkeypatch
):
    from factory.orchestration import factory_funding_limits as funding

    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    task = admitted(policy)
    with Session(db) as session:
        receipt = controls._receipt(session, task)
        for i in range(12):
            old = SwarmTask(
                id=f"old-{i}",
                task_text="same issue",
                conductor_model="opus",
                budget_usd=50,
            )
            session.add(old)
            session.flush()
            session.add(
                FactoryStart(
                    task_id=old.id,
                    start_key=f"old:{i}",
                    actor="factory",
                    model="opus",
                    max_cost_usd=20,
                    cost_usd=16.6,
                    status="succeeded",
                )
            )
            controls._audit(
                session,
                "scheduler",
                "admit_next",
                task_id=old.id,
                receipt_id=receipt.id,
            )
        session.commit()
        total = funding.objective(session, task)
        assert len(total["task_ids"]) == 13
        assert total["committed_cost_usd"] == pytest.approx(199.2)
    denied = grant(task, cost=1)
    assert denied["reason"] == "objective_budget_limit"


def test_objective_unions_work_item_and_legacy_issue_receipts(db):
    from factory.orchestration import factory_funding_limits as funding

    with Session(db) as session:
        item = WorkItem(
            title="item", state="open", source_kind="factory", trust="trusted"
        )
        session.add(item)
        session.flush()
        tasks = [
            SwarmTask(
                id=name,
                task_text=name,
                conductor_model="opus",
                budget_usd=10,
            )
            for name in ("current-work", "moved-work", "legacy-work")
        ]
        session.add_all(tasks)
        session.flush()
        session.add_all(
            [
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=1,
                    title="current",
                    body="",
                    url="https://github.com/owner/repo/issues/1",
                    actor="test",
                    task_id="current-work",
                    work_item_id=item.id,
                ),
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=2,
                    title="moved",
                    body="",
                    url="https://github.com/owner/repo/issues/2",
                    actor="test",
                    task_id="moved-work",
                    work_item_id=item.id,
                ),
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=1,
                    generation=1,
                    title="legacy",
                    body="",
                    url="https://github.com/owner/repo/issues/1",
                    actor="test",
                    task_id="legacy-work",
                ),
            ]
        )
        session.add_all(
            [
                FactoryStart(
                    task_id=task.id,
                    start_key=task.id,
                    actor="test",
                    model="opus",
                    max_cost_usd=10,
                    cost_usd=cost,
                    status="succeeded",
                )
                for task, cost in zip(tasks, (1.0, 2.0, 3.0), strict=True)
            ]
        )
        session.commit()
        total = funding.objective(session, "current-work")
    assert total["task_ids"] == ["current-work", "legacy-work", "moved-work"]
    assert total["committed_cost_usd"] == pytest.approx(6.0)


def test_objective_with_null_work_item_uses_issue_identity_only(db):
    from factory.orchestration import factory_funding_limits as funding

    with Session(db) as session:
        item = WorkItem(
            title="item", state="open", source_kind="factory", trust="trusted"
        )
        session.add(item)
        session.flush()
        tasks = [
            SwarmTask(
                id=name,
                task_text=name,
                conductor_model="opus",
                budget_usd=10,
            )
            for name in ("legacy-current", "same-issue", "same-item")
        ]
        session.add_all(tasks)
        session.flush()
        session.add_all(
            [
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=5,
                    title="current",
                    body="",
                    url="https://github.com/owner/repo/issues/5",
                    actor="test",
                    task_id="legacy-current",
                ),
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=5,
                    generation=1,
                    title="same issue",
                    body="",
                    url="https://github.com/owner/repo/issues/5",
                    actor="test",
                    task_id="same-issue",
                    work_item_id=item.id,
                ),
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=6,
                    title="same item only",
                    body="",
                    url="https://github.com/owner/repo/issues/6",
                    actor="test",
                    task_id="same-item",
                    work_item_id=item.id,
                ),
            ]
        )
        session.add_all(
            [
                FactoryStart(
                    task_id=task.id,
                    start_key=task.id,
                    actor="test",
                    model="opus",
                    max_cost_usd=10,
                    cost_usd=cost,
                    status="succeeded",
                )
                for task, cost in zip(tasks, (1.0, 2.0, 50.0), strict=True)
            ]
        )
        session.commit()
        total = funding.objective(session, "legacy-current")
    assert total["task_ids"] == ["legacy-current", "same-issue"]
    assert total["committed_cost_usd"] == pytest.approx(3.0)


def test_concurrent_objective_reservations_cannot_cross_ceiling(
    db, policy, monkeypatch
):
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    task = admitted(policy)
    with Session(db) as session:
        receipt = controls._receipt(session, task)
        old = SwarmTask(
            id="old", task_text="same objective", conductor_model="opus", budget_usd=200
        )
        session.add(old)
        session.flush()
        session.add(
            FactoryStart(
                task_id="old",
                start_key="old",
                actor="factory",
                model="opus",
                max_cost_usd=199,
                cost_usd=199,
                status="succeeded",
            )
        )
        controls._audit(
            session, "scheduler", "admit_next", task_id="old", receipt_id=receipt.id
        )
        session.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda key: grant(task, key=key, cost=1), ["first", "second"])
        )
    assert sum(result["ok"] for result in results) == 1
    assert any(result.get("reason") == "objective_budget_limit" for result in results)


def test_control_request_replay_never_reapplies_after_a_newer_stop(db, policy):
    task = admitted(policy)
    version = controls.status()["version"]
    first = controls.request_control(
        "pause_task",
        "operator",
        task_id=task,
        request_key="pause-one",
        expected_version=version,
    )
    assert first["ok"]
    stopped = controls.set_control("stop", "operator")
    assert (
        controls.request_control(
            "pause_task",
            "operator",
            task_id=task,
            request_key="pause-one",
            expected_version=version,
        )
        == first
    )
    current = controls.status()
    assert current["state"] == "stopped"
    assert current["version"] == stopped["version"]
    assert current["receipts"][0]["cancellation_requested"]


def test_control_request_rejects_stale_version_and_key_reuse(db, policy):
    admitted(policy)
    version = controls.status()["version"]
    controls.set_control("pause_admissions", "operator")
    rejected = controls.request_control(
        "enable",
        "operator",
        request_key="enable-one",
        expected_version=version,
    )
    assert rejected["reason"] == "control_version_changed"
    assert controls.status()["state"] == "paused"
    conflict = controls.request_control(
        "enable",
        "operator",
        request_key="enable-one",
        expected_version=version + 1,
    )
    assert conflict["reason"] == "conflicting_control_request"
    assert controls.status()["state"] == "paused"


def test_duplicate_concurrent_control_requests_commit_one_transition(db, policy):
    task = admitted(policy)
    grant(task, "spent", cost=1.25)
    before = controls.task_snapshot(task)
    version = controls.status()["version"]

    def pause():
        return controls.request_control(
            "pause_task",
            "operator",
            task_id=task,
            request_key="same-pause",
            expected_version=version,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: pause(), range(2)))
    assert results[0] == results[1]
    assert results[0]["ok"]
    assert controls.status()["version"] == version + 1
    after = controls.task_snapshot(task)
    assert after["committed_cost_usd"] == before["committed_cost_usd"]
    assert after["starts"] == before["starts"]
    with Session(db) as session:
        records = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "control_request")
        ).all()
        assert len(records) == 1
        assert records[0].actor == "operator"
        assert records[0].task_id == task


def test_requested_resume_never_selects_an_escalation_option(db, policy, monkeypatch):
    task = admitted(policy)
    with Session(db) as session:
        row = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task)
        ).one()
        row.state = controls.ESCALATED
        session.add(row)
        session.commit()

    def explode(*args, **kwargs):
        raise AssertionError("task resume must not answer an escalation")

    monkeypatch.setattr(
        "factory.orchestration.factory_decisions.resume_escalated", explode
    )
    result = controls.request_control(
        "resume_task",
        "operator",
        task_id=task,
        request_key="resume",
        expected_version=controls.status()["version"],
    )
    assert result["reason"] == "task_not_active"


@pytest.mark.parametrize(
    "action,task_id", [("stop", "task"), ("pause_task", None), ("configure", None)]
)
def test_control_request_rejects_ambiguous_or_unsupported_actions(db, action, task_id):
    with pytest.raises(ValueError):
        controls.request_control(
            action,
            "operator",
            task_id=task_id,
            request_key="invalid",
            expected_version=0,
        )
    with Session(db) as session:
        assert session.exec(select(FactoryAudit)).all() == []


def test_automerge_delivery_pending_until_exact_trusted_rollout(db, policy):
    policy["auto_merge"] = True
    task = admitted(policy)
    evidence = {
        "pr_url": "https://github.com/owner/repo/pull/9",
        "head_sha": "a" * 40,
        "review_session_id": 7,
        "state": "ready_for_review",
    }
    assert controls.finish_task(task, "succeeded", "scheduler", evidence=evidence) == {
        "ok": True,
        "state": "landing",
    }
    assert controls.task_snapshot(task)["evidence"] == evidence
    with Session(db) as session:
        assert session.get(SwarmTask, task).settled_at is None
        assert session.get(SwarmTask, task).start_state == "landing"
        assert not session.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == task, FactoryAudit.action == "finish_task"
            )
        ).all()
    # A flag change cannot turn deployment evidence into an optional gate.
    policy["auto_merge"] = False
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    for actor, head, pr in [
        ("worker", "a" * 40, 9),
        ("factory:landing", "b" * 40, 9),
        ("factory:landing", "a" * 40, 10),
    ]:
        with Session(db) as session:
            session.add(
                FactoryAudit(
                    actor=actor,
                    action="rollout_verified",
                    task_id=task,
                    detail_json=json.dumps(
                        {"verified": True, "approved_head_sha": head, "pr_number": pr}
                    ),
                )
            )
            session.commit()
        assert controls.finish_task(
            task, "succeeded", "scheduler", evidence=evidence
        ) == {"ok": True, "state": "landing"}
    assert (
        controls.finish_task(
            task, "succeeded", "scheduler", evidence={**evidence, "head_sha": "c" * 40}
        )["reason"]
        == "conflicting_outcome"
    )
    with Session(db) as session:
        session.add(
            FactoryAudit(
                actor="factory:landing",
                action="rollout_verified",
                task_id=task,
                detail_json=json.dumps(
                    {"verified": True, "approved_head_sha": "a" * 40, "pr_number": 9}
                ),
            )
        )
        session.commit()
    assert controls.finish_task(
        task, "succeeded", "factory:landing", evidence=evidence
    ) == {"ok": True, "state": "succeeded"}
    assert controls.finish_task(
        task, "succeeded", "factory:landing", evidence=evidence
    )["ok"]
    with Session(db) as session:
        assert session.get(SwarmTask, task).settled_at is not None
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.task_id == task,
                        FactoryAudit.action == "delivery_ready",
                    )
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.task_id == task,
                        FactoryAudit.action == "finish_task",
                    )
                ).all()
            )
            == 1
        )


def test_landing_retains_ownership_across_generation_without_guest_slot(db, policy):
    policy["auto_merge"] = True
    task = admitted(policy)
    controls.finish_task(
        task,
        "succeeded",
        "scheduler",
        evidence={
            "pr_url": "https://github.com/owner/repo/pull/9",
            "head_sha": "a" * 40,
            "state": "ready_for_review",
        },
    )
    assert controls.can_start(task)["ok"] is False
    policy["generation"] = 1
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    for issue in [1, 2]:
        receive_issue(
            "owner/repo",
            issue,
            "issue",
            "body",
            f"https://github.com/owner/repo/issues/{issue}",
            "poller",
            generation=1,
        )
    result = admit_next("scheduler")
    assert result["ok"]
    with Session(db) as session:
        receipt = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == result["task_id"])
        ).one()
        assert receipt.issue_number == 2
        pending = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task)
        ).one()
        assert pending.state == "landing"
