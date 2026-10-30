"""Removal-only receipt policy changes preserve execution and funding authority."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_funding_limits as funding
from factory.orchestration import factory_policy_amendment as amendments
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from factory.orchestration.models import (
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

NOW = datetime(2026, 10, 30, 6, tzinfo=UTC)
EXPIRED = (NOW - timedelta(hours=1)).isoformat()
OVERLAY = {"remove_models": ["spark"]}
DIRECTION = "Remove the unused Spark lane from this receipt."
TABLES = (
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
    FactoryAudit,
    SwarmTask,
    SwarmNodeRun,
    SwarmPlanVersion,
    SwarmPlanNode,
    AgentSession,
    PendingMessage,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'restriction.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in TABLES])
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    # Deliberately keep an unsorted allowlist: removing a lane cannot reorder it.
    policy = {
        "repo": "org/repo",
        "issue_numbers": [7001],
        "generation": 14,
        "max_tasks": 2,
        "max_turns_per_task": 8,
        "max_task_turns_hard": 8,
        "max_planner_turns": 8,
        "task_budget_usd": 50,
        "turn_budget_usd": 4,
        "allowed_models": ["sol", "astra", "spark", "opus"],
        "conductor_model": "astra",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "model_pools": {
            "implement": ["sol", "spark", "astra", "opus"],
            "refine": ["astra", "spark", "opus"],
            "worker": ["sol", "spark", "astra", "opus"],
            "conductor": ["astra"],
            "reviewer": ["opus", "astra"],
        },
        "base_branch": "main",
        "max_attempts": 2,
        "task_timeout_seconds": 7200,
        "turn_timeout_seconds": 3600,
        "auto_merge": False,
    }
    with Session(engine) as db, db.begin():
        db.add_all(
            [
                FactoryControl(
                    id="factory",
                    state="enabled",
                    version=269,
                    actor="operator",
                    policy_json=json.dumps(policy),
                ),
                *[
                    SwarmTask(
                        id=task_id,
                        task_text="fix",
                        conductor_model="astra",
                        budget_usd=50,
                        start_state="factory",
                        created_at=NOW - timedelta(hours=4),
                    )
                    for task_id in ("task", "other")
                ],
                *[
                    FactoryReceipt(
                        id=receipt_id,
                        task_id=task_id,
                        repo="org/repo",
                        issue_number=receipt_id,
                        title="fix",
                        body="fix",
                        url="",
                        actor="operator",
                        state="admitted",
                        generation=14,
                        task_paused=True,
                        policy_json=json.dumps(policy),
                        direction_json=json.dumps(
                            {
                                "landing_hold": {"reason": "draft only"},
                                "nonrenewable_recovery": {"reason": "no renewal"},
                                "conductor_gates": [{"scope": "draft only"}],
                            }
                        ),
                    )
                    for receipt_id, task_id in ((927, "task"), (1027, "other"))
                ],
                SwarmPlanVersion(
                    task_id="task",
                    version=1,
                    op="bootstrap",
                    author_kind="human",
                    author="operator",
                    change_json="{}",
                    cause_kind="initial",
                ),
                SwarmPlanNode(
                    task_id="task",
                    node_key="implement",
                    kind="agent",
                    prompt="fix",
                    model="astra",
                    deps_json="[]",
                    max_cost_usd=4,
                    side_effects=True,
                    created_in_version=1,
                ),
                # A settled historical Spark attempt is evidence, not a current pin.
                SwarmNodeRun(
                    task_id="task",
                    node_key="old",
                    attempt=1,
                    status="failed",
                    model="spark",
                    pin_json=json.dumps({"model": "spark"}),
                    cost_usd=2,
                    session_id=1,
                ),
                FactoryStart(
                    task_id="task",
                    start_key="factory-node:task:old:1",
                    actor="factory",
                    model="spark",
                    status="failed",
                    max_cost_usd=4,
                    cost_usd=2,
                    session_id=1,
                ),
                AgentSession(
                    id=1,
                    local_session_id="factory:task:old:1",
                    workspace="guest",
                    branch="factory/task",
                    status="completed",
                    model="spark",
                ),
            ]
        )
        db.flush()
        controls._audit(
            db,
            "operator",
            controls.TASK_RECOVERY_ACTION,
            task_id="task",
            proposal={
                "deadline_at": EXPIRED,
                "work_turn_ceiling": 7,
                "planner_turn_ceiling": 8,
                "expected_revision": 1,
                "planner_node_key": "plan",
            },
        )
        controls._audit(
            db,
            "factory",
            "funding_granted",
            task_id="task",
            policy_overlay={"task_budget_usd": 60, "max_review_rounds": 3},
            deadline_at=EXPIRED,
            review_due_at=EXPIRED,
            next_plan="Existing grant",
        )
        controls._audit(
            db,
            "factory",
            "funding_review_requested",
            task_id="task",
            node_key="funding",
            start_key="funding",
            model="astra",
            deadline_at=EXPIRED,
        )
    yield engine
    engine.dispose()


def snapshot(engine):
    with Session(engine) as db:
        return {
            model.__name__: [r.model_dump() for r in db.exec(select(model)).all()]
            for model in TABLES
        }


def apply(proposal, *, overlay=None, key="remove-spark"):
    return amendments.apply(
        927,
        OVERLAY if overlay is None else overlay,
        DIRECTION,
        actor="operator",
        request_key=key,
        expected_preview_sha256=proposal["preview_sha256"],
    )


def test_restriction_preserves_expired_authority_and_every_other_row(database):
    before = snapshot(database)
    with Session(database) as db:
        old_policy = funding.effective_policy(db, db.get(FactoryReceipt, 927))
        old_grant = controls.task_recovery_grant("task", session=db)
        old_funding = funding.amendment(db, "task")
        old_pending = funding.pending(db, "task")
    proposal = amendments.preview(927, OVERLAY, DIRECTION)
    assert snapshot(database) == before
    assert proposal["restriction_only"] is True
    assert proposal["expected_version"] == 269
    assert proposal["expected_revision"] == 1
    result = apply(proposal)
    assert result["stays_paused"] is True
    assert result["version"] == 270
    after = snapshot(database)
    for table in set(before) - {"FactoryControl", "FactoryReceipt", "FactoryAudit"}:
        assert after[table] == before[table]
    old_control = before["FactoryControl"][0]
    new_control = after["FactoryControl"][0]
    assert {
        k: v
        for k, v in new_control.items()
        if k not in {"version", "updated_at", "actor"}
    } == {
        k: v
        for k, v in old_control.items()
        if k not in {"version", "updated_at", "actor"}
    }
    assert after["FactoryReceipt"][1] == before["FactoryReceipt"][1]
    old_receipt = before["FactoryReceipt"][0]
    new_receipt = after["FactoryReceipt"][0]
    assert {k: v for k, v in new_receipt.items() if k != "direction_json"} == {
        k: v for k, v in old_receipt.items() if k != "direction_json"
    }
    old_direction = json.loads(old_receipt["direction_json"])
    new_direction = json.loads(new_receipt["direction_json"])
    assert all(new_direction[k] == v for k, v in old_direction.items())
    assert new_direction["model_restriction"]["removed_models"] == ["spark"]
    assert after["FactoryAudit"][:-1] == before["FactoryAudit"]
    audit = json.loads(after["FactoryAudit"][-1]["detail_json"])
    assert audit["proposal"] == proposal
    with Session(database) as db:
        policy = funding.effective_policy(db, db.get(FactoryReceipt, 927))
        assert policy["allowed_models"] == ["sol", "astra", "opus"]
        assert policy["model_pools"] == {
            role: [m for m in pool if m != "spark"]
            for role, pool in old_policy["model_pools"].items()
        }
        assert {
            k: v
            for k, v in policy.items()
            if k not in {"allowed_models", "model_pools"}
        } == {
            k: v
            for k, v in old_policy.items()
            if k not in {"allowed_models", "model_pools"}
        }
        assert controls.task_recovery_grant("task", session=db) == old_grant
        assert funding.amendment(db, "task") == old_funding
        assert funding.pending(db, "task") == old_pending
        assert controls._recovery_deadline(db, "task") < NOW
    assert apply(proposal) == result
    assert snapshot(database) == after
    with pytest.raises(ValueError, match="conflicting_policy_amendment_request"):
        apply(proposal, overlay={"remove_models": ["sol"]})


@pytest.mark.parametrize(
    "overlay",
    [
        {"allowed_models": ["astra", "opus", "sol"]},
        {"model_pools": {"worker": ["astra", "sol", "opus"]}},
        {"remove_models": ["spark"], "task_budget_usd": 100},
        {"remove_models": ["spark"], "landing_hold": True},
        {"remove_models": ["spark"], "nonrenewable_recovery": True},
        {"remove_models": ["spark"], "allowed_models": ["astra", "opus", "sol"]},
        {"remove_models": []},
        {"remove_models": "spark"},
        {"remove_models": ["spark", "spark"]},
        {"remove_models": [None]},
        {"remove_models": [" spark"]},
        {"remove_models": ["new-model"]},
    ],
)
def test_restriction_refuses_replacements_additions_and_mixed_fields(database, overlay):
    before = snapshot(database)
    with pytest.raises(ValueError):
        amendments.preview(927, overlay, DIRECTION)
    assert snapshot(database) == before


@pytest.mark.parametrize(
    "source",
    [
        "conductor_model",
        "worker_model",
        "reviewer_model",
        "task_conductor",
        "task_start",
        "graph",
        "funding_choice",
        "empty_pool",
        "backlog_refill",
    ],
)
def test_restriction_refuses_used_models_and_empty_pools(database, source):
    with Session(database) as db, db.begin():
        receipt = db.get(FactoryReceipt, 927)
        policy = json.loads(receipt.policy_json)
        if source in {"conductor_model", "worker_model", "reviewer_model"}:
            policy[source] = "spark"
        elif source == "empty_pool":
            policy["model_pools"]["refine"] = ["spark"]
        elif source == "backlog_refill":
            policy["backlog_refill"] = {"model": "spark"}
        elif source == "task_conductor":
            db.get(SwarmTask, "task").conductor_model = "spark"
        elif source == "task_start":
            db.get(SwarmTask, "task").start_model = "spark"
        elif source == "graph":
            db.exec(select(SwarmPlanNode)).one().model = "spark"
        else:
            controls._audit(
                db,
                "factory",
                "funding_review_requested",
                task_id="task",
                node_key="new-funding",
                start_key="new-funding",
                model="spark",
                deadline_at=EXPIRED,
            )
        receipt.policy_json = json.dumps(policy)
    before = snapshot(database)
    with pytest.raises(ValueError, match="model_restriction_(model_in_use|empty_pool)"):
        amendments.preview(927, OVERLAY, DIRECTION)
    assert snapshot(database) == before


@pytest.mark.parametrize(
    "source",
    [
        "unpaused",
        "cancelled",
        "stopped",
        "settled",
        "reserved",
        "uncertain_start",
        "admitted",
        "dispatched",
        "uncertain",
        "unknown_run",
        "native_claim",
        "native_queued",
        "decision",
    ],
)
def test_restriction_requires_paused_quiescent_receipt(database, monkeypatch, source):
    if source == "decision":
        from factory.orchestration import factory_decisions

        monkeypatch.setattr(factory_decisions, "decision_in_flight", lambda *_: True)
    with Session(database) as db, db.begin():
        receipt = db.get(FactoryReceipt, 927)
        if source == "unpaused":
            receipt.task_paused = False
        elif source == "cancelled":
            receipt.cancellation_requested = True
        elif source == "settled":
            receipt.state = "succeeded"
        elif source == "stopped":
            db.get(FactoryControl, "factory").state = "stopped"
        elif source in {"reserved", "uncertain_start"}:
            db.exec(select(FactoryStart)).one().status = (
                "reserved" if source == "reserved" else "uncertain"
            )
        elif source in {"admitted", "dispatched", "uncertain", "unknown_run"}:
            db.exec(select(SwarmNodeRun)).one().status = source
        elif source in {"native_claim", "native_queued"}:
            db.add(
                PendingMessage(
                    session_id=1,
                    seq=1,
                    message_text="run",
                    claimed_by_replica="owner" if source == "native_claim" else None,
                    claimed_at=NOW,
                )
            )
    before = snapshot(database)
    with pytest.raises(ValueError, match="amendment_"):
        amendments.preview(927, OVERLAY, DIRECTION)
    assert snapshot(database) == before


@pytest.mark.parametrize(
    "change",
    [
        "control",
        "graph",
        "pin",
        "accounting",
        "hold",
        "policy",
        "funding",
        "recovery",
        "lease",
        "task",
        "pending_review",
        "unpaused",
        "claim",
    ],
)
def test_restriction_rechecks_preview_bindings(database, change):
    proposal = amendments.preview(927, OVERLAY, DIRECTION)
    with Session(database) as db, db.begin():
        receipt = db.get(FactoryReceipt, 927)
        if change == "control":
            db.get(FactoryControl, "factory").version += 1
        elif change == "graph":
            db.exec(select(SwarmPlanVersion)).one().version += 1
        elif change == "pin":
            db.exec(select(SwarmPlanNode)).one().model = "sol"
        elif change == "accounting":
            db.exec(select(FactoryStart)).one().cost_usd += 1
        elif change == "hold":
            receipt.direction_json = json.dumps(
                {"landing_hold": {"reason": "new hold"}}
            )
        elif change == "policy":
            policy = json.loads(receipt.policy_json)
            policy["max_task_turns_hard"] += 1
            receipt.policy_json = json.dumps(policy)
        elif change == "task":
            db.get(SwarmTask, "task").start_model = "sol"
        elif change == "unpaused":
            receipt.task_paused = False
        elif change == "claim":
            db.add(
                PendingMessage(
                    session_id=1, seq=1, message_text="run", claimed_by_replica="owner"
                )
            )
        else:
            action = {
                "funding": "funding_granted",
                "recovery": controls.TASK_RECOVERY_ACTION,
                "lease": "progress_continuation_lease",
                "pending_review": "funding_review_requested",
            }[change]
            previous = funding.latest(db, "task", action) or {"deadline_at": EXPIRED}
            previous.pop("audit_id", None)
            controls._audit(db, "other-operator", action, task_id="task", **previous)
    before = snapshot(database)
    with pytest.raises(
        ValueError, match="(policy_amendment_preview_changed|amendment_)"
    ):
        apply(proposal)
    assert snapshot(database) == before


@pytest.mark.parametrize("same_key", [False, True])
def test_restriction_serializes_concurrent_apply(database, same_key):
    proposal = amendments.preview(927, OVERLAY, DIRECTION)

    def run(key):
        try:
            return apply(proposal, key=key)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["first", "first" if same_key else "second"]))
    if same_key:
        assert results[0] == results[1]
    else:
        assert sum(isinstance(r, dict) for r in results) == 1
    with Session(database) as db:
        assert db.get(FactoryControl, "factory").version == 270
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == amendments.ACTION)
                ).all()
            )
            == 1
        )


def test_restriction_survives_later_funding_overlay(database):
    apply(amendments.preview(927, OVERLAY, DIRECTION))
    with Session(database) as db, db.begin():
        receipt = db.get(FactoryReceipt, 927)
        original = json.loads(receipt.policy_json)
        controls._audit(
            db,
            "factory",
            "funding_granted",
            task_id="task",
            policy_overlay={
                "task_budget_usd": 70,
                "allowed_models": original["allowed_models"],
                "model_pools": original["model_pools"],
            },
            deadline_at=EXPIRED,
            review_due_at=EXPIRED,
        )
        policy = funding.effective_policy(db, receipt)
        assert policy["task_budget_usd"] == 70
        assert "spark" not in policy["allowed_models"]
        assert all("spark" not in pool for pool in policy["model_pools"].values())


def test_numeric_amendment_still_refuses_expired_deadline(database):
    with pytest.raises(ValueError, match="amendment_task_deadline_expired"):
        amendments.preview(927, {"max_review_rounds": 4}, DIRECTION)


@pytest.mark.parametrize("retired", ["discarded_in_version", "cancelled_in_version"])
def test_restriction_preserves_retired_spark_graph_pins(database, retired):
    with Session(database) as db, db.begin():
        db.add(
            SwarmPlanNode(
                task_id="task",
                node_key="retired",
                kind="agent",
                prompt="old",
                model="spark",
                deps_json="[]",
                max_cost_usd=4,
                side_effects=True,
                created_in_version=1,
                **{retired: 1},
            )
        )
    before = snapshot(database)
    apply(amendments.preview(927, OVERLAY, DIRECTION))
    after = snapshot(database)
    assert after["SwarmPlanNode"] == before["SwarmPlanNode"]
    assert after["SwarmNodeRun"] == before["SwarmNodeRun"]


def test_restriction_preserves_expired_progress_lease(database):
    with Session(database) as db, db.begin():
        original = funding.latest(db, "task", controls.TASK_RECOVERY_ACTION)
        controls._audit(
            db,
            "operator",
            "progress_continuation_authorized",
            task_id="task",
            proposal={"original_recovery": original},
        )
        controls._audit(
            db,
            "operator",
            "progress_continuation_lease",
            task_id="task",
            original_recovery_audit_id=original["audit_id"],
            deadline_at=EXPIRED,
            work_turn_ceiling=8,
            planner_turn_ceiling=8,
        )
        before = controls.task_recovery_grant("task", session=db)
    apply(amendments.preview(927, OVERLAY, DIRECTION))
    with Session(database) as db:
        assert controls.task_recovery_grant("task", session=db) == before
        assert controls._recovery_deadline(db, "task") < NOW
        assert db.get(FactoryReceipt, 927).task_paused is True


def test_restriction_preserves_sparse_policy_without_recovery_or_funding(database):
    with Session(database) as db, db.begin():
        for audit in db.exec(select(FactoryAudit)).all():
            db.delete(audit)
        receipt = db.get(FactoryReceipt, 927)
        policy = json.loads(receipt.policy_json)
        policy.pop("model_pools")
        receipt.policy_json = json.dumps(policy)
    before = snapshot(database)
    apply(amendments.preview(927, OVERLAY, DIRECTION))
    with Session(database) as db:
        actual = funding.effective_policy(db, db.get(FactoryReceipt, 927))
        assert actual == {**policy, "allowed_models": ["sol", "astra", "opus"]}
        assert funding.amendment(db, "task") is None
        assert controls.task_recovery_grant("task", session=db) is None
        assert db.get(SwarmTask, "task").model_dump() == before["SwarmTask"][0]


def test_restrictions_accumulate_without_restoring_previous_removals(database):
    with Session(database) as db, db.begin():
        receipt = db.get(FactoryReceipt, 927)
        policy = json.loads(receipt.policy_json)
        policy["allowed_models"].append("sonnet")
        policy["model_pools"]["implement"].append("sonnet")
        receipt.policy_json = json.dumps(policy)
    first = amendments.preview(927, OVERLAY, DIRECTION)
    first_result = apply(first)
    second_overlay = {"remove_models": ["sonnet"]}
    second = amendments.preview(927, second_overlay, DIRECTION)
    apply(second, overlay=second_overlay, key="remove-sonnet")
    with Session(database) as db:
        receipt = db.get(FactoryReceipt, 927)
        policy = funding.effective_policy(db, receipt)
        assert policy["allowed_models"] == ["sol", "astra", "opus"]
        assert policy["model_pools"]["implement"] == ["sol", "astra", "opus"]
        assert json.loads(receipt.direction_json)["model_restriction"][
            "removed_models"
        ] == ["sonnet", "spark"]
    before = snapshot(database)
    assert apply(first) == first_result
    assert snapshot(database) == before


def test_restriction_protects_default_enabled_refill_model(database):
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 927)
        policy = json.loads(row.policy_json)
        policy["conductor_model"] = "sol"
        policy["model_pools"]["conductor"] = ["sol"]
        policy["backlog_refill"] = {"enabled": True}
        row.policy_json = json.dumps(policy)
        db.get(SwarmTask, "task").conductor_model = "sol"
        db.exec(select(SwarmPlanNode)).one().model = "sol"
        pending = funding.pending(db, "task")
        controls._audit(
            db,
            "operator",
            "funding_review_settled",
            task_id="task",
            request_id=pending["audit_id"],
            outcome="settled",
        )
    with pytest.raises(ValueError, match="model_restriction_model_in_use"):
        amendments.preview(927, {"remove_models": ["astra"]}, DIRECTION)
