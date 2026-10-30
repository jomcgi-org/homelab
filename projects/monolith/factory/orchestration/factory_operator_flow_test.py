"""Real MCP-to-ledger flow, with only GitHub and quota I/O replaced by fixtures.

These are local synthetic receipts, never calls against live factory records.
Restriction and continuation remain separate operator actions and digests.
"""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind
from fastmcp import Client
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_conductor as conductor
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_funding_limits as funding
from factory.orchestration import factory_progress_continuation as progress
from factory.orchestration import mcp
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

NOW = datetime(2026, 10, 30, 10, tzinfo=UTC)
EXPIRED = (NOW - timedelta(hours=4)).isoformat()
TARGETS = {927: (7436, "a" * 40, 6), 1027: (7433, "b" * 40, 7)}
REMOVE = {"remove_models": ["spark"]}
REASON = "Restrict the unused model. This does not authorize continuation."
CONTINUE = "Explicitly authorize this saved artifact under finite progress leases."
IMMUTABLE = (
    SwarmTask,
    SwarmPlanVersion,
    SwarmPlanNode,
    SwarmNodeRun,
    FactoryStart,
    AgentSession,
)


def principal(authority=Authority.STANDING):
    return Principal(
        subject="operator",
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=authority,
    )


def call(operation, receipt_id, overlay, direction, **kwargs):
    """Use the advertised JSON schema and actual authenticated MCP transport."""

    async def run():
        async with Client(mcp.mcp) as client:
            result = await client.call_tool(
                "factory_amend_policy",
                {
                    "operation": operation,
                    "receipt_id": receipt_id,
                    "overlay": overlay,
                    "direction": direction,
                    **kwargs,
                },
            )
            return result.structured_content

    token = set_current_principal(kwargs.pop("principal", principal()))
    try:
        return asyncio.run(run())
    finally:
        reset_current_principal(token)


def continuation(receipt_id):
    pr, head, _ = TARGETS[receipt_id]
    return {"progress_continuation": {"pr_number": pr, "head_sha": head}}


def ledger(engine):
    with Session(engine) as db:
        return {
            model.__name__: [
                r.model_dump(mode="json") for r in db.exec(select(model)).all()
            ]
            for model in (*IMMUTABLE, PendingMessage)
        }


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operator-flow.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    tables = (*IMMUTABLE, PendingMessage, FactoryAudit, FactoryControl, FactoryReceipt)
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in tables])
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    policy = {
        "repo": "org/repo",
        "issue_numbers": [7153, 7428],
        "generation": 14,
        "max_tasks": 10,
        "max_turns_per_task": 8,
        "max_task_turns_hard": 8,
        "max_planner_turns": 8,
        "task_budget_usd": 50,
        "turn_budget_usd": 4,
        "allowed_models": ["astra", "sol", "spark", "opus"],
        "conductor_model": "astra",
        "worker_model": "astra",
        "reviewer_model": "opus",
        "model_pools": {
            "worker": ["astra", "spark"],
            "implement": ["astra", "spark"],
            "refine": ["astra", "spark"],
            "conductor": ["astra"],
            "reviewer": ["opus"],
        },
        "base_branch": "main",
        "max_attempts": 2,
        "task_timeout_seconds": 7200,
        "turn_timeout_seconds": 3600,
        "auto_merge": False,
    }
    pulls = {}
    with Session(engine) as db, db.begin():
        db.add(
            FactoryControl(
                id="factory",
                state="enabled",
                version=269,
                actor="operator",
                policy_json=json.dumps(policy),
            )
        )
        for receipt_id in (*TARGETS, 996):
            task = f"task-{receipt_id}"
            db.add(
                SwarmTask(
                    id=task,
                    task_text="Preserve saved artifact",
                    repo="org/repo",
                    base_branch="main",
                    conductor_model="astra",
                    budget_usd=50,
                    start_state="factory",
                    created_at=NOW - timedelta(hours=8),
                )
            )
            db.add(
                FactoryReceipt(
                    id=receipt_id,
                    task_id=task,
                    repo="org/repo",
                    issue_number=receipt_id,
                    title="Saved work",
                    body="Keep scope",
                    url="",
                    actor="operator",
                    state="admitted",
                    generation=14,
                    task_paused=True,
                    policy_json=json.dumps(policy),
                    direction_json=json.dumps(
                        {
                            "landing_hold": {"reason": "draft only"},
                            "nonrenewable_recovery": {"reason": "original grant"},
                            "conductor_gates": [
                                {"scope": "retain security and public holds"}
                            ],
                        }
                    ),
                )
            )
            db.add(
                SwarmPlanVersion(
                    task_id=task,
                    version=1,
                    op="bootstrap",
                    author_kind="human",
                    author="operator",
                    change_json="{}",
                    cause_kind="initial",
                )
            )
            if receipt_id == 996:
                # A later non-invocation does not settle the original unknown start.
                db.add_all(
                    [
                        FactoryStart(
                            task_id=task,
                            start_key=f"factory-node:{task}:original:1",
                            actor="factory",
                            model="astra",
                            status="uncertain",
                            max_cost_usd=4,
                        ),
                        FactoryStart(
                            task_id=task,
                            start_key=f"factory-node:{task}:later:1",
                            actor="factory",
                            model="astra",
                            status="failed",
                            max_cost_usd=4,
                            accounting_basis="no_model_post",
                        ),
                    ]
                )
                continue
            number, head, work = TARGETS[receipt_id]
            pulls[number] = {
                "state": "open",
                "draft": True,
                "head": {
                    "sha": head,
                    "ref": f"factory/{task}",
                    "repo": {"full_name": "org/repo"},
                },
                "base": {"ref": "main"},
            }
            for index, (node, model) in enumerate(
                (("implement_1", "astra"), ("review_1", "opus")), 1
            ):
                session_id = receipt_id * 10 + index
                artifact = {"pr_number": number, "head_sha": head}
                if node.startswith("review"):
                    artifact.update(
                        verdict="changes_requested",
                        summary="Correct the saved artifact",
                    )
                db.add_all(
                    [
                        SwarmPlanNode(
                            task_id=task,
                            node_key=node,
                            kind="agent",
                            prompt="Continue saved work",
                            model=model,
                            deps_json="[]",
                            max_cost_usd=4,
                            side_effects=False,
                            created_in_version=1,
                        ),
                        AgentSession(
                            id=session_id,
                            local_session_id=f"factory:{task}:{node}:1",
                            workspace="guest",
                            branch=f"factory/{task}",
                            status="completed",
                            model=model,
                        ),
                        SwarmNodeRun(
                            task_id=task,
                            node_key=node,
                            attempt=1,
                            status="succeeded",
                            model=model,
                            pin_json=json.dumps({"model": model}),
                            session_id=session_id,
                            head_sha=head,
                            cost_usd=1,
                            outcome_json=json.dumps({"artifact": artifact}),
                        ),
                    ]
                )
            db.add_all(
                [
                    FactoryStart(
                        task_id=task,
                        start_key=f"factory-node:{task}:past_{i}:1",
                        actor="factory",
                        model="spark" if i == 0 else "astra",
                        status="succeeded",
                        cost_usd=1,
                        max_cost_usd=4,
                    )
                    for i in range(work)
                ]
            )
            db.add_all(
                [
                    FactoryStart(
                        task_id=task,
                        start_key=f"factory-node:{task}:conductor_{i}:1",
                        actor="factory",
                        model="astra",
                        status="succeeded",
                        cost_usd=0.2,
                        max_cost_usd=4,
                    )
                    for i in range(7)
                ]
            )
            controls._audit(
                db,
                "operator",
                controls.TASK_RECOVERY_ACTION,
                task_id=task,
                proposal={
                    "deadline_at": EXPIRED,
                    "work_turn_ceiling": work + 1,
                    "planner_turn_ceiling": 8,
                    "expected_revision": 1,
                },
            )
            controls._audit(
                db,
                "operator",
                "funding_granted",
                task_id=task,
                policy_overlay={"task_budget_usd": 50},
                deadline_at=EXPIRED,
                review_due_at=EXPIRED,
                next_plan="Preserve original grant",
            )
    observations = {"github": [], "quota": []}
    quota = {
        "available": True,
        "providers": {
            provider: {
                "observed": True,
                "observed_at": NOW.isoformat(),
                "age_seconds": 0,
                "status": "allowed",
                "windows": [
                    {
                        "name": "primary",
                        "used_percent": 20,
                        "expired": False,
                        "resets_at": (NOW + timedelta(days=1)).isoformat(),
                    }
                ],
            }
            for provider in ("codex", "claude")
        },
    }
    checks = {
        "state": "success",
        "statuses": [{"context": "pr-checks", "state": "success"}],
    }

    def github(repo, path):
        observations["github"].append((repo, path))
        assert repo == "org/repo"
        if path.startswith("issues/"):
            return {"state": "open", "labels": []}
        if path.startswith("pulls/"):
            return deepcopy(pulls[int(path.split("/")[1])])
        assert path.startswith("commits/") and path.endswith("/status")
        return deepcopy(checks)

    def observe(**kwargs):
        observations["quota"].append(kwargs)
        return deepcopy(quota)

    monkeypatch.setattr(conductor, "github_get", github)
    monkeypatch.setattr(progress.provider_quota, "fetch_provider_quota_sync", observe)
    yield engine, pulls, checks, quota, observations
    engine.dispose()


def restrict(receipt_id):
    preview = call("preview", receipt_id, REMOVE, REASON)
    assert preview["restriction_only"]
    return call(
        "apply",
        receipt_id,
        REMOVE,
        REASON,
        request_key=f"remove-{receipt_id}",
        expected_preview_sha256=preview["preview_sha256"],
    )


@pytest.mark.parametrize("receipt_id", TARGETS)
def test_restriction_then_separate_continuation_preserves_same_artifact(
    flow, receipt_id
):
    engine, _, _, _, observed = flow
    before = ledger(engine)
    refused = call("preview", receipt_id, continuation(receipt_id), CONTINUE)
    assert refused["reason"] == "progress_subscription_evidence_stale"
    assert (
        refused["subscription_diagnostics"]["refused_model"]["state"]
        == "unsupported_subscription_feed"
    )
    with Session(engine) as db:
        original = funding.latest(
            db, f"task-{receipt_id}", controls.TASK_RECOVERY_ACTION
        )
        grant = funding.amendment(db, f"task-{receipt_id}")
    removed = restrict(receipt_id)
    assert removed["ok"] and ledger(engine) == before
    with Session(engine) as db:
        row = db.get(FactoryReceipt, receipt_id)
        assert row.task_paused and controls.landing_held(row)
        assert funding.amendment(db, row.task_id) == grant
        assert (
            controls.task_recovery_grant(row.task_id, session=db)["deadline_at"]
            == EXPIRED
        )
        assert "spark" in json.loads(row.policy_json)["allowed_models"]
        assert "spark" not in funding.effective_policy(db, row)["allowed_models"]
        direction = row.direction_json
    preview = call("preview", receipt_id, continuation(receipt_id), CONTINUE)
    assert "preview_sha256" in preview, preview
    assert preview["expected_version"] == removed["version"]
    assert preview["original_recovery"] == original
    # Neither a successful removal nor a continuation preview resumes the task.
    with Session(engine) as db:
        assert db.get(FactoryReceipt, receipt_id).task_paused
    wrong = call(
        "apply",
        receipt_id,
        continuation(receipt_id),
        CONTINUE,
        request_key="not-removal-authority",
        expected_preview_sha256="0" * 64,
    )
    assert wrong["reason"] == "progress_preview_changed"
    args = {
        "request_key": f"continue-{receipt_id}",
        "expected_preview_sha256": preview["preview_sha256"],
    }
    result = call("apply", receipt_id, continuation(receipt_id), CONTINUE, **args)
    assert result["ok"] and result["landing_hold"]
    assert result["lease"]["deadline_at"] == (NOW + timedelta(hours=2)).isoformat()
    reads = len(observed["quota"])
    assert (
        call("apply", receipt_id, continuation(receipt_id), CONTINUE, **args) == result
    )
    assert len(observed["quota"]) == reads
    assert observed["quota"] and all(v == {"force": True} for v in observed["quota"])
    assert (
        ledger(engine) == before
    )  # No new invocation, graph, accounting or unknown retry.
    with Session(engine) as db:
        row = db.get(FactoryReceipt, receipt_id)
        assert not row.task_paused and row.direction_json == direction
        assert row.state == "admitted" and controls.landing_held(row)
        assert (
            funding.latest(db, row.task_id, controls.TASK_RECOVERY_ACTION) == original
        )
        assert db.get(FactoryReceipt, 996).task_paused
        assert db.get(FactoryReceipt, 1027 if receipt_id == 927 else 927).task_paused
        assert db.get(FactoryControl, "factory").version == 271


@pytest.mark.parametrize(
    "problem",
    [
        "version",
        "head",
        "repo",
        "ci",
        "stale_codex",
        "missing_claude",
        "uncertain",
        "pending",
        "receipt",
        "revision",
        "grant",
        "funding",
        "hold",
        "review",
        "capacity",
        "exhausted_claude",
    ],
)
def test_fresh_continuation_apply_rechecks_every_boundary_after_removal(
    flow, monkeypatch, problem
):
    engine, pulls, checks, quota, _ = flow
    restrict(927)
    if problem == "receipt":
        restrict(1027)
    preview = call("preview", 927, continuation(927), CONTINUE)
    assert "preview_sha256" in preview, preview
    receipt_id = 927
    with Session(engine) as db, db.begin():
        if problem == "version":
            db.get(FactoryControl, "factory").version += 1
        elif problem == "uncertain":
            db.exec(
                select(FactoryStart).where(FactoryStart.task_id == "task-927")
            ).first().status = "uncertain"
        elif problem == "revision":
            db.add(
                SwarmPlanVersion(
                    task_id="task-927",
                    version=2,
                    op="replace",
                    author_kind="human",
                    author="operator",
                    change_json="{}",
                    cause_kind="initial",
                )
            )
        elif problem == "grant":
            controls._audit(
                db,
                "operator",
                controls.TASK_RECOVERY_ACTION,
                task_id="task-927",
                proposal={
                    "deadline_at": EXPIRED,
                    "work_turn_ceiling": 8,
                    "planner_turn_ceiling": 8,
                    "expected_revision": 1,
                },
            )
        elif problem == "funding":
            controls._audit(
                db,
                "operator",
                "funding_granted",
                task_id="task-927",
                policy_overlay={"task_budget_usd": 49},
                deadline_at=EXPIRED,
                review_due_at=EXPIRED,
                next_plan="Changed funding is not reviewed",
            )
        elif problem == "hold":
            row = db.get(FactoryReceipt, 927)
            direction = json.loads(row.direction_json)
            direction["conductor_gates"].append({"scope": "new human decision"})
            row.direction_json = json.dumps(direction)
        elif problem == "review":
            db.get(AgentSession, 9272).local_session_id = "factory:foreign:review_1:1"
        elif problem == "pending":
            db.add(
                PendingMessage(session_id=9271, seq=2, message_text="not dispatched")
            )
    if problem == "head":
        pulls[7436]["head"]["sha"] = "c" * 40
    elif problem == "repo":
        pulls[7436]["head"]["repo"]["full_name"] = "foreign/repo"
    elif problem == "ci":
        checks["state"] = checks["statuses"][0]["state"] = "pending"
    elif problem == "stale_codex":
        quota["providers"]["codex"]["age_seconds"] = 901
    elif problem == "missing_claude":
        quota["providers"].pop("claude")
    elif problem == "receipt":
        receipt_id = 1027
    elif problem == "capacity":
        # The unresolved original keeps the sole execution slot.
        monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "1")
    elif problem == "exhausted_claude":
        quota["providers"]["claude"]["windows"][0]["used_percent"] = 100
    before = ledger(engine)
    result = call(
        "apply",
        receipt_id,
        continuation(receipt_id),
        CONTINUE,
        request_key="changed",
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert result["ok"] is False, result
    assert ledger(engine) == before
    with Session(engine) as db:
        assert db.get(FactoryReceipt, receipt_id).task_paused
        assert funding.latest(db, f"task-{receipt_id}", progress.ACTION) is None


def test_removal_cannot_resume_or_reconcile_an_unknown_original(flow):
    engine, *_ = flow
    before = ledger(engine)
    refused = call("preview", 996, REMOVE, REASON)
    assert refused["reason"] == "amendment_unresolved_execution"
    assert ledger(engine) == before


@pytest.mark.parametrize("overlay", [REMOVE, continuation(927)])
def test_delegated_execution_identity_cannot_grant_operator_authority(flow, overlay):
    engine, *_ = flow
    before = ledger(engine)
    refused = call(
        "preview", 927, overlay, CONTINUE, principal=principal(Authority.DELEGATED)
    )
    assert refused["error"] == mcp.OPERATOR_REQUIRED
    assert ledger(engine) == before


def staged_gate(reason="Retain repository-only scope"):
    return {
        "kind": "live_validation",
        "classification": "reversible",
        "reason": reason,
        "scope": "Repository-only saved PR. No merge, deployment or production access.",
        "leave_unmerged": True,
        "tracked_in": 7153,
        "live_checks": ["Separately authorized rollout and real-host checks"],
        "effects": {
            "spends_money": False,
            "deletes_data": False,
            "touches_external_account": False,
            "uses_credentials": False,
        },
    }


@pytest.fixture
def renewed_review(flow):
    """The v269 restriction, v271 grant, and completed correction/review lineage."""
    engine, pulls, _, _, _ = flow
    task = "task-927"
    with Session(engine) as db, db.begin():
        row = db.get(FactoryReceipt, 927)
        direction = json.loads(row.direction_json)
        direction["conductor_gates"] = [
            staged_gate(f"Earlier hold {i}") for i in range(4)
        ]
        row.direction_json = json.dumps(direction)
    restrict(927)
    preview = call("preview", 927, continuation(927), CONTINUE)
    applied = call(
        "apply",
        927,
        continuation(927),
        CONTINUE,
        request_key="initial-927",
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert applied["version"] == 271
    assert applied["lease"]["work_turn_ceiling"] == 8
    head = "c" * 40
    pulls[7436]["head"]["sha"] = head
    with Session(engine) as db, db.begin():
        row = db.get(FactoryReceipt, 927)
        direction = json.loads(row.direction_json)
        direction["conductor_gates"].append(staged_gate("Reaffirm the existing hold"))
        row.direction_json = json.dumps(direction)
        for index, (node, model) in enumerate(
            (("correct_2", "astra"), ("review_2", "opus")), 1
        ):
            artifact = {"pr_number": 7436, "head_sha": head}
            if node.startswith("review"):
                artifact.update(
                    verdict="changes_requested", summary="Fix CI colour vocabulary"
                )
            db.add_all(
                [
                    AgentSession(
                        id=10000 + index,
                        local_session_id=f"factory:{task}:{node}:1",
                        workspace="guest",
                        branch=f"factory/{task}",
                        status="completed",
                        model=model,
                    ),
                    SwarmNodeRun(
                        task_id=task,
                        node_key=node,
                        attempt=1,
                        status="succeeded",
                        model=model,
                        pin_json=json.dumps({"model": model}),
                        session_id=10000 + index,
                        head_sha=head,
                        cost_usd=1,
                        outcome_json=json.dumps({"artifact": artifact}),
                    ),
                    FactoryStart(
                        task_id=task,
                        start_key=f"factory-node:{task}:{node}:1",
                        actor="factory",
                        model=model,
                        status="succeeded",
                        cost_usd=1,
                        max_cost_usd=4,
                    ),
                ]
            )
    return flow, {"progress_renewal": {"pr_number": 7436, "head_sha": head}}


def test_progress_renewal_preserves_appended_staged_hold_and_funds_correction(
    renewed_review,
):
    (engine, _, _, _, _), _ = renewed_review
    before = ledger(engine)
    with Session(engine) as db:
        direction = db.get(FactoryReceipt, 927).direction_json
    assert progress.renew("task-927")
    assert (
        ledger(engine) == before
    )  # No new dispatch, model run or accounting identity.
    with Session(engine) as db:
        lease = funding.latest(db, "task-927", progress.LEASE)
        assert lease["work_turn_ceiling"] == 10
        assert lease["consecutive_failures"] == 1
        assert db.get(FactoryReceipt, 927).direction_json == direction
        assert len(json.loads(direction)["conductor_gates"]) == 5
        assert db.get(FactoryControl, "factory").version == 272
        assert db.get(FactoryReceipt, 1027).task_paused
        assert db.get(FactoryReceipt, 996).task_paused
    assert not progress.renew("task-927")  # Same reviewed head is never renewed twice.


@pytest.mark.parametrize(
    "change", ["remove", "weaken", "scope", "effect", "authority", "checks", "untyped"]
)
def test_progress_renewal_refuses_changed_boundaries(renewed_review, change):
    (engine, _, _, _, _), _ = renewed_review
    with Session(engine) as db, db.begin():
        row = db.get(FactoryReceipt, 927)
        direction = json.loads(row.direction_json)
        gates = direction["conductor_gates"]
        if change == "remove":
            gates.pop(0)
        elif change == "weaken":
            gates[-1]["leave_unmerged"] = False
        elif change == "scope":
            gates[-1]["scope"] = "Deploy a new public endpoint"
        elif change == "effect":
            gates[-1]["effects"]["spends_money"] = True
        elif change == "authority":
            direction["task_recovery"] = {"reason": "replacement authority"}
        elif change == "checks":
            gates[-1]["live_checks"] = ["Unrelated check"]
        else:
            gates[-1]["unknown"] = "new instruction"
        row.direction_json = json.dumps(direction)
    before = ledger(engine)
    assert not progress.renew("task-927")
    assert ledger(engine) == before
    with Session(engine) as db:
        assert funding.latest(db, "task-927", progress.LEASE)["work_turn_ceiling"] == 8
        diagnostic = progress.renewal_status(db, "task-927")
        assert diagnostic["reason"] == "progress_authority_changed"
        assert diagnostic["head_sha"] == "c" * 40
        assert diagnostic["review_id"] is not None
    # Repeated observation must not flood the durable audit.
    assert not progress.renew("task-927")
    with Session(engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == progress.REFUSAL)
                ).all()
            )
            == 1
        )


def pause_allocation(engine):
    assert controls.pause_nonrenewable_recovery(
        "task-927",
        'Correction plan exceeds current allocation: envelope exceeded: {"turns":{"allowed":8,"needed":10}}',
    )
    with Session(engine) as db:
        assert db.get(FactoryReceipt, 927).task_paused


def test_exact_operator_renewal_recovers_allocation_pause_without_regrant(
    renewed_review,
):
    (engine, _, _, _, observations), overlay = renewed_review
    pause_allocation(engine)
    before = ledger(engine)
    with Session(engine) as db:
        direction = db.get(FactoryReceipt, 927).direction_json
        original = funding.latest(db, "task-927", progress.ACTION)
    preview = call(
        "preview",
        927,
        overlay,
        "Reconcile the existing continuation grant with retained holds",
    )
    assert preview["resume_paused"] and preview["expected_version"] == 272
    assert preview["work_turn_ceiling"] == 10
    assert ledger(engine) == before
    with Session(engine) as db:
        assert db.get(FactoryReceipt, 927).task_paused
    args = {
        "request_key": "renew-927",
        "expected_preview_sha256": preview["preview_sha256"],
    }
    result = call(
        "apply",
        927,
        overlay,
        "Reconcile the existing continuation grant with retained holds",
        **args,
    )
    assert result["ok"] and result["version"] == 273
    reads = len(observations["quota"])
    assert (
        call(
            "apply",
            927,
            overlay,
            "Reconcile the existing continuation grant with retained holds",
            **args,
        )
        == result
    )
    assert len(observations["quota"]) == reads
    assert ledger(engine) == before
    with Session(engine) as db:
        assert funding.latest(db, "task-927", progress.ACTION) == original
        row = db.get(FactoryReceipt, 927)
        assert not row.task_paused and row.direction_json == direction
        assert (
            db.get(FactoryReceipt, 1027).task_paused
            and db.get(FactoryReceipt, 996).task_paused
        )
        assert funding.effective_policy(db, row)["task_budget_usd"] == 50
        assert (
            json.loads(db.get(FactoryControl, "factory").policy_json)["generation"]
            == 14
        )


@pytest.mark.parametrize(
    "change",
    ["operator_pause", "version", "head", "quota", "uncertain", "hold", "grant"],
)
def test_operator_renewal_rechecks_pause_identity_artifact_and_quota(
    renewed_review, change
):
    (engine, pulls, _, quota, _), overlay = renewed_review
    pause_allocation(engine)
    proposal = call("preview", 927, overlay, "Renew existing authority")
    assert "preview_sha256" in proposal
    if change == "operator_pause":
        assert controls.set_control("pause_task", "human", task_id="task-927")["ok"]
    elif change == "head":
        pulls[7436]["head"]["sha"] = "d" * 40
    elif change == "quota":
        quota["providers"]["claude"]["age_seconds"] = 901
    else:
        with Session(engine) as db, db.begin():
            if change == "version":
                db.get(FactoryControl, "factory").version += 1
            elif change == "uncertain":
                db.exec(
                    select(FactoryStart).where(FactoryStart.task_id == "task-927")
                ).first().status = "uncertain"
            elif change == "hold":
                row = db.get(FactoryReceipt, 927)
                direction = json.loads(row.direction_json)
                direction["landing_hold"] = {"reason": "changed"}
                row.direction_json = json.dumps(direction)
            else:
                controls._audit(
                    db,
                    "other",
                    controls.TASK_RECOVERY_ACTION,
                    task_id="task-927",
                    proposal={},
                )
    before = ledger(engine)
    result = call(
        "apply",
        927,
        overlay,
        "Renew existing authority",
        request_key="refused-renewal",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    assert result["ok"] is False
    assert ledger(engine) == before
    with Session(engine) as db:
        assert db.get(FactoryReceipt, 927).task_paused
        assert funding.latest(db, "task-927", progress.LEASE)["work_turn_ceiling"] == 8


def test_renewed_lease_admits_existing_engine_correction_and_independent_review(
    renewed_review, monkeypatch
):
    from factory.orchestration import graph
    from factory.orchestration.models import SwarmConductorCall

    (engine, _, _, _, _), _ = renewed_review
    monkeypatch.setattr(conductor, "get_engine", lambda: engine)
    monkeypatch.setattr(graph, "get_engine", lambda: engine)
    SQLModel.metadata.create_all(engine, tables=[SwarmConductorCall.__table__])
    with Session(engine) as db, db.begin():
        for node in db.exec(
            select(SwarmPlanNode).where(SwarmPlanNode.task_id == "task-927")
        ).all():
            node.kind = "gate" if node.node_key.startswith("review") else "work"
            node.max_attempts = 1
        db.add_all(
            [
                SwarmNodeRun(
                    task_id="task-927",
                    node_key=f"past_{i}",
                    attempt=1,
                    status="succeeded",
                    model="astra",
                    cost_usd=1,
                    pin_json=json.dumps({"model": "astra"}),
                    outcome_json="{}",
                )
                for i in range(4)
            ]
        )
        db.add_all(
            [
                SwarmPlanNode(
                    task_id="task-927",
                    node_key="correct_2",
                    kind="work",
                    model="astra",
                    prompt="Remove unrelated changes",
                    deps_json='["review_1"]',
                    side_effects=True,
                    max_cost_usd=4,
                    max_attempts=1,
                    created_in_version=1,
                ),
                SwarmPlanNode(
                    task_id="task-927",
                    node_key="review_2",
                    kind="gate",
                    model="opus",
                    prompt="Independent review",
                    deps_json='["correct_2"]',
                    side_effects=False,
                    max_cost_usd=4,
                    max_attempts=1,
                    created_in_version=1,
                ),
            ]
        )
    task = conductor._task("task-927")
    nodes = graph.load_graph("task-927")
    runs = graph.node_runs("task-927")
    review = next(r for r in runs if r["node_key"] == "review_2")
    projected = nodes + [
        {
            "node_key": "correct_3",
            "deps": ["review_2"],
            "max_cost_usd": 4,
            "max_attempts": 1,
        },
        {
            "node_key": "review_3",
            "deps": ["correct_3"],
            "max_cost_usd": 4,
            "max_attempts": 1,
        },
    ]
    with Session(engine) as db:
        old_policy = funding.effective_policy(db, db.get(FactoryReceipt, 927))
    refusal = conductor._envelope_refusal(
        "task-927",
        old_policy,
        projected,
        review_rounds_remaining=0,
        fan_ins_remaining=0,
    )
    assert '"allowed": 8' in refusal and '"needed": 10' in refusal
    assert progress.renew("task-927")
    with Session(engine) as db:
        policy = funding.effective_policy(db, db.get(FactoryReceipt, 927))
    assert conductor._insert_review_round(
        task, policy, nodes, runs, review, 3, 3, 1
    ) == (True, None)
    updated = {n["node_key"]: n for n in graph.load_graph("task-927")}
    assert updated["correct_3"]["deps"] == ["review_2"]
    assert updated["review_3"]["deps"] == ["correct_3"]
    assert updated["correct_3"]["model"] == "astra"
    assert updated["review_3"]["model"] == "opus"
    assert (
        len(graph.node_runs("task-927")) == 8
    )  # Graph insertion never invokes a model.


def test_appended_hold_may_require_more_checks_but_next_lease_cannot_remove_it(
    renewed_review,
):
    (engine, pulls, _, _, _), _ = renewed_review
    with Session(engine) as db, db.begin():
        row = db.get(FactoryReceipt, 927)
        direction = json.loads(row.direction_json)
        direction["conductor_gates"][-1]["live_checks"].append(
            "Additional private-host regression check"
        )
        row.direction_json = json.dumps(direction)
    assert progress.renew("task-927")
    pulls[7436]["head"]["sha"] = "d" * 40
    with Session(engine) as db, db.begin():
        row = db.get(FactoryReceipt, 927)
        direction = json.loads(row.direction_json)
        direction[
            "conductor_gates"
        ].pop()  # This gate was absent from initial authority.
        row.direction_json = json.dumps(direction)
        db.add_all(
            [
                AgentSession(
                    id=10003,
                    local_session_id="factory:task-927:review_3:1",
                    workspace="guest",
                    branch="factory/task-927",
                    status="completed",
                    model="opus",
                ),
                SwarmNodeRun(
                    task_id="task-927",
                    node_key="review_3",
                    attempt=1,
                    status="succeeded",
                    model="opus",
                    pin_json=json.dumps({"model": "opus"}),
                    session_id=10003,
                    head_sha="d" * 40,
                    cost_usd=1,
                    outcome_json=json.dumps(
                        {
                            "artifact": {
                                "pr_number": 7436,
                                "head_sha": "d" * 40,
                                "verdict": "approve",
                                "summary": "Reviewed",
                            }
                        }
                    ),
                ),
            ]
        )
    assert not progress.renew("task-927")
    with Session(engine) as db:
        assert (
            progress.renewal_status(db, "task-927")["reason"]
            == "progress_authority_changed"
        )


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("ci", "progress_ci_failed"),
        ("quota", "progress_subscription_evidence_stale"),
        ("transport", "progress_observation_unavailable"),
    ],
)
def test_renewal_records_exact_observation_refusal_without_grant(
    renewed_review, monkeypatch, fault, reason
):
    import httpx

    (engine, _, checks, quota, _), _ = renewed_review
    if fault == "ci":
        checks["state"] = "failure"
    elif fault == "quota":
        quota["providers"]["claude"]["age_seconds"] = 901
    else:

        def unavailable(*args, **kwargs):
            raise httpx.ConnectError("unavailable")

        monkeypatch.setattr(conductor, "github_get", unavailable)
    before = ledger(engine)
    assert not progress.renew("task-927")
    assert ledger(engine) == before
    with Session(engine) as db:
        status = progress.renewal_status(db, "task-927")
        assert status["reason"] == reason
        assert status["coverage"] == "last_recorded_attempt"
        if fault == "quota":
            assert (
                status["subscription_diagnostics"]["refused_model"]["provider"]
                == "claude"
            )
        assert funding.latest(db, "task-927", progress.LEASE)["work_turn_ceiling"] == 8
