"""Bounded scouting uses one durable receipt and no alternate writer."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_improvement_fence as fence
from factory.orchestration import factory_improvement_scout as scout
from factory.orchestration import factory_improvements as service
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def current():
    cap = {
        "subscription_only": True,
        "overage_disabled": True,
        "evidence": "verified existing subscription config",
    }
    grants = {
        name: {
            "provider": "codex",
            "grant": name,
            "observed": True,
            "observed_at": NOW.isoformat(),
            "status": "allowed",
            "exhausted": False,
            "pacing": {"state": "use_more"},
            "windows": [
                {
                    "name": "weekly",
                    "used_percent": 0,
                    "window_minutes": 10080,
                    "resets_at": (NOW + timedelta(days=2)).isoformat(),
                    "expired": False,
                }
            ],
        }
        for name in ("codex-a", "codex-b")
    }
    return {
        "control_state": "enabled",
        "control_version": 0,
        "repo_enabled": True,
        "intake_complete": True,
        "owners": [],
        "goals": [{"id": 7, "statement": "Reliability"}],
        "improvement_history": [],
        "captured_at": NOW.isoformat(),
        "policy": {
            "repo": service.REPO,
            "generation": 1,
            "task_budget_usd": 50,
            "turn_budget_usd": 10,
            "allowed_models": ["sol"],
            "turn_timeout_seconds": 600,
            "problem_issues": {
                "enabled": True,
                "sources": {"quota_improvement": True},
                "improvement_scout": {
                    "enabled": True,
                    "issue_number": 7000,
                    "subscription_caps": {name: deepcopy(cap) for name in grants},
                },
            },
        },
        "inventory": [
            {
                "kind": "issue",
                "number": 7000,
                "title": "Scout known repo gaps",
                "body": "Bounded quota scout charter",
                "labels": [{"name": "needs-thought"}],
                "assignees": [],
            }
        ],
        "quota": {
            "available": True,
            "grants_complete": True,
            "fetched_at": NOW.isoformat(),
            "grants": grants,
        },
    }


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(controls, "_now", lambda: NOW)


def test_all_accounts_permit_but_preview_does_not_reserve():
    data = current()
    before = deepcopy(data)
    assert scout.eligibility(data)["ok"]
    assert data == before


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            lambda d: d["policy"]["problem_issues"]["improvement_scout"].update(
                enabled=False
            ),
            "scout_disabled",
        ),
        (lambda d: d.update(control_state="paused"), "scout_disabled"),
        (lambda d: d.update(intake_complete=False), "context_unknown"),
        (lambda d: d.update(goals=[]), "scout_no_active_goals"),
        (
            lambda d: d["policy"].update(allowed_models=["opus"]),
            "scout_model_not_allowed",
        ),
        (
            lambda d: d["inventory"][0].update(assignees=[{"login": "owner"}]),
            "scout_charter_not_ready",
        ),
        (
            lambda d: d["inventory"][0].update(labels=[{"name": "agent-ready"}]),
            "scout_charter_not_ready",
        ),
        (
            lambda d: d["quota"].update(grants_complete=False),
            "scout_all_grants_required",
        ),
        (
            lambda d: d["quota"]["grants"]["codex-a"]["pacing"].update(
                state="conserve"
            ),
            "scout_all_grants_required",
        ),
        (
            lambda d: d["quota"]["grants"]["codex-a"].update(
                observed_at=(NOW - timedelta(hours=1)).isoformat()
            ),
            "scout_all_grants_required",
        ),
        (
            lambda d: d["quota"]["grants"]["codex-a"]["windows"][0].update(
                used_percent=100
            ),
            "scout_all_grants_required",
        ),
        (
            lambda d: d["quota"]["grants"]["codex-a"]["windows"][0].update(
                resets_at=NOW.isoformat()
            ),
            "scout_all_grants_required",
        ),
        (
            lambda d: d["quota"]["grants"]["codex-a"]["windows"][0].update(
                reset_source="estimate"
            ),
            "scout_all_grants_required",
        ),
        (
            lambda d: d["policy"]["problem_issues"]["improvement_scout"][
                "subscription_caps"
            ].pop("codex-a"),
            "scout_cap_evidence_required",
        ),
    ],
)
def test_missing_prerequisites_never_fallback(mutation, reason):
    data = current()
    mutation(data)
    assert scout.eligibility(data)["reason"] == reason


def test_burst_exhaustion_on_one_account_blocks_both():
    data = current()
    data["quota"]["grants"]["codex-a"]["windows"].append(
        {
            "name": "burst",
            "used_percent": 95,
            "window_minutes": 300,
            "resets_at": (NOW + timedelta(hours=2)).isoformat(),
            "expired": False,
        }
    )
    assert scout.eligibility(data)["reason"] == "scout_all_grants_required"


def test_ready_backlog_has_priority():
    data = current()
    data["inventory"].append(
        {"kind": "issue", "number": 7001, "labels": [{"name": "agent-ready"}]}
    )
    assert scout.eligibility(data)["reason"] == "existing_ready_work"


def test_cooldown_and_useful_trial_limit_apply():
    data = current()
    data["improvement_history"] = [{"attempted_at": NOW.isoformat()}]
    assert scout.eligibility(data)["reason"] == "cooldown"
    data["improvement_history"] = [
        {"attempted_at": (NOW - timedelta(days=i)).isoformat()} for i in (2, 3, 4)
    ]
    assert scout.eligibility(data)["reason"] == "usefulness_review_required"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scout.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                FactoryControl,
                FactoryAudit,
                FactoryReceipt,
                WorkItem,
                SwarmNodeRun,
                SwarmTask,
                FactoryStart,
                FactoryClassTier,
                FactoryReviewVerdict,
                WorkItemEdge,
                WorkItemEvent,
                SwarmPlanNode,
                SwarmPlanVersion,
                SwarmConductorCall,
            )
        ],
    )
    data = current()
    with Session(engine) as session:
        session.add(
            FactoryControl(
                id="factory",
                actor="test",
                state="enabled",
                policy_json=controls._json(data["policy"]),
            )
        )
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(service, "_inventory", lambda: deepcopy(data["inventory"]))
    monkeypatch.setattr(service, "_quota", lambda: deepcopy(data["quota"]))
    monkeypatch.setattr(service, "_goals", lambda db: deepcopy(data["goals"]))
    monkeypatch.setattr(fence, "list_active_goals", lambda db: deepcopy(data["goals"]))
    monkeypatch.setattr(
        fence, "fetch_provider_quota_sync", lambda **kw: deepcopy(data["quota"])
    )
    with Session(engine) as session:
        session.add(
            FactoryAudit(
                actor="test",
                action="intake_evaluated",
                created_at=NOW,
                detail_json=controls._json(
                    {
                        "generation": 1,
                        "github": "completed",
                        "truncated": False,
                        "candidates": {"delivery": 0, "advisory": 0},
                    }
                ),
            )
        )
        session.commit()
    yield engine, data
    engine.dispose()


def test_concurrent_ticks_create_one_receipt_and_claim(db):
    engine, data = db
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: scout.tick(data["policy"]), range(2)))
    assert sum(result["ok"] for result in results) == 1
    with Session(engine) as session:
        assert len(session.exec(select(FactoryReceipt)).all()) == 1
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == scout.CLAIM)
                ).all()
            )
            == 1
        )
    assert not scout.tick(data["policy"])["ok"]


def attach_task(engine):
    with Session(engine) as session:
        session.add(
            SwarmTask(
                id="t-scout",
                task_text="scout",
                repo=service.REPO,
                base_branch="main",
                conductor_model="sol",
            )
        )
        row = session.exec(select(FactoryReceipt)).one()
        row.task_id = "t-scout"
        row.state = "admitted"
        session.commit()
    return {"id": "t-scout", "repo": service.REPO}


def test_dispatch_rechecks_quota_and_policy(db):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    assert scout.dispatch_allowed("t-scout")["ok"]
    data["quota"]["grants"]["codex-a"]["pacing"]["state"] = "conserve"
    assert not scout.dispatch_allowed("t-scout")["ok"]
    data["quota"]["grants"]["codex-a"]["pacing"]["state"] = "use_more"
    with Session(engine) as session:
        policy = deepcopy(data["policy"])
        policy["generation"] = 2
        session.get(FactoryControl, "factory").policy_json = controls._json(policy)
        session.commit()
    assert scout.dispatch_allowed("t-scout")["reason"] == "scout_policy_changed"


def test_claim_provenance_required(db):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    with Session(engine) as session:
        row = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == scout.CLAIM)
        ).one()
        session.delete(row)
        session.commit()
    assert not scout.is_scout("t-scout")
    assert not scout.dispatch_allowed("t-scout")["ok"]


def test_empty_scout_settlement_counts_once_and_replays(db, monkeypatch):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    monkeypatch.setattr(controls, "finish_task", lambda *a, **kw: {"ok": True})
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "_artifact", lambda run: run["artifact"])
    runs = [
        {
            "node_key": scout.COLLECT_KEY,
            "status": "succeeded",
            "id": 1,
            "session_id": 10,
            "artifact": {
                "findings": [],
                "repo_head": "a" * 40,
                "summary": "No actionable evidence",
            },
        }
    ]
    scout.reconcile(task, data["policy"], [], runs, 0)
    scout.reconcile(task, data["policy"], [], runs, 0)
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == service.ATTEMPT)
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == scout.SETTLED)
                ).all()
            )
            == 1
        )


def test_bounded_read_only_prompts_and_schema():
    data = current()
    data["review_digest"] = "a" * 64
    assert "Do not change files" in scout.boundary({"id": "t"})
    assert scout.schema(scout.COLLECT_KEY)["properties"]["findings"]["maxItems"] == 20
    assert scout.schema(scout.REVIEW_KEY) is scout.REVIEW_SCHEMA
    data["inventory"][0]["body"] = "x" * 60_001
    with pytest.raises(ValueError, match="too_large"):
        scout._prompt(data)


@pytest.mark.parametrize(
    "approved,same_session,digest_matches",
    [(False, False, True), (True, True, True), (True, False, False)],
)
def test_rejected_or_nonindependent_review_never_submits(
    db, monkeypatch, approved, same_session, digest_matches
):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "_artifact", lambda run: run["artifact"])
    monkeypatch.setattr(controls, "finish_task", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(
        service,
        "submit",
        lambda *a, **kw: pytest.fail("unreviewed candidate submitted"),
    )
    receipt_id = scout._receipt(task["id"])["id"]
    context = service.context(scout_receipt_id=receipt_id)
    collection = {
        "findings": [{"problem_key": "known-gap"}],
        "repo_head": "a" * 40,
        "summary": "Scoped finding",
    }
    review = {
        "approved": approved,
        "review_digest": context["review_digest"] if digest_matches else "b" * 64,
        "collection_digest": service._digest(collection),
        "evidence": "Checked exact source",
    }
    runs = [
        {
            "node_key": scout.COLLECT_KEY,
            "status": "succeeded",
            "id": 1,
            "session_id": 10,
            "artifact": collection,
        },
        {
            "node_key": scout.REVIEW_KEY,
            "status": "succeeded",
            "id": 2,
            "session_id": 10 if same_session else 11,
            "artifact": review,
        },
    ]
    scout.reconcile(task, data["policy"], [], runs, 0)
    with Session(engine) as session:
        attempt = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == service.ATTEMPT)
        ).one()
        assert "scout_review_rejected" in attempt.detail_json
        assert not session.exec(
            select(FactoryAudit).where(FactoryAudit.action == scout.REVIEW)
        ).all()


def test_approved_review_uses_existing_submit_once(db, monkeypatch):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "_artifact", lambda run: run["artifact"])
    monkeypatch.setattr(controls, "finish_task", lambda *a, **kw: {"ok": True})
    calls = []
    monkeypatch.setattr(
        service,
        "submit",
        lambda batch, **kw: calls.append((batch, kw)) or {"reason": "candidates"},
    )
    receipt_id = scout._receipt(task["id"])["id"]
    context = service.context(scout_receipt_id=receipt_id)
    collection = {
        "findings": [{"problem_key": "known-gap"}],
        "repo_head": "a" * 40,
        "summary": "Scoped finding",
    }
    review = {
        "approved": True,
        "review_digest": context["review_digest"],
        "collection_digest": service._digest(collection),
        "evidence": "Checked exact source",
    }
    runs = [
        {
            "node_key": scout.COLLECT_KEY,
            "status": "succeeded",
            "id": 1,
            "session_id": 10,
            "artifact": collection,
        },
        {
            "node_key": scout.REVIEW_KEY,
            "status": "succeeded",
            "id": 2,
            "session_id": 11,
            "artifact": review,
        },
    ]
    scout.reconcile(task, data["policy"], [], runs, 0)
    scout.reconcile(task, data["policy"], [], runs, 0)
    assert len(calls) == 1
    assert calls[0][1] == {"actor": scout.ACTOR, "scout_receipt_id": receipt_id}
    assert calls[0][0]["findings"] == collection["findings"]
    with Session(engine) as session:
        assert session.exec(
            select(FactoryAudit).where(FactoryAudit.action == scout.REVIEW)
        ).one()


def test_submission_commit_before_finish_replays_without_resubmission(db, monkeypatch):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    with Session(engine) as session:
        session.add(
            FactoryAudit(
                actor=scout.ACTOR,
                task_id=task["id"],
                action=service.ATTEMPT,
                detail_json=controls._json({"reason": "candidates"}),
            )
        )
        session.commit()
    monkeypatch.setattr(
        service, "submit", lambda *a, **kw: pytest.fail("duplicate submission")
    )
    finished = []
    monkeypatch.setattr(
        controls, "finish_task", lambda *a, **kw: finished.append(a) or {"ok": True}
    )
    scout.reconcile(task, data["policy"], [], [], 0)
    assert finished[0][1] == "succeeded"
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == service.ATTEMPT)
                ).all()
            )
            == 1
        )


def test_empty_result_counts_even_if_quota_has_since_gone_stale(db, monkeypatch):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    data["quota"]["fetched_at"] = (NOW - timedelta(hours=1)).isoformat()
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "_artifact", lambda run: run["artifact"])
    monkeypatch.setattr(controls, "finish_task", lambda *a, **kw: {"ok": True})
    scout.reconcile(
        task,
        data["policy"],
        [],
        [
            {
                "node_key": scout.COLLECT_KEY,
                "status": "succeeded",
                "artifact": {
                    "findings": [],
                    "repo_head": "a" * 40,
                    "summary": "No findings",
                },
            }
        ],
        0,
    )
    with Session(engine) as session:
        assert session.exec(
            select(FactoryAudit).where(FactoryAudit.action == service.ATTEMPT)
        ).one()


def test_approved_scout_persists_candidate_through_real_service(db, monkeypatch):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "_artifact", lambda run: run["artifact"])
    monkeypatch.setattr(controls, "finish_task", lambda *a, **kw: {"ok": True})
    receipt_id = scout._receipt(task["id"])["id"]
    context = service.context(scout_receipt_id=receipt_id)
    finding = {
        "problem_key": "connector/wet-hour-explanation",
        "title": "Explain measured rain frequency",
        "kind": "bug",
        "evidence": "Test output at commit " + "a" * 40,
        "reproduction": "Run exact rain-frequency test",
        "benefit": "Honest connector explanation",
        "acceptance_tests": "Eight wet hours of eight qualifying hours appear in explanation",
        "owner": "factory refinement",
        "goal_id": 7,
        "ready": True,
        "observed_at": NOW.isoformat(),
    }
    collection = {
        "findings": [finding],
        "repo_head": "a" * 40,
        "summary": "Verified existing gap",
    }
    review = {
        "approved": True,
        "review_digest": context["review_digest"],
        "collection_digest": service._digest(collection),
        "evidence": "Verified exact source and dedup inventory",
    }
    runs = [
        {
            "node_key": scout.COLLECT_KEY,
            "status": "succeeded",
            "session_id": 10,
            "artifact": collection,
        },
        {
            "node_key": scout.REVIEW_KEY,
            "status": "succeeded",
            "session_id": 11,
            "artifact": review,
        },
    ]
    with Session(engine) as session:
        rows = [
            SwarmNodeRun(
                task_id=task["id"],
                node_key=run["node_key"],
                attempt=1,
                status="succeeded",
                session_id=run["session_id"],
            )
            for run in runs
        ]
        session.add_all(rows)
        session.flush()
        for run, row in zip(runs, rows):
            run["id"] = row.id
        session.commit()
    scout.reconcile(task, data["policy"], [], runs, 0)
    scout.reconcile(task, data["policy"], [], runs, 0)
    with Session(engine) as session:
        attempt = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == service.ATTEMPT)
        ).one()
        assert attempt.task_id == task["id"]
        candidate = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == service.CANDIDATE)
        ).one()
        assert '"scout_receipt_id":' + str(receipt_id) in candidate.detail_json
        assert "Explain measured rain frequency" in candidate.detail_json
        assert session.exec(
            select(FactoryAudit).where(FactoryAudit.action == scout.SETTLED)
        ).one()


def test_transport_checks_scout_on_each_wake_and_fails_closed(db, monkeypatch):
    from factory.orchestration.api import factory_session_allowed

    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    seed_run(engine, scout.COLLECT_KEY)
    seed_run(engine, scout.REVIEW_KEY)
    monkeypatch.setattr(controls, "can_start", lambda *a, **kw: {"ok": True})
    calls = []

    def quota(**kwargs):
        calls.append(kwargs)
        return deepcopy(data["quota"])

    monkeypatch.setattr(fence, "fetch_provider_quota_sync", quota)
    assert factory_session_allowed("factory:t-scout:scout_collect_1:1")
    data["quota"]["grants"]["codex-a"]["pacing"]["state"] = "conserve"
    assert not factory_session_allowed("factory:t-scout:scout_collect_1:1")
    assert calls == [{"force": True}, {"force": True}]
    assert factory_session_allowed("factory:t-other:implement_1:1")
    assert len(calls) == 2

    def unavailable(**kwargs):
        raise RuntimeError("quota telemetry unavailable")

    monkeypatch.setattr(fence, "fetch_provider_quota_sync", unavailable)
    assert not factory_session_allowed("factory:t-scout:scout_review_1:1")


def test_claimed_generation_does_not_repeat_network_scans(db, monkeypatch):
    _engine, data = db
    assert scout.tick(data["policy"])["ok"]
    monkeypatch.setattr(service, "context", lambda **kw: pytest.fail("repeated scan"))
    for _ in range(3):
        assert (
            scout.tick(data["policy"])["reason"] == "scout_generation_already_claimed"
        )


@pytest.mark.parametrize("key", [scout.COLLECT_KEY, scout.REVIEW_KEY])
def test_conductor_scout_nodes_are_bounded_and_read_only(monkeypatch, key):
    from factory.orchestration import factory_conductor as conductor

    calls = []
    monkeypatch.setattr(
        conductor.graph, "add_node", lambda *a, **kw: calls.append((a, kw))
    )
    policy = {**current()["policy"], "conductor_model": "sol", "max_attempts": 2}
    conductor._add(
        {"id": "t-scout", "repo": service.REPO},
        policy,
        key,
        "collect only known evidence",
        [],
        "sol",
        "scout",
        "test",
        refine=True,
        max_attempts=1,
        max_cost_usd=1,
        turn_timeout_seconds=300,
        expected_version=0,
    )
    args = calls[0][1]
    assert args["model"] == "sol"
    assert args["side_effects"] is False
    assert args["max_attempts"] == 1
    assert args["turn_timeout_seconds"] == 300
    assert "Output the requested structured artifact only" in args["prompt"]
    assert "Deliver repository changes" not in args["prompt"]
    assert conductor._schema(key) is scout.schema(key)


@pytest.mark.parametrize("schema", [scout.COLLECT_SCHEMA, scout.REVIEW_SCHEMA])
def test_physical_guest_prompt_keeps_scout_read_only(schema):
    from factory.orchestration.node_workflows import _node_prompt

    prompt = _node_prompt(
        scout.boundary({"id": "t"}), ".factory/result.json", schema, branch="factory/t"
    )
    assert "Inspect branch factory/t read-only" in prompt
    assert "Do not modify source, commit or push" in prompt
    assert "Commit and push source changes" not in prompt
    assert "Write only the declared transient artifact" in prompt


def test_unknown_grant_provider_cannot_hide_routable_account(monkeypatch):
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    data = current()
    next(iter(data["quota"]["grants"].values()))["provider"] = None
    assert scout.eligibility(data)["reason"] == "scout_grant_inventory_unknown"


def seed_run(engine, node_key, *, model="sol", pin_model="sol", attempt=1):
    with Session(engine) as session:
        session.add(
            SwarmNodeRun(
                task_id="t-scout",
                node_key=node_key,
                attempt=attempt,
                status="admitted",
                model=model,
                pin_json=controls._json(
                    {
                        "task_id": "t-scout",
                        "node_key": node_key,
                        "attempt": attempt,
                        "model": pin_model,
                    }
                ),
            )
        )
        session.commit()


@pytest.mark.parametrize(
    "key,model,pin_model,attempt",
    [
        ("conductor_funding_1", "sol", "sol", 1),
        (scout.COLLECT_KEY, "opus", "sol", 1),
        (scout.COLLECT_KEY, "sol", "opus", 1),
        (scout.COLLECT_KEY, "sol", "sol", 2),
    ],
)
def test_scout_transport_rejects_generic_nodes_changed_models_and_retries(
    db, key, model, pin_model, attempt
):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    seed_run(engine, key, model=model, pin_model=pin_model, attempt=attempt)
    assert not scout.transport_allowed("t-scout", key, attempt)
    assert scout.transport_allowed("ordinary-task", key, attempt)


@pytest.mark.parametrize("action", [scout.SETTLED, service.ATTEMPT])
def test_settlement_waiting_for_unresolved_starts_fences_all_transport(db, action):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    seed_run(engine, scout.COLLECT_KEY)
    with Session(engine) as session:
        session.add(
            FactoryAudit(
                actor=scout.ACTOR,
                action=action,
                task_id="t-scout",
                detail_json=controls._json(
                    {"reason": "scout_nonrenewable", "success": False}
                ),
            )
        )
        session.commit()
    assert scout.dispatch_allowed("t-scout")["reason"] == "scout_already_settled"
    assert not scout.transport_allowed("t-scout", scout.COLLECT_KEY, 1)


@pytest.mark.parametrize("entrypoint", ["request", "reconcile"])
def test_funding_cannot_add_judge_for_audited_scout(db, monkeypatch, entrypoint):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_funding as funding

    monkeypatch.setattr(funding, "enabled", lambda: True)
    monkeypatch.setattr(
        conductor, "_add", lambda *a, **kw: pytest.fail("funding judge escaped")
    )
    monkeypatch.setattr(controls, "finish_task", lambda *a, **kw: {"ok": True})
    if entrypoint == "request":
        assert not funding.request(task, "turn limit")
    else:
        assert funding.reconcile(
            task, data["policy"], [], {"ok": False, "reason": "task_deadline"}
        )
    with Session(engine) as session:
        assert session.exec(
            select(FactoryAudit).where(FactoryAudit.action == scout.SETTLED)
        ).one()
        assert not session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "funding_review_requested"
            )
        ).all()
        assert not session.exec(select(SwarmPlanNode)).all()


def test_healthy_scout_passes_funding_without_judge(db, monkeypatch):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    task = attach_task(engine)
    from factory.orchestration import factory_funding as funding

    monkeypatch.setattr(funding, "enabled", lambda: True)
    assert funding.reconcile(task, data["policy"], [], {"ok": True}) is False
    with Session(engine) as session:
        assert not session.exec(
            select(FactoryAudit).where(FactoryAudit.action == scout.SETTLED)
        ).all()


def test_real_admission_and_funding_reconcile_keep_scout_on_bounded_path(
    db, monkeypatch
):
    engine, data = db
    import knowledge.api

    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_funding as funding
    from factory.orchestration.factory_intake import admit_next

    monkeypatch.setattr(knowledge.api, "prepare_recall", lambda *a, **kw: None)
    monkeypatch.setattr(conductor, "get_engine", lambda: engine)
    monkeypatch.setattr(funding, "enabled", lambda: True)
    policy = controls.validate_policy(
        {
            **data["policy"],
            "issue_numbers": [1],
            "max_tasks": {"advisory": 1, "delivery": 1},
            "max_turns_per_task": 2,
            "conductor_model": "sol",
            "worker_model": "sol",
            "base_branch": "main",
            "max_attempts": 1,
            "task_timeout_seconds": 3600,
        }
    )
    data["policy"] = policy
    with Session(engine) as session:
        session.get(FactoryControl, "factory").policy_json = controls._json(policy)
        session.commit()
    assert scout.tick(policy)["ok"]
    admitted = admit_next("scheduler", lanes=("advisory",))
    assert admitted["ok"], admitted
    task = conductor._task(admitted["task_id"])
    assert task["improvement_scout"] is True
    assert scout.is_scout(task["id"])
    assert funding.reconcile(task, policy, [], {"ok": True}) is False
    assert (
        funding.reconcile(task, policy, [], {"ok": False, "reason": "task_deadline"})
        is True
    )
    with Session(engine) as session:
        receipt = session.exec(select(FactoryReceipt)).one()
        assert receipt.state == "failed"
        assert not session.exec(select(SwarmPlanNode)).all()
        assert not session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "funding_review_requested"
            )
        ).all()


def test_missing_claim_cannot_reclassify_scout_as_ordinary_transport(db):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    seed_run(engine, scout.COLLECT_KEY)
    with Session(engine) as session:
        event = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == scout.CLAIM)
        ).one()
        event.actor = "untrusted"
        session.commit()
    assert scout.owns_task("t-scout")
    assert not scout.is_scout("t-scout")
    assert not scout.transport_allowed("t-scout", scout.COLLECT_KEY, 1)
    assert not scout.transport_allowed("t-scout", "conductor_funding_1", 1)


@pytest.mark.parametrize(
    "change", ["stale_intake", "ready_backlog", "new_owner", "no_goals", "stale_quota"]
)
def test_leaf_transport_requires_current_database_and_quota_evidence(db, change):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    seed_run(engine, scout.COLLECT_KEY)
    assert fence.transport_allowed("t-scout", scout.COLLECT_KEY, 1)
    if change == "no_goals":
        data["goals"] = []
    elif change == "stale_quota":
        data["quota"]["fetched_at"] = (NOW - timedelta(seconds=901)).isoformat()
    else:
        with Session(engine) as session:
            if change == "new_owner":
                session.add(
                    FactoryReceipt(
                        repo=service.REPO,
                        issue_number=7001,
                        generation=1,
                        title="Important existing task",
                        body="known work",
                        url="https://github.com/jomcgi-org/homelab/issues/7001",
                        actor="factory:intake",
                        task_class="bug-fix",
                        state="queued",
                    )
                )
            else:
                sweep = session.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.action == "intake_evaluated"
                    )
                ).one()
                if change == "stale_intake":
                    sweep.created_at = NOW - timedelta(seconds=901)
                else:
                    sweep.detail_json = controls._json(
                        {
                            "github": "completed",
                            "truncated": False,
                            "generation": 1,
                            "candidates": {"delivery": 1, "advisory": 0},
                        }
                    )
            session.commit()
    assert not fence.transport_allowed("t-scout", scout.COLLECT_KEY, 1)


@pytest.mark.parametrize("change", ["pause", "policy", "new_owner", "goals"])
def test_broker_delay_cannot_carry_stale_transport_permission(db, monkeypatch, change):
    engine, data = db
    assert scout.tick(data["policy"])["ok"]
    attach_task(engine)
    seed_run(engine, scout.COLLECT_KEY)

    def delayed_read(**kwargs):
        assert kwargs == {"force": True}
        assert engine.pool.checkedout() == 0
        if change == "goals":
            data["goals"] = []
        else:
            with Session(engine) as session:
                control = session.get(FactoryControl, "factory")
                if change == "pause":
                    control.state = "paused"
                elif change == "policy":
                    updated = deepcopy(data["policy"])
                    updated["generation"] = 2
                    control.policy_json = controls._json(updated)
                else:
                    session.add(
                        FactoryReceipt(
                            repo=service.REPO,
                            issue_number=7001,
                            generation=1,
                            title="Existing priority task",
                            body="ready",
                            url="https://github.com/jomcgi-org/homelab/issues/7001",
                            actor="factory:intake",
                            task_class="bug-fix",
                            state="queued",
                        )
                    )
                session.commit()
        return deepcopy(data["quota"])

    monkeypatch.setattr(fence, "fetch_provider_quota_sync", delayed_read)
    assert not fence.transport_allowed("t-scout", scout.COLLECT_KEY, 1)
