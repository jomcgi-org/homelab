"""Exact operator preview/apply through the real queue and start owners."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from auth.principal import Authority, Principal, PrincipalKind
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration import operator_readmission as readmission
from factory.orchestration.factory_intake import (
    admit_next,
    _eligible_clause,
    automatic_history_exclusion,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
    FactoryClassTier,
    FactoryReviewVerdict,
    WorkItem,
    WorkItemEvent,
    WorkItemEdge,
)
from factory.orchestration.models import (
    SwarmTask,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmConductorCall,
)

TABLES = [
    m.__table__
    for m in (
        FactoryControl,
        FactoryReceipt,
        FactoryAudit,
        FactoryStart,
        WorkItem,
        WorkItemEvent,
        WorkItemEdge,
        FactoryClassTier,
        FactoryReviewVerdict,
        SwarmTask,
        SwarmNodeRun,
        SwarmPlanNode,
        SwarmPlanVersion,
        SwarmConductorCall,
    )
]
NOW = datetime(2026, 11, 12, 15, tzinfo=timezone.utc)
OPERATOR = Principal(
    subject="operator:joe",
    actor=(),
    scope=(),
    groups=("operators",),
    email=None,
    kind=PrincipalKind.HUMAN,
    authority=Authority.STANDING,
)
POLICY = {
    "repo": "owner/repo",
    "issue_numbers": [7250],
    "generation": 7,
    "max_tasks": 2,
    "max_turns_per_task": 8,
    "max_planner_turns": 4,
    "task_budget_usd": 5.0,
    "turn_budget_usd": 2.0,
    "allowed_models": ["opus", "luna"],
    "conductor_model": "opus",
    "worker_model": "luna",
    "base_branch": "main",
    "turn_timeout_seconds": 60,
    "max_attempts": 3,
    "task_timeout_seconds": 3600,
    "intake": {"enabled": True},
}


def setup(engine, monkeypatch):
    SQLModel.metadata.create_all(engine, tables=TABLES)
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(graph, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    monkeypatch.setattr(readmission, "_issue", lambda repo, number: issue(number))
    with Session(engine) as db:
        db.add(
            FactoryControl(
                id="factory",
                actor="operator",
                state="enabled",
                version=8,
                policy_json=json.dumps(controls.validate_policy(POLICY)),
            )
        )
        db.commit()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'readmission.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    setup(engine, monkeypatch)
    yield engine
    engine.dispose()


def issue(number=7777):
    return {
        "number": number,
        "title": "remaining work",
        "body": "accepted task",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": "open",
        "labels": [{"name": "agent-ready"}],
        "user": {"login": "jomcgi", "type": "User"},
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-11-12T00:00:00Z",
    }


def seed(
    engine,
    number=7777,
    *,
    state="cancelled",
    task_class="bug-fix",
    attempts=0,
    direction=None,
):
    with Session(engine) as db:
        task = SwarmTask(
            id=f"prior-{number}",
            conductor_model="opus",
            task_text="prior",
            start_state=state,
            settled_at=NOW,
        )
        db.add(task)
        db.flush()
        row = FactoryReceipt(
            repo="owner/repo",
            issue_number=number,
            generation=6,
            title="old",
            body="old body",
            url=issue(number)["html_url"],
            actor=controls.INTAKE_ACTOR,
            state=state,
            task_id=task.id,
            task_class=task_class,
            policy_json=json.dumps(controls.validate_policy(POLICY)),
            direction_json=json.dumps(direction) if direction else None,
        )
        db.add(row)
        db.add_all(
            [
                FactoryStart(
                    task_id=task.id,
                    start_key=f"factory-node:{task.id}:implement:1",
                    actor="executor",
                    model="luna",
                    status="failed",
                    max_cost_usd=1,
                    cost_usd=0.75,
                )
            ]
            if attempts
            else []
        )
        db.add_all(
            [
                SwarmNodeRun(
                    task_id=task.id,
                    node_key="implement",
                    attempt=i + 1,
                    status="failed",
                    pin_json=json.dumps({"max_attempts": 3}),
                )
                for i in range(attempts)
            ]
        )
        db.commit()
        return row.id


def preview(number=7777, key="request", principal=OPERATOR):
    return readmission.control(
        principal, "preview", "owner/repo", number, "Revisit accepted work", key
    )


def apply(
    p, number=7777, key="request", principal=OPERATOR, reason="Revisit accepted work"
):
    return readmission.control(
        principal, "apply", "owner/repo", number, reason, key, p["preview_sha256"]
    )


def unchanged(engine):
    with Session(engine) as db:
        row = db.get(FactoryControl, "factory")
        return row.policy_json, row.version, row.actor


def test_preview_apply_real_admission_preserves_natural_fence_and_accounting(engine):
    prior = seed(engine, attempts=1)
    before = unchanged(engine)
    with Session(engine) as db:
        db.add(
            FactoryAudit(
                actor="operator",
                action="standing_landing_authority",
                detail_json='{"unchanged":true}',
            )
        )
        db.commit()
        old = db.get(FactoryReceipt, prior).model_dump()
        assert (
            automatic_history_exclusion(
                db, "owner/repo", 7777, controls.INTAKE_ACTOR, 7
            )
            == "cancelled_issue"
        )
    p = preview()
    assert p["attempt_debt"] == 1
    assert p["accounting"]["turns_used"] == 1
    assert p["accounting"]["committed_cost_usd"] == 0.75
    assert p["remaining"]["max_attempts"] == 2
    assert p["remaining"]["max_task_turns_hard"] == 7
    with Session(engine) as db:
        assert len(db.exec(select(FactoryReceipt)).all()) == 1
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == readmission.ACTION)
        ).all()
    result = apply(p)
    assert apply(p) == result
    assert unchanged(engine) == before
    with Session(engine) as db:
        row = db.get(FactoryReceipt, result["receipt_id"])
        assert row.generation == 7 and row.recurrence_key.startswith("operator:")
        assert controls.admission_eligible(
            POLICY, 7777, row.actor, receipt=row, session=db
        )
        assert db.exec(
            select(FactoryReceipt.id).where(
                _eligible_clause(POLICY), FactoryReceipt.state == "queued"
            )
        ).all() == [row.id]
        assert controls.execution_slot_holders(POLICY, [row], include_queued=True) == [
            row
        ]
        assert (
            automatic_history_exclusion(
                db, "owner/repo", 7777, controls.INTAKE_ACTOR, 7
            )
            == "cancelled_issue"
        )
    admitted = admit_next("test")
    assert admitted["ok"], admitted
    assert admitted["receipt_id"] == result["receipt_id"]
    assert admitted["policy"]["max_attempts"] == 2
    assert admitted["policy"]["max_task_turns_hard"] == 7
    task_id = admitted["task_id"]
    # A caller requesting a higher node bound still sees the inherited cap in
    # readiness and physical dispatch, even after changing the node's name.
    node = graph.add_node(
        task_id,
        node_key="renamed",
        kind="work",
        prompt="work",
        model="luna",
        deps=[],
        max_cost_usd=1,
        side_effects=True,
        max_attempts=3,
        turn_timeout_seconds=60,
        expected_version=1,
        author_kind="conductor",
        author="test",
        cause_kind="test",
        cause_ref=None,
        stated_reason="test",
    )
    assert node.ok, node
    assert graph.load_graph(task_id)[0]["max_attempts"] == 2
    with Session(engine) as db:
        db.add_all(
            [
                SwarmNodeRun(
                    task_id=task_id, node_key="renamed", attempt=i + 1, status="failed"
                )
                for i in range(2)
            ]
        )
        db.commit()
    refused = graph.admit_dispatch(
        task_id, "renamed", dispatch_key="new-attempt", execution_context={}
    )
    assert not refused.ok and refused.refusal_code == "attempts_exhausted"
    with Session(engine) as db:
        assert db.get(FactoryReceipt, prior).model_dump() == old
        assert (
            db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.action == "standing_landing_authority"
                )
            )
            .one()
            .detail_json
            == '{"unchanged":true}'
        )
        from factory.orchestration.factory_funding_limits import objective_for_receipt

        objective = objective_for_receipt(
            db, db.get(FactoryReceipt, result["receipt_id"])
        )
        assert objective["committed_cost_usd"] == 0.75
        assert f"prior-7777" in objective["task_ids"]


@pytest.mark.parametrize("attempts", [3, 4])
def test_exhausted_prior_attempts_refuse_new_allowance(engine, attempts):
    seed(engine, attempts=attempts)
    with pytest.raises(ValueError, match="attempts_exhausted"):
        preview()
    with Session(engine) as db:
        assert len(db.exec(select(FactoryReceipt)).all()) == 1


def test_failed_refinement_requires_explicit_recurrence(engine):
    seed(engine, state="failed", task_class="refine")
    with Session(engine) as db:
        assert (
            automatic_history_exclusion(
                db, "owner/repo", 7777, controls.INTAKE_ACTOR, 7
            )
            == "refinement_failed"
        )
    result = apply(preview())
    assert admit_next("test")["receipt_id"] == result["receipt_id"]
    with Session(engine) as db:
        assert (
            automatic_history_exclusion(
                db, "owner/repo", 7777, controls.INTAKE_ACTOR, 7
            )
            == "refinement_failed"
        )


@pytest.mark.parametrize("kind", ["human", "delegated", "workload", "delegation_claim"])
@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_authentication_precedes_every_read(engine, monkeypatch, kind, operation):
    principal = {
        "human": replace(OPERATOR, groups=()),
        "delegated": replace(OPERATOR, authority=Authority.DELEGATED),
        "workload": replace(OPERATOR, kind=PrincipalKind.WORKLOAD),
        "delegation_claim": replace(OPERATOR, delegation_claim_present=True),
    }[kind]
    monkeypatch.setattr(
        readmission, "_issue", lambda *args: pytest.fail("unauthenticated network read")
    )
    with pytest.raises(ValueError, match="standing_operator_required"):
        readmission.control(
            principal, operation, "owner/repo", 7777, "reason", "key", "a" * 64
        )


@pytest.mark.parametrize(
    "change, reason",
    [
        ("closed", "issue_not_open"),
        ("missing_label", "intake_label_missing"),
        ("human", "human_owned"),
        ("excluded", "excluded_label"),
        ("assigned", "assigned"),
        ("updated", "readmission_preview_changed"),
    ],
)
def test_apply_rechecks_issue_and_preview(engine, monkeypatch, change, reason):
    seed(engine)
    p = preview()
    snapshot = issue()
    if change == "closed":
        snapshot["state"] = "closed"
    elif change == "missing_label":
        snapshot["labels"] = []
    elif change == "human":
        snapshot["labels"].append({"name": "human"})
    elif change == "excluded":
        snapshot["labels"].append({"name": "needs-human"})
    elif change == "assigned":
        snapshot["assignees"] = [{"login": "owner"}]
    else:
        snapshot["updated_at"] = "2026-11-12T14:00:00Z"
    monkeypatch.setattr(readmission, "_issue", lambda *args: snapshot)
    with pytest.raises(ValueError, match=reason):
        apply(p)
    with Session(engine) as db:
        assert len(db.exec(select(FactoryReceipt)).all()) == 1
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == readmission.ACTION)
        ).all()


@pytest.mark.parametrize(
    "hold", ["landing", "active", "uncertain", "human", "recovery", "cancel", "queued"]
)
def test_protected_history_vetoes_preview_and_stale_apply(engine, hold):
    prior = seed(engine)
    p = preview()
    with Session(engine) as db:
        row = db.get(FactoryReceipt, prior)
        if hold == "landing":
            row.direction_json = '{"landing_hold":{"actor":"operator"}}'
        elif hold in ("active", "queued"):
            row.state = "admitted" if hold == "active" else "queued"
        elif hold == "uncertain":
            db.add(
                FactoryStart(
                    task_id=row.task_id,
                    start_key="unknown",
                    actor="executor",
                    model="luna",
                    status="uncertain",
                    max_cost_usd=1,
                )
            )
        elif hold == "human":
            db.add(
                FactoryAudit(
                    task_id=row.task_id, actor="operator", action="human_handoff"
                )
            )
        elif hold == "recovery":
            row.direction_json = '{"nonrenewable_recovery":true}'
        elif hold == "cancel":
            row.cancellation_requested = True
        db.commit()
    with pytest.raises(ValueError):
        preview()
    with pytest.raises(ValueError):
        apply(p)


def test_landing_hold_across_legacy_alias_and_class_wins_over_cancellation(engine):
    seed(engine)
    with Session(engine) as db:
        item = WorkItem(
            title="alias",
            body="alias",
            state="ready",
            task_class="refine",
            labels=["agent-ready"],
            github_repo="owner/repo",
            github_issue_number=7777,
            source_kind="github",
            authority="github",
            source_ref="alias",
            trust="trusted",
        )
        db.add(item)
        db.flush()
        db.add(
            FactoryReceipt(
                repo="legacy/repo",
                issue_number=44,
                generation=1,
                task_class="refine",
                title="alias",
                body="alias",
                url="alias",
                actor="operator",
                state="failed",
                work_item_id=item.id,
                direction_json='{"landing_hold":{"actor":"operator"}}',
            )
        )
        db.commit()
    with pytest.raises(ValueError, match="landing_held"):
        preview()
    with Session(engine) as db:
        assert (
            automatic_history_exclusion(
                db, "owner/repo", 7777, controls.INTAKE_ACTOR, 7
            )
            == "cancelled_issue"
        )


def test_replay_conflict_stale_control_and_forged_marker(engine):
    seed(engine)
    p = preview()
    result = apply(p)
    with pytest.raises(ValueError, match="request_conflict"):
        apply(p, reason="changed")
    with Session(engine) as db:
        row = db.get(FactoryReceipt, result["receipt_id"])
        row.recurrence_key = "operator:forged"
        controls._audit(
            db, "pretend-operator", "operator_readmission_queued", receipt_id=row.id
        )
        db.commit()
        assert not controls.admission_eligible(
            POLICY, 7777, row.actor, receipt=row, session=db
        )
        assert not db.exec(
            select(FactoryReceipt.id).where(
                _eligible_clause(POLICY), FactoryReceipt.state == "queued"
            )
        ).all()
    assert not admit_next("test")["ok"]
    seed(engine, 8888)
    p = preview(8888, "other")
    with Session(engine) as db:
        db.get(FactoryControl, "factory").version += 1
        db.commit()
    with pytest.raises(ValueError, match="preview_changed"):
        apply(p, 8888, "other")


def test_final_admission_rechecks_live_hold_without_releasing_it(engine, monkeypatch):
    prior = seed(engine)
    apply(preview())
    with Session(engine) as db:
        db.get(
            FactoryReceipt, prior
        ).direction_json = '{"landing_hold":{"actor":"operator"}}'
        db.commit()
    assert not admit_next("test")["ok"]
    with Session(engine) as db:
        assert controls.landing_held(db.get(FactoryReceipt, prior))


def test_rolling_boundary_and_repository_wide_quota(engine, monkeypatch):
    for number in range(24):
        seed(engine, 8000 + number)
        p = preview(8000 + number, str(number))
        apply(p, 8000 + number, str(number))
    seed(engine)
    p = preview()
    with pytest.raises(ValueError, match="rolling_limit"):
        apply(p)
    monkeypatch.setattr(controls, "_now", lambda: NOW + timedelta(hours=24))
    assert apply(p)["ok"]


def test_rebrief_retains_authority_but_cannot_renew_renamed_attempt_lineage(engine):
    seed(engine, attempts=1)
    received = apply(preview())
    admitted = admit_next("test")
    from factory.orchestration import factory_decisions

    with Session(engine) as db:
        row = db.get(FactoryReceipt, received["receipt_id"])
        row.state = "escalated"
        task = db.get(SwarmTask, admitted["task_id"])
        task.start_state, task.settled_at = "escalated", NOW
        db.add_all(
            [
                SwarmNodeRun(
                    task_id=task.id, node_key="renamed", attempt=i + 1, status="failed"
                )
                for i in range(2)
            ]
        )
        db.commit()
        assert factory_decisions._requeue_blocker(db, row) is None
        factory_decisions._requeue(row)
        db.commit()
    # The original task spent one attempt and this task spent two. Changing
    # node names or rebriefing this same receipt must not grant a fourth.
    assert not admit_next("test")["ok"]


@pytest.mark.parametrize("change", ["closed", "human", "missing_label"])
def test_final_admission_rechecks_fresh_issue(engine, monkeypatch, change):
    seed(engine)
    apply(preview())
    snapshot = issue()
    if change == "closed":
        snapshot["state"] = "closed"
    elif change == "human":
        snapshot["labels"].append({"name": "human"})
    else:
        snapshot["labels"] = []
    monkeypatch.setattr(readmission, "_issue", lambda *args: snapshot)
    assert not admit_next("test")["ok"]


def test_exact_replay_after_quota_or_evidence_changes_creates_no_work(
    engine, monkeypatch
):
    seed(engine)
    p = preview()
    result = apply(p)
    monkeypatch.setattr(
        readmission, "_issue", lambda *args: pytest.fail("replay read GitHub")
    )
    assert apply(p) == result
    with Session(engine) as db:
        assert (
            readmission.status(db, "owner/repo")["successful_applications_last_24h"]
            == 1
        )
        assert len(db.exec(select(FactoryReceipt)).all()) == 2


def test_succeeded_advisory_history_keeps_delivery_path_but_delivered_work_refuses(
    engine,
):
    prior = seed(engine)
    with Session(engine) as db:
        db.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=7777,
                generation=5,
                task_class="refine",
                title="brief",
                body="brief",
                url=issue()["html_url"],
                actor="operator",
                state="succeeded",
            )
        )
        db.commit()
    p = preview()
    with Session(engine) as db:
        row = db.get(FactoryReceipt, prior)
        row.state = "succeeded"
        db.commit()
    with pytest.raises(ValueError, match="existing_work_owner"):
        apply(p)


def test_failed_delivery_alone_cannot_bypass_natural_cooldown(engine):
    seed(engine, state="failed")
    with pytest.raises(ValueError, match="no_excluded_terminal_history"):
        preview()
