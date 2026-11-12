"""Human consent, durable authority and the native queue's exception boundary."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory import review_publisher
from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_conductor as conductor
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_landing as landing
from factory.orchestration import factory_landing_release as releases
from factory.orchestration import operator_landing_authorization as mod
from factory.orchestration import operator_landing_evidence as evidence
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
    WorkItem,
)
from factory.orchestration.models import (
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)
from factory.orchestration.operator_landing_evidence_test import (
    BASE,
    BRANCH,
    HEAD,
    REPO,
    REF,
    TIP,
    Repository,
    proof_hash,
    removal_and_rename,
)

NOW = datetime(2026, 11, 8, 15, tzinfo=timezone.utc)
TASK = "operator-reviewed-task"
NUMBER = 7
RECEIPT = 17


def consent():
    return {
        "classification": "requires_operator",
        "evidence": "Reviewed the credential boundary and exact native reviewer assessment.",
        "acknowledged_effects": {
            key: key in {"security_sensitive", "authentication_or_credentials"}
            for key in mod.EFFECTS
        },
        "rollback_plan": "Revert this exact change through reviewed GitOps delivery.",
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }


def outcome(scope, verdict="approve"):
    value = {
        "verdict": verdict,
        "head_sha": HEAD,
        "pr_number": NUMBER,
        "summary": "Inspected this exact diff and its security effects.",
    }
    if scope is not None:
        value["landing_scope"] = scope
    return json.dumps(
        {"value": value, "artifact": {"status": "ok", "errors": [], "value": value}}
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = Repository()
    repo.pull.update(
        state="open",
        merged=False,
        draft=False,
        node_id="PR_7",
        body="Fixes #123",
        auto_merge=None,
    )
    repo.pull["head"].update(ref=f"factory/{TASK}", repo={"full_name": REPO})
    policy = {
        "repo": REPO,
        "base_branch": BRANCH,
        "auto_merge": True,
        "generation": 14,
        "allowed_models": ["terra", "opus"],
        "reviewer_model": "terra",
        "task_budget_usd": 50,
        "turn_budget_usd": 4,
        "max_turns_per_task": 10,
    }
    typed = {
        "classification": "requires_operator",
        **consent()["acknowledged_effects"],
        "evidence": "This exact credential boundary change requires operator approval.",
        "base_sha": BASE,
        "paths": sorted(
            row["filename"] for row in repo.data[f"compare/{TIP}...{HEAD}"]["files"]
        ),
    }
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operator.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                AgentSession,
                PendingMessage,
                SwarmTask,
                SwarmNodeRun,
                SwarmPlanVersion,
                SwarmPlanNode,
                FactoryControl,
                FactoryReceipt,
                FactoryStart,
                FactoryAudit,
                WorkItem,
            )
        ],
    )
    with Session(engine) as db, db.begin():
        db.add_all(
            [
                FactoryControl(
                    id="factory",
                    actor="operator",
                    state="enabled",
                    policy_json=json.dumps(policy),
                    version=9,
                ),
                SwarmTask(
                    id=TASK,
                    task_text="Deliver the bounded repair",
                    repo=REPO,
                    base_branch=BRANCH,
                    conductor_model="terra",
                    budget_usd=50,
                    start_state="factory",
                ),
                WorkItem(
                    id=42,
                    title="Repair",
                    state="active",
                    source_kind="github",
                    trust="trusted",
                    authority="github",
                    github_repo=REPO,
                    github_issue_number=123,
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                FactoryReceipt(
                    id=RECEIPT,
                    task_id=TASK,
                    repo=REPO,
                    issue_number=123,
                    title="Repair",
                    body="Scope of the repair",
                    url="https://example.invalid/123",
                    actor="operator",
                    state="landing",
                    generation=14,
                    work_item_id=42,
                    policy_json=json.dumps(policy),
                    updated_at=NOW - timedelta(minutes=1),
                ),
                FactoryAudit(
                    actor="factory:intake",
                    action="admit_next",
                    task_id=TASK,
                    detail_json=json.dumps({"policy_version": 9}),
                ),
                FactoryAudit(
                    actor="factory:reconciler",
                    action="delivery_ready",
                    task_id=TASK,
                    detail_json=json.dumps(
                        {
                            "outcome": "succeeded",
                            "evidence": {
                                "pr_url": f"https://github.com/{REPO}/pull/{NUMBER}",
                                "head_sha": HEAD,
                                "state": "ready_for_review",
                            },
                        }
                    ),
                ),
                SwarmNodeRun(
                    id=100,
                    task_id=TASK,
                    node_key="implement_fix",
                    attempt=1,
                    session_id=10,
                    status="succeeded",
                    head_sha=HEAD,
                ),
                SwarmNodeRun(
                    id=200,
                    task_id=TASK,
                    node_key="review_fix",
                    attempt=1,
                    session_id=20,
                    status="succeeded",
                    head_sha=HEAD,
                    outcome_json=outcome(typed),
                ),
                FactoryStart(
                    task_id=TASK,
                    start_key=f"factory-node:{TASK}:implement_fix:1",
                    actor="factory",
                    model="terra",
                    max_cost_usd=4,
                    status="succeeded",
                    cost_usd=2,
                    session_id=10,
                    created_at=NOW - timedelta(minutes=5),
                ),
            ]
        )
    checks = {
        "sha": HEAD,
        "total_count": 1,
        "statuses": [
            {
                "id": 17,
                "context": "pr-checks",
                "state": "success",
                "target_url": "https://ci.invalid/17",
                "updated_at": NOW.isoformat(),
            }
        ],
    }
    clock = [NOW]
    calls = []
    fail_arm = [False]

    def get(repository, suffix):
        assert repository == REPO
        if suffix == f"pulls/{NUMBER}":
            return copy.deepcopy(repo.pull)
        if suffix == f"commits/{HEAD}/status?per_page=100":
            return copy.deepcopy(checks)
        return repo.get(repository, suffix)

    def graphql(query, variables):
        if query == landing._QUEUE_ENTRIES:
            return {
                "nodes": [
                    {
                        "id": node,
                        "mergeQueueEntry": copy.deepcopy(repo.pull.get("queue_entry")),
                    }
                    for node in variables["ids"]
                ]
            }
        calls.append(query)
        if query == landing._ARM_AUTO_MERGE:
            assert variables == {"pullRequestId": "PR_7", "expectedHeadOid": HEAD}
            repo.pull["auto_merge"] = {"merge_method": "REBASE"}
            repo.pull["queue_entry"] = {"id": "MQ_1"}
            if fail_arm[0]:
                raise httpx.ReadTimeout("response unknown")
        elif query == landing._DISARM_AUTO_MERGE:
            repo.pull["auto_merge"] = None
        elif query == landing._DEQUEUE_PULL_REQUEST:
            repo.pull["queue_entry"] = None
        else:
            raise AssertionError(query)
        return {}

    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: clock[0])
    monkeypatch.setattr(landing, "_now", lambda: clock[0])
    monkeypatch.setattr(conductor, "github_get", get)
    monkeypatch.setattr(landing, "github_get", get)
    monkeypatch.setattr(landing, "github_graphql", graphql)
    monkeypatch.setattr(evidence, "_get", get)
    yield SimpleNamespace(
        db=engine,
        repo=repo,
        typed=typed,
        policy=policy,
        checks=checks,
        clock=clock,
        calls=calls,
        fail_arm=fail_arm,
    )
    engine.dispose()


def preview(scope=None, *, actor="joe"):
    return releases.preview(
        RECEIPT,
        NUMBER,
        HEAD,
        "Approve this exact repair",
        scope=scope or consent(),
        actor=actor,
    )


def apply(proposal, *, key="approval-1", actor="joe", scope=None):
    return releases.apply(
        RECEIPT,
        NUMBER,
        HEAD,
        "Approve this exact repair",
        actor=actor,
        request_key=key,
        expected_preview_sha256=proposal["preview_sha256"],
        scope=scope or consent(),
    )


def refusal(world):
    with controls._locked_session() as (db, _):
        return releases.scoped_refusal(
            db, db.get(FactoryReceipt, RECEIPT), NUMBER, HEAD
        )


def audits(world, action):
    with Session(world.db) as db:
        return [
            row.model_dump()
            for row in db.exec(
                select(FactoryAudit).where(FactoryAudit.action == action)
            ).all()
        ]


def item():
    return {
        "task_id": TASK,
        "pr_number": NUMBER,
        "head_sha": HEAD,
        "approved_head_sha": HEAD,
        "armed": 0,
        "ejected": 0,
        "merged": None,
        "refused": False,
    }


def revoke(result, *, key="revoke-1", audit_id=None):
    return mod.revoke(
        RECEIPT,
        NUMBER,
        HEAD,
        "Revoke this decision",
        actor="second-operator",
        request_key=key,
        authorization_audit_id=audit_id or result["authorization_audit_id"],
    )


def test_preview_apply_binds_actor_native_review_git_ci_and_preserves_delivery(world):
    assert isinstance(review_publisher.collect(TASK), review_publisher.ReviewEvidence)
    proposal = preview()
    assert proposal["actor"] == "joe"
    assert proposal["operator_evidence"]["git"]["base_tip_sha"] == TIP
    assert proposal["operator_evidence"]["git"]["merge_base_sha"] == BASE
    assert proposal["operator_evidence"]["review"]["identity"]["review_run_id"] == 200
    assert not audits(world, releases.SCOPE_ACTION)
    result = apply(proposal)
    assert result["ok"] and not result["delivery_accepted"]
    grant = result["landing_authorization"]
    assert grant["schema_version"] == 2
    assert grant["authorization_audit_id"] == result["authorization_audit_id"]
    assert (
        grant["actor"] == "joe"
        and grant["scope"]["expires_at"] == consent()["expires_at"]
    )
    assert refusal(world) is None
    assert apply(proposal) == result
    assert len(audits(world, releases.SCOPE_ACTION)) == 1
    with Session(world.db) as db:
        row = db.get(FactoryReceipt, RECEIPT)
        assert row.state == "landing" and not row.task_paused
        assert db.get(SwarmTask, TASK).budget_usd == 50
        assert db.exec(select(FactoryStart)).one().cost_usd == 2


@pytest.mark.parametrize("review_paths", ["destination", "source", "both"])
def test_mixed_removed_renamed_preview_confirms_exact_new_name_scope(
    world, review_paths
):
    # Repository/DB fixtures only. This is no evidence of #7380's live preview.
    old_pull = copy.deepcopy(world.repo.pull)
    removal_and_rename(world.repo, edited=True)
    old_pull["changed_files"] = world.repo.pull["changed_files"]
    world.repo.pull = old_pull
    paths = sorted(
        item["filename"] for item in world.repo.data[f"compare/{TIP}...{HEAD}"]["files"]
    )
    if review_paths == "source":
        paths = ["chat_public/retention.py", "tests/old/retention_test.py"]
    elif review_paths == "both":
        paths = sorted([*paths, "chat_public/retention.py"])
    with Session(world.db) as db, db.begin():
        db.get(SwarmNodeRun, 200).outcome_json = outcome(
            {**world.typed, "paths": paths}
        )
    if review_paths != "destination":
        with pytest.raises(ValueError, match="operator_landing_review_scope_changed"):
            preview()
        return
    proposal = preview()
    proof = proposal["operator_evidence"]["git"]
    assert [item["status"] for item in proof["files"]] == ["renamed", "removed"]
    world.repo.calls.clear()
    assert evidence.confirm(world.repo.pull, proof) is None
    assert world.repo.calls == [REF]
    assert not audits(world, releases.SCOPE_ACTION)
    result = apply(proposal)
    assert result["ok"] and not result["delivery_accepted"]
    assert refusal(world) is None
    # A valid-looking replacement manifest cannot authenticate itself by
    # recomputing its digest; the persisted audit must still match byte-for-byte.
    with Session(world.db) as db, db.begin():
        row = db.get(FactoryReceipt, RECEIPT)
        direction = json.loads(row.direction_json)
        stored = direction["landing_authorization"]["operator_evidence"]["git"]
        stored["files"][0]["previous_filename"] = "forged/source.py"
        stored["manifest_sha256"] = proof_hash(stored)
        row.direction_json = json.dumps(direction)
    assert refusal(world) == "operator_landing_grant_unaudited"


@pytest.mark.parametrize("changed", ["actor", "scope", "reason", "request_key"])
def test_preview_and_idempotency_bind_exact_actor_and_request(world, changed):
    proposal = preview()
    if changed == "actor":
        with pytest.raises(ValueError, match="landing_release_preview_changed"):
            apply(proposal, actor="someone-else")
        assert not audits(world, releases.SCOPE_ACTION)
        return
    result = apply(proposal)
    args = dict(
        actor="joe",
        request_key="approval-1",
        expected_preview_sha256=proposal["preview_sha256"],
        scope=consent(),
    )
    reason = "Approve this exact repair"
    if changed == "scope":
        args["scope"]["rollback_plan"] = "A different rollback plan"
    elif changed == "reason":
        reason = "Different authorization"
    else:
        args["request_key"] = "different-request"
    with pytest.raises(
        ValueError,
        match="conflicting_landing_release_request|landing_release_preview_changed",
    ):
        releases.apply(RECEIPT, NUMBER, HEAD, reason, **args)
    assert len(audits(world, releases.SCOPE_ACTION)) == 1
    assert refusal(world) is None and result["ok"]


@pytest.mark.parametrize(
    "change",
    [
        "missing_effect",
        "extra_effect",
        "truthy_effect",
        "effect_false",
        "missing_rollback",
        "extra_actor",
        "naive_expiry",
        "nonutc_expiry",
        "malformed_expiry",
        "expired",
        "too_far",
        "blank_evidence",
    ],
)
def test_requires_operator_consent_is_explicit_bounded_and_not_forgeable(world, change):
    value = consent()
    if change == "missing_effect":
        value["acknowledged_effects"].pop("destructive")
    elif change == "extra_effect":
        value["acknowledged_effects"]["skip_ci"] = True
    elif change == "truthy_effect":
        value["acknowledged_effects"]["security_sensitive"] = 1
    elif change == "effect_false":
        value["acknowledged_effects"]["security_sensitive"] = False
    elif change == "missing_rollback":
        value.pop("rollback_plan")
    elif change == "extra_actor":
        value["actor"] = "forged-human"
    elif change == "naive_expiry":
        value["expires_at"] = "2026-11-08T16:00:00"
    elif change == "nonutc_expiry":
        value["expires_at"] = "2026-11-08T18:00:00+02:00"
    elif change == "malformed_expiry":
        value["expires_at"] = "tomorrow"
    elif change == "expired":
        value["expires_at"] = NOW.isoformat()
    elif change == "too_far":
        value["expires_at"] = (NOW + timedelta(hours=24, microseconds=1)).isoformat()
    else:
        value["evidence"] = " "
    with pytest.raises(ValueError):
        preview(value)
    assert not audits(world, releases.SCOPE_ACTION)


def test_absolute_expiry_boundary_and_expired_replay_are_not_renewal(world):
    value = consent()
    value["expires_at"] = (NOW + timedelta(hours=24)).isoformat()
    proposal = preview(value)
    result = apply(proposal, scope=value)
    world.clock[0] = NOW + timedelta(hours=24)
    assert refusal(world) == "operator_landing_expiry_out_of_bounds"
    assert apply(proposal, scope=value) == result
    assert len(audits(world, releases.SCOPE_ACTION)) == 1
    with pytest.raises(ValueError, match="expiry_out_of_bounds"):
        preview(value)


def mutate(world, change):
    if change == "head":
        world.repo.pull["head"]["sha"] = "9" * 40
    elif change == "base":
        world.repo.data[REF]["object"]["sha"] = "8" * 40
    elif change == "draft":
        world.repo.pull["draft"] = True
    elif change == "closed":
        world.repo.pull["state"] = "closed"
    elif change == "branch":
        world.repo.pull["base"]["ref"] = "different"
    elif change == "ci":
        world.checks["statuses"][0]["state"] = "failure"
    elif change == "ci_replaced":
        world.checks["statuses"][0]["id"] += 1
    else:
        with Session(world.db) as db, db.begin():
            row = db.get(FactoryReceipt, RECEIPT)
            control = db.get(FactoryControl, "factory")
            if change == "hold":
                row.direction_json = json.dumps(
                    {
                        **json.loads(row.direction_json or "{}"),
                        "landing_hold": {"actor": "joe", "reason": "Keep held"},
                    }
                )
            elif change == "pause":
                row.task_paused = True
            elif change == "cancel":
                row.cancellation_requested = True
            elif change == "human_label":
                db.get(WorkItem, 42).labels = ["human"]
            elif change == "generation":
                row.generation += 1
            elif change == "policy":
                control.policy_json = json.dumps({**world.policy, "auto_merge": False})
            elif change == "policy_roundtrip":
                controls._audit(db, "operator", "configure", result={"ok": True})
            elif change == "receipt_body":
                row.body = "Changed objective"
            elif change == "work_item":
                db.get(WorkItem, 42).updated_at += timedelta(seconds=1)
            elif change == "task":
                db.get(SwarmTask, TASK).start_state = "failed"
            elif change == "review":
                db.get(SwarmNodeRun, 200).outcome_json = outcome(
                    world.typed, "changes_requested"
                )
            elif change == "review_replaced":
                db.add(
                    SwarmNodeRun(
                        id=201,
                        task_id=TASK,
                        node_key="review_other",
                        attempt=1,
                        session_id=21,
                        status="succeeded",
                        head_sha=HEAD,
                        outcome_json=outcome(world.typed),
                    )
                )
            elif change == "review_scope":
                db.get(SwarmNodeRun, 200).outcome_json = outcome(
                    {
                        **world.typed,
                        "evidence": "Changed independent assessment of the same paths.",
                    }
                )
            elif change == "start":
                db.exec(select(FactoryStart)).one().status = "reserved"
            elif change in ("claimed", "pending"):
                db.add(
                    AgentSession(
                        id=55,
                        local_session_id=f"factory:{TASK}:extra:1",
                        workspace="guest",
                        branch=f"factory/{TASK}",
                        status="completed",
                        model="terra",
                    )
                )
                db.flush()
                db.add(
                    PendingMessage(
                        session_id=55,
                        seq=1,
                        message_text="pending",
                        claimed_by_replica="live-owner"
                        if change == "claimed"
                        else None,
                    )
                )
            elif change == "decision":
                controls._audit(
                    db,
                    "operator",
                    "decision_claimed",
                    task_id=TASK,
                    receipt_id=RECEIPT,
                    option_key="continue",
                )
            elif change == "funding":
                controls._audit(
                    db, "factory:funding", "funding_review_requested", task_id=TASK
                )
            elif change == "owner":
                db.add(
                    SwarmTask(id="other", task_text="other", conductor_model="terra")
                )
                db.flush()
                db.add(
                    FactoryReceipt(
                        id=18,
                        task_id="other",
                        repo=REPO,
                        issue_number=2,
                        title="other",
                        body="other",
                        url="https://example.invalid/2",
                        actor="operator",
                        state="admitted",
                        policy_json=json.dumps(world.policy),
                        direction_json=json.dumps(
                            {
                                "delivery_branch": f"factory/{TASK}",
                                "delivery_pr_number": NUMBER,
                            }
                        ),
                    )
                )
            else:
                raise AssertionError(change)


CHANGES = [
    "head",
    "base",
    "draft",
    "closed",
    "branch",
    "ci",
    "ci_replaced",
    "hold",
    "pause",
    "cancel",
    "human_label",
    "generation",
    "policy",
    "policy_roundtrip",
    "receipt_body",
    "work_item",
    "task",
    "review",
    "review_replaced",
    "review_scope",
    "start",
    "claimed",
    "pending",
    "decision",
    "funding",
    "owner",
]


@pytest.mark.parametrize("change", CHANGES)
def test_apply_refuses_changed_evidence_and_preserves_unrelated_state(world, change):
    proposal = preview()
    mutate(world, change)
    with pytest.raises(ValueError):
        apply(proposal)
    assert not audits(world, releases.SCOPE_ACTION)
    with Session(world.db) as db:
        row = db.get(FactoryReceipt, RECEIPT)
        assert "landing_authorization" not in json.loads(row.direction_json or "{}")
        if change == "hold":
            assert controls.landing_held(row)


@pytest.mark.parametrize("change", CHANGES)
def test_current_grant_refuses_drift_after_apply(world, change):
    apply(preview())
    mutate(world, change)
    assert refusal(world) is not None


@pytest.mark.parametrize(
    "change", ["missing", "path", "base", "invalid", "not_independent"]
)
def test_typed_native_review_is_required_and_bound_to_exact_diff(world, change):
    with Session(world.db) as db, db.begin():
        run = db.get(SwarmNodeRun, 200)
        value = copy.deepcopy(world.typed)
        if change == "missing":
            value = None
        elif change == "path":
            value["paths"] = ["different.py"]
        elif change == "base":
            value["base_sha"] = TIP
        elif change == "invalid":
            value["security_sensitive"] = "yes"
        else:
            run.session_id = 10
        run.outcome_json = outcome(value)
    with pytest.raises(
        ValueError, match="operator_landing_(review_scope|independent_review)"
    ):
        preview()


@pytest.mark.parametrize(
    "change",
    [
        "truncated",
        "duplicate",
        "missing",
        "head",
        "non_advisory_failure",
        "missing_state",
        "missing_identity",
    ],
)
def test_ci_must_be_complete_current_and_successful(world, change):
    if change == "truncated":
        world.checks["total_count"] = 2
    elif change == "duplicate":
        world.checks["statuses"] *= 2
        world.checks["total_count"] = 2
    elif change == "missing":
        world.checks.pop("total_count")
    elif change == "head":
        world.checks["sha"] = BASE
    elif change == "missing_state":
        world.checks["statuses"][0].pop("state")
    elif change == "missing_identity":
        world.checks["statuses"][0].pop("id")
    else:
        world.checks["statuses"].append(
            {"context": "required-extra", "state": "failure"}
        )
        world.checks["total_count"] = 2
    with pytest.raises(ValueError, match="operator_landing_ci_unverified"):
        preview()


@pytest.mark.parametrize(
    "change", ["actor", "audit", "git", "review", "scope", "binding"]
)
def test_tampered_grant_cannot_self_attest_new_evidence(world, change):
    apply(preview())
    with Session(world.db) as db, db.begin():
        row = db.get(FactoryReceipt, RECEIPT)
        direction = json.loads(row.direction_json)
        grant = direction["landing_authorization"]
        if change == "actor":
            grant["actor"] = "forged"
        elif change == "audit":
            grant["authorization_audit_id"] += 1
        elif change == "git":
            grant["operator_evidence"]["git"]["base_tip_sha"] = HEAD
        elif change == "review":
            grant["operator_evidence"]["review"]["artifact_sha256"] = "f" * 64
        elif change == "scope":
            grant["scope"]["expires_at"] = (NOW + timedelta(hours=20)).isoformat()
        else:
            grant["operator_evidence"]["binding"]["generation"] += 1
        row.direction_json = json.dumps(direction)
    assert refusal(world) == "operator_landing_grant_unaudited"


def test_revoke_is_a_tombstone_and_lost_response_replay_cannot_revoke_new_grant(world):
    first = apply(preview())
    result = revoke(first)
    assert result["ok"]
    assert refusal(world) == "operator_landing_grant_revoked"
    with Session(world.db) as db:
        row = db.get(FactoryReceipt, RECEIPT)
        assert releases.delegation_owner(row) is None
        assert (
            json.loads(row.direction_json)["landing_authorization"]["revocation"][
                "actor"
            ]
            == "second-operator"
        )
    second = apply(preview(), key="approval-2")
    assert second["authorization_audit_id"] != first["authorization_audit_id"]
    assert revoke(first) == result
    assert refusal(world) is None
    with pytest.raises(ValueError, match="revoke_target_changed"):
        revoke(first, key="old-target-new-request")
    assert refusal(world) is None


def test_revocation_durable_audit_prevents_restoring_old_grant_bytes(world):
    result = apply(preview())
    revoke(result)
    with Session(world.db) as db, db.begin():
        row = db.get(FactoryReceipt, RECEIPT)
        row.direction_json = json.dumps(
            {"landing_authorization": result["landing_authorization"]}
        )
    assert refusal(world) == "operator_landing_grant_revoked"


def test_revoke_is_available_while_held_expired_and_github_unavailable(
    world, monkeypatch
):
    result = apply(preview())
    mutate(world, "hold")
    world.clock[0] += timedelta(days=1)
    monkeypatch.setattr(
        conductor,
        "github_get",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("revocation must not read GitHub")
        ),
    )
    assert revoke(result)["ok"]
    with Session(world.db) as db:
        row = db.get(FactoryReceipt, RECEIPT)
        assert controls.landing_held(row)
        assert json.loads(row.direction_json)["landing_authorization"]["revocation"]


def test_concurrent_apply_has_one_winner_and_audit_failure_is_atomic(world):
    proposal = preview()

    def authorize(key):
        try:
            return apply(proposal, key=key)["ok"]
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(authorize, ["first", "second"])) == [False, True]
    assert len(audits(world, releases.SCOPE_ACTION)) == 1
    assert refusal(world) is None


def test_audit_failure_rolls_back_authority_and_version(world):
    proposal = preview()

    def reject_audit(session, *_args):
        if any(
            isinstance(row, FactoryAudit) and row.action == releases.SCOPE_ACTION
            for row in session.new
        ):
            raise ValueError("simulated audit failure")

    event.listen(Session, "before_flush", reject_audit)
    try:
        with pytest.raises(ValueError, match="simulated audit failure"):
            apply(proposal)
    finally:
        event.remove(Session, "before_flush", reject_audit)
    with Session(world.db) as db:
        assert not json.loads(
            db.get(FactoryReceipt, RECEIPT).direction_json or "{}"
        ).get("landing_authorization")
        assert db.get(FactoryControl, "factory").version == proposal["expected_version"]


@pytest.mark.parametrize("unknown_response", [False, True])
def test_native_arm_has_one_durable_attempt_even_when_response_is_unknown(
    world, unknown_response
):
    apply(preview())
    world.fail_arm[0] = unknown_response
    landing._arm(REPO, item())
    assert world.calls.count(landing._ARM_AUTO_MERGE) == 1
    assert len(audits(world, mod.ARM_ACTION)) == 1
    landing._arm(REPO, item())
    assert world.calls.count(landing._ARM_AUTO_MERGE) == 1
    assert len(audits(world, mod.ARM_ACTION)) == 1
    # The actual queue may have accepted the timed-out call. Observation can
    # retain that exact valid admission without replaying the mutation.
    assert not landing._fence_ungranted_queue_entry(REPO, item())
    assert len(audits(world, mod.QUEUE_ACTION)) == 1


@pytest.mark.parametrize("race", ["expiry", "hold", "revoke", "base"])
def test_final_locked_arm_refuses_drift_after_durable_intent(world, monkeypatch, race):
    result = apply(preview())
    original = mod.reserve_arm

    def reserve(*args):
        intent = original(*args)
        if race == "expiry":
            world.clock[0] += timedelta(hours=1)
        elif race == "revoke":
            revoke(result)
        else:
            mutate(world, race)
        return intent

    monkeypatch.setattr(mod, "reserve_arm", reserve)
    landing._arm(REPO, item())
    assert not world.calls
    assert len(audits(world, mod.ARM_ACTION)) == 1


def test_final_read_cooldown_preserves_the_single_operator_mutation_attempt(
    world, monkeypatch
):
    from factory.orchestration.github_read_cooldown import GitHubReadDeferred

    apply(preview())
    original = mod.reserve_arm
    read = landing.github_get

    def reserve(*args):
        intent = original(*args)

        def deferred(*_args):
            raise GitHubReadDeferred("final read, no arming write")

        monkeypatch.setattr(landing, "github_get", deferred)
        return intent

    monkeypatch.setattr(mod, "reserve_arm", reserve)
    landing._arm(REPO, item())
    assert not world.calls
    assert len(audits(world, mod.ARM_ACTION)) == 1
    assert len(audits(world, "operator_landing_arm_read_deferred")) == 1
    monkeypatch.setattr(landing, "github_get", read)
    monkeypatch.setattr(mod, "reserve_arm", original)
    landing._arm(REPO, item())
    assert world.calls.count(landing._ARM_AUTO_MERGE) == 1
    landing._arm(REPO, item())
    assert world.calls.count(landing._ARM_AUTO_MERGE) == 1


def test_unreserved_manual_queue_is_fenced_even_with_an_operator_grant(world):
    apply(preview())
    world.repo.pull.update(
        auto_merge={"merge_method": "REBASE"}, queue_entry={"id": "MQ_manual"}
    )
    assert landing._fence_ungranted_queue_entry(REPO, item())
    assert (
        world.repo.pull["auto_merge"] is None and world.repo.pull["queue_entry"] is None
    )
    assert world.calls == [landing._DISARM_AUTO_MERGE, landing._DEQUEUE_PULL_REQUEST]


@pytest.mark.parametrize(
    "change", ["revoke", "expiry", "base", "ci", "review", "hold", "second_entry"]
)
def test_queue_observer_dequeues_invalid_or_repeated_admission(world, change):
    result = apply(preview())
    landing._arm(REPO, item())
    assert not landing._fence_ungranted_queue_entry(REPO, item())
    if change == "revoke":
        revoke(result)
    elif change == "expiry":
        world.clock[0] += timedelta(hours=1)
    elif change == "second_entry":
        world.repo.pull["queue_entry"] = {"id": "MQ_2"}
    else:
        mutate(world, change)
    assert landing._fence_ungranted_queue_entry(REPO, item())
    assert (
        world.repo.pull["auto_merge"] is None and world.repo.pull["queue_entry"] is None
    )
    assert world.calls.count(landing._ARM_AUTO_MERGE) == 1


@pytest.mark.parametrize("later", [None, "untyped", "invalid", "sensitive", "private"])
def test_typed_operator_assessment_cannot_be_downgraded_via_legacy_scope(world, later):
    if later is not None:
        assessment = copy.deepcopy(world.typed)
        if later == "untyped":
            assessment = None
        elif later == "invalid":
            assessment["security_sensitive"] = "yes"
        elif later == "private":
            assessment.update(
                classification="private_internal_reversible",
                **{key: False for key in mod.EFFECTS},
            )
        with Session(world.db) as db, db.begin():
            db.add(
                SwarmNodeRun(
                    id=201,
                    task_id=TASK,
                    node_key="review_reassess",
                    attempt=1,
                    session_id=21,
                    status="succeeded",
                    head_sha=HEAD,
                    outcome_json=outcome(assessment),
                )
            )
    private = {
        "classification": "private_internal_reversible",
        "evidence": "Operator's private assessment",
    }
    with pytest.raises(
        ValueError,
        match="operator_landing_(private_scope_downgrade|review_scope_missing_or_invalid)",
    ):
        releases.preview(RECEIPT, NUMBER, HEAD, "private", scope=private)


def test_legacy_untyped_reviews_retain_private_grant_behavior(world):
    with Session(world.db) as db, db.begin():
        db.get(SwarmNodeRun, 200).outcome_json = outcome(None)
    private = {
        "classification": "private_internal_reversible",
        "evidence": "Legacy ordinary private delivery",
    }
    proposal = releases.preview(RECEIPT, NUMBER, HEAD, "private", scope=private)
    result = releases.apply(
        RECEIPT,
        NUMBER,
        HEAD,
        "private",
        scope=private,
        actor="joe",
        request_key="legacy",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    assert result["landing_authorization"]["schema_version"] == 1
    assert refusal(world) is None
    with Session(world.db) as db, db.begin():
        db.get(SwarmNodeRun, 200).outcome_json = outcome(world.typed)
    assert refusal(world) is not None
