"""Exact-event problem issue production and failure-path regressions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from core.jsonio import dumps
import factory.orchestration.factory_controls as controls
import factory.orchestration.factory_problem_issues as producer
from factory.orchestration.factory_improvement_preview import digest
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
)
from factory.orchestration.models import SwarmTask

START = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'problem-issues.db'}",
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
                FactoryControl,
                WorkItem,
                FactoryReceipt,
                FactoryAudit,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.add(
            SwarmTask(
                id="t-1",
                task_text="deliver issue",
                repo="owner/repo",
                base_branch="main",
                conductor_model="astra",
            )
        )
        session.commit()
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=6002,
                generation=1,
                title="factory programme",
                body="body",
                url="https://github.com/owner/repo/issues/6002",
                actor="test",
                state="succeeded",
                task_id="t-1",
            )
        )
        session.commit()
    clock = {"now": START}
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: clock["now"])
    monkeypatch.setattr(producer, "_now", lambda: clock["now"])
    yield engine, clock
    engine.dispose()


def policy(**overrides):
    block = {
        "enabled": True,
        "sources": {
            "node_stalled": True,
            "workflow_stranded": False,
            "landing_recovery_exhausted": False,
        },
        **overrides,
    }
    return {"repo": "owner/repo", "problem_issues": block}


def test_policy_defaults_every_source_off_and_refuses_broader_writes():
    block = controls._validate_problem_issues({})
    assert block == controls.DEFAULT_PROBLEM_ISSUES
    assert not any(block["sources"].values())
    with pytest.raises(ValueError, match="labels must be exactly bug"):
        controls._validate_problem_issues({"labels": ["bug", "agent-ready"]})
    with pytest.raises(ValueError, match="max_per_tick"):
        controls._validate_problem_issues({"max_per_tick": 2})


def test_github_write_streams_and_refuses_an_oversized_response(monkeypatch):
    client_class = httpx.Client
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            201,
            content=b"x" * (producer.RESPONSE_LIMIT_BYTES + 1),
        )
    )
    monkeypatch.setattr(
        producer.httpx,
        "Client",
        lambda **kwargs: client_class(transport=transport, **kwargs),
    )

    with pytest.raises(ValueError, match="response exceeds factory limit"):
        producer.github_write("owner/repo", {"title": "bounded", "body": "body"})


def add_event(engine, *, workflow="wf-1", action="node_stalled", **detail):
    payload = {"workflow_id": workflow, "node_key": "implement", **detail}
    with Session(engine) as session:
        row = FactoryAudit(
            actor="factory:conductor",
            action=action,
            task_id="t-1",
            detail_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def audits(engine, action):
    with Session(engine) as session:
        return session.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == action)
            .order_by(FactoryAudit.id)
        ).all()


def details(rows):
    return [json.loads(row.detail_json) for row in rows]


def enable_after_watermark(engine, selected_policy):
    producer.problem_issues_tick(selected_policy)
    observed = details(audits(engine, "problem_issue_policy_observed"))
    assert any(observed[-1]["effective_sources"].values())


def github(monkeypatch, issues=None):
    state = {
        "issues": list(issues or []),
        "writes": [],
        "reads": [],
        "read_repos": [],
        "write_repos": [],
    }

    def read(repo, suffix):
        state["read_repos"].append(repo)
        state["reads"].append(suffix)
        return list(state["issues"])

    def write(repo, payload):
        state["write_repos"].append(repo)
        state["writes"].append(payload)
        created = {"number": 7000 + len(state["writes"]), **payload}
        state["issues"].insert(0, created)
        return created

    monkeypatch.setattr(producer, "github_list", read)
    monkeypatch.setattr(producer, "github_write", write)
    return state


def test_omitted_and_disabled_policy_make_no_github_calls_or_writes(db, monkeypatch):
    engine, _clock = db
    monkeypatch.setattr(
        producer,
        "github_list",
        lambda *_args: pytest.fail("disabled producer read GitHub"),
    )
    monkeypatch.setattr(
        producer,
        "github_write",
        lambda *_args: pytest.fail("disabled producer wrote GitHub"),
    )
    producer.problem_issues_tick({"repo": "owner/repo"})
    producer.problem_issues_tick(
        {"repo": "owner/repo", "problem_issues": {"enabled": False}}
    )
    assert audits(engine, "problem_issue_write_started") == []
    view = controls.problem_issues_state({})
    assert view["status"] == "off"
    assert view["policy"] == controls.DEFAULT_PROBLEM_ISSUES


def test_omission_watermarks_events_before_reenable(db, monkeypatch):
    engine, _clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)

    producer.problem_issues_tick({"repo": "owner/repo"})
    add_event(engine, workflow="while-omitted")
    producer.problem_issues_tick(selected)
    assert state["writes"] == []

    add_event(engine, workflow="after-reenable")
    producer.problem_issues_tick(selected)
    assert len(state["writes"]) == 1
    assert "after-reenable" in state["writes"][0]["body"]


def test_first_enabled_tick_watermarks_history_then_creates_source_linked_issue(
    db, monkeypatch
):
    engine, _clock = db
    add_event(engine, workflow="historical")
    state = github(monkeypatch)
    selected = policy()

    enable_after_watermark(engine, selected)
    assert state["reads"] == [] and state["writes"] == []

    source_id = add_event(engine, workflow="new-event", idle_seconds=901)
    producer.problem_issues_tick(selected)

    assert len(state["writes"]) == 1
    issue = state["writes"][0]
    assert issue["labels"] == ["bug"]
    assert "https://github.com/owner/repo/issues/6002" in issue["body"]
    assert "This issue is discovery input only" in issue["body"]
    assert "<!-- factory-problem:" in issue["body"]
    created = details(audits(engine, "problem_issue_created"))
    assert created == [
        {
            "fingerprint": created[0]["fingerprint"],
            "issue_number": 7001,
            "source": "node_stalled",
            "source_audit_id": source_id,
        }
    ]
    with Session(engine) as session:
        assert len(session.exec(select(FactoryReceipt)).all()) == 1


@pytest.mark.parametrize(
    ("source", "action", "event_detail", "title_fragment", "body_fragment"),
    [
        (
            "workflow_stranded",
            "workflow_stranded",
            {"workflow": "wf-stranded", "running_version": "v2"},
            "workflow stranded",
            "Running Version: `v2`",
        ),
        (
            "landing_recovery_exhausted",
            "merge_arm_refused",
            {"reason": "landing_recovery_exhausted", "pr_number": 6123},
            "landing recovery exhausted",
            "https://github.com/owner/repo/pull/6123",
        ),
    ],
)
def test_other_selected_exact_sources_create_the_same_bounded_issue_shape(
    db,
    monkeypatch,
    source,
    action,
    event_detail,
    title_fragment,
    body_fragment,
):
    engine, _clock = db
    selected = policy(
        sources={
            "node_stalled": False,
            "workflow_stranded": source == "workflow_stranded",
            "landing_recovery_exhausted": source == "landing_recovery_exhausted",
        }
    )
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, action=action, **event_detail)

    producer.problem_issues_tick(selected)

    assert len(state["writes"]) == 1
    assert title_fragment in state["writes"][0]["title"]
    assert body_fragment in state["writes"][0]["body"]
    assert details(audits(engine, "problem_issue_created"))[-1]["source"] == source


def test_replayed_exact_event_reconciles_marker_without_duplicate_write(
    db, monkeypatch
):
    engine, _clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="same-workflow")
    producer.problem_issues_tick(selected)
    add_event(engine, workflow="same-workflow")
    producer.problem_issues_tick(selected)

    assert len(state["writes"]) == 1
    reconciled = details(audits(engine, "problem_issue_reconciled"))
    assert reconciled[-1]["issue_numbers"] == [7001]
    assert reconciled[-1]["retry"] == 0


def test_issue_discovery_failure_refuses_the_write_and_backs_off(db, monkeypatch):
    engine, clock = db
    selected = policy()
    enable_after_watermark(engine, selected)
    add_event(engine)
    writes = []
    monkeypatch.setattr(
        producer,
        "github_list",
        lambda *_args: (_ for _ in ()).throw(httpx.ReadError("lost")),
    )
    monkeypatch.setattr(producer, "github_write", lambda *_args: writes.append(1))

    producer.problem_issues_tick(selected)
    producer.problem_issues_tick(selected)
    assert writes == []
    failure = details(audits(engine, "problem_issue_discovery_failed"))
    assert len(failure) == 1 and failure[0]["error"] == "ReadError"
    assert datetime.fromisoformat(failure[0]["next_retry_at"]) == clock[
        "now"
    ] + timedelta(minutes=2)


@pytest.mark.parametrize("omit_policy", [False, True])
def test_uncertain_write_reconciles_marker_after_backoff_without_reposting(
    db, monkeypatch, omit_policy
):
    engine, clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="uncertain")

    def uncertain(_repo, payload):
        state["writes"].append(payload)
        state["issues"].append({"number": 7331, **payload})
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(producer, "github_write", uncertain)
    producer.problem_issues_tick(selected)
    assert len(state["writes"]) == 1
    started = details(audits(engine, "problem_issue_write_started"))[0]
    assert started["repo"] == "owner/repo"
    uncertain_audit = details(audits(engine, "problem_issue_write_uncertain"))[0]
    assert datetime.fromisoformat(uncertain_audit["next_retry_at"]) == clock[
        "now"
    ] + timedelta(minutes=2)

    producer.problem_issues_tick(selected)
    assert len(state["reads"]) == 1
    clock["now"] += timedelta(minutes=2)
    disabled = {**selected, "repo": "moved/repo"}
    if omit_policy:
        disabled.pop("problem_issues")
    else:
        disabled["problem_issues"] = {"enabled": False}
    producer.problem_issues_tick(disabled)
    assert len(state["writes"]) == 1
    assert state["read_repos"] == ["owner/repo", "owner/repo"]
    assert details(audits(engine, "problem_issue_reconciled"))[-1]["issue_numbers"] == [
        7331
    ]


@pytest.mark.parametrize(
    ("headers", "expected_action"),
    [
        ({"X-RateLimit-Remaining": "0"}, "problem_issue_write_uncertain"),
        ({}, "problem_issue_write_refused"),
    ],
)
def test_github_403_distinguishes_rate_limit_from_permission_refusal(
    db, monkeypatch, headers, expected_action
):
    engine, _clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow=expected_action)

    def forbidden(_repo, payload):
        state["writes"].append(payload)
        request = httpx.Request("POST", "https://api.github.test/issues")
        response = httpx.Response(403, headers=headers, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(producer, "github_write", forbidden)
    producer.problem_issues_tick(selected)

    assert len(state["writes"]) == 1
    assert details(audits(engine, expected_action))[-1]["status"] == 403
    other = (
        "problem_issue_write_refused"
        if expected_action == "problem_issue_write_uncertain"
        else "problem_issue_write_uncertain"
    )
    assert audits(engine, other) == []


def test_six_reconciled_retries_end_unresolved_without_blind_write_retry(
    db, monkeypatch
):
    engine, clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="lost")

    def timeout(_repo, payload):
        state["writes"].append(payload)
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(producer, "github_write", timeout)
    producer.problem_issues_tick(selected)
    for delay in controls.DEFAULT_PROBLEM_ISSUES["retry_minutes"]:
        clock["now"] += timedelta(minutes=delay)
        producer.problem_issues_tick(selected)

    assert len(state["writes"]) == 1
    retries = details(audits(engine, "problem_issue_reconcile_retry"))
    assert [row["retry"] for row in retries] == [1, 2, 3, 4, 5]
    assert details(audits(engine, "problem_issue_unresolved"))[-1]["retry"] == 6


def test_definite_github_refusal_is_terminal_and_not_retried(db, monkeypatch):
    engine, clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="refused")

    def refused(_repo, payload):
        state["writes"].append(payload)
        request = httpx.Request("POST", "https://api.github.test/issues")
        response = httpx.Response(422, request=request)
        raise httpx.HTTPStatusError("unprocessable", request=request, response=response)

    monkeypatch.setattr(producer, "github_write", refused)
    producer.problem_issues_tick(selected)
    clock["now"] += timedelta(days=1)
    producer.problem_issues_tick(selected)

    assert len(state["writes"]) == 1
    refusal = details(audits(engine, "problem_issue_write_refused"))
    assert refusal[-1]["status"] == 422
    assert audits(engine, "problem_issue_reconcile_retry") == []


def test_one_per_tick_and_rolling_daily_cap_are_audited(db, monkeypatch):
    engine, _clock = db
    selected = policy(max_per_24_hours=1)
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="first")
    add_event(engine, workflow="second")

    producer.problem_issues_tick(selected)
    assert len(state["writes"]) == 1
    producer.problem_issues_tick(selected)
    assert len(state["writes"]) == 1
    capped = details(audits(engine, "problem_issue_daily_capped"))
    assert capped[-1]["used"] == 1 and capped[-1]["limit"] == 1


def test_concurrent_observers_claim_one_external_write(db, monkeypatch):
    engine, _clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _n: producer.problem_issues_tick(selected), range(2)))

    assert len(state["writes"]) == 1
    assert len(audits(engine, "problem_issue_write_started")) == 1


def test_malformed_source_is_refused_and_does_not_block_the_next_event(db, monkeypatch):
    engine, _clock = db
    selected = policy()
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    with Session(engine) as session:
        session.add(
            FactoryAudit(
                actor="factory:conductor",
                action="node_stalled",
                task_id="t-1",
                detail_json="not-json",
            )
        )
        session.commit()
    add_event(engine, workflow="after-malformed")

    producer.problem_issues_tick(selected)
    assert state["writes"] == []
    refusal = details(audits(engine, "problem_issue_source_refused"))[-1]
    assert refusal["reason"] == "malformed_source_audit"

    producer.problem_issues_tick(selected)
    assert len(state["writes"]) == 1


def test_source_receipt_repository_owns_discovery_links_and_write(db, monkeypatch):
    engine, _clock = db
    selected = {**policy(), "repo": "new-owner/new-repo"}
    state = github(monkeypatch)
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="pinned-repo")

    producer.problem_issues_tick(selected)

    assert state["read_repos"] == ["owner/repo"]
    assert state["write_repos"] == ["owner/repo"]
    assert "https://github.com/owner/repo/issues/6002" in state["writes"][0]["body"]


def test_pull_request_marker_does_not_deduplicate_an_issue(db, monkeypatch):
    engine, _clock = db
    selected = policy()
    enable_after_watermark(engine, selected)
    source_id = add_event(engine, workflow="pr-marker")
    with Session(engine) as session:
        row = session.get(FactoryAudit, source_id)
        assert row is not None
        detail = json.loads(row.detail_json)
        fingerprint = producer._fingerprint("node_stalled", row, detail)
    assert fingerprint is not None
    state = github(
        monkeypatch,
        issues=[
            {
                "number": 6999,
                "body": producer._marker(fingerprint),
                "pull_request": {"url": "https://api.github.test/pulls/6999"},
            }
        ],
    )

    producer.problem_issues_tick(selected)

    assert len(state["writes"]) == 1
    assert details(audits(engine, "problem_issue_created"))[-1]["issue_number"] == 7001


def test_issue_lookup_stays_bounded_when_two_pages_are_full(db, monkeypatch):
    engine, _clock = db
    selected = policy()
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="paged")
    calls = []

    def read(_repo, suffix):
        calls.append(suffix)
        return [{"number": number, "body": ""} for number in range(100)]

    monkeypatch.setattr(producer, "github_list", read)
    writes = []

    def write(_repo, payload):
        writes.append(payload)
        return {"number": 8000, **payload}

    monkeypatch.setattr(producer, "github_write", write)
    producer.problem_issues_tick(selected)
    assert len(calls) == 2
    assert len(writes) == 1
    assert "per_page=100&page=1" in calls[0]
    assert "per_page=100&page=2" in calls[1]
    capped = details(audits(engine, "problem_issue_issue_scan_capped"))
    assert capped[-1]["pages"] == 2 and capped[-1]["per_page"] == 100
    assert audits(engine, "problem_issue_source_refused") == []
    assert details(audits(engine, "problem_issue_created"))[-1]["issue_number"] == 8000


def test_improvement_preview_is_read_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not read or write external state")

    monkeypatch.setattr(producer, "github_write", forbidden)
    monkeypatch.setattr(producer, "github_list", forbidden)
    monkeypatch.setattr(producer, "_locked_session", forbidden)
    result = producer.improvement_preview({}, now=START)
    assert result["mode"] == "preview_only"
    assert result["reason"] == "context_unknown"
    assert result["drafts"] == []


@pytest.fixture
def improvements(db, monkeypatch):
    from factory.orchestration import factory_improvements as improvements

    engine, clock = db
    quota = {
        "available": True,
        "grants_complete": True,
        "fetched_at": START.isoformat(),
        "grants": {
            "account-a": {
                "provider": "codex",
                "grant": "account-a",
                "observed": True,
                "observed_at": START.isoformat(),
                "status": "allowed",
                "exhausted": False,
                "windows": [
                    {
                        "name": "primary",
                        "window_minutes": 10080,
                        "used_percent": 0,
                        "resets_at": (START + timedelta(days=2)).isoformat(),
                        "expired": False,
                    }
                ],
            }
        },
    }
    configured = {
        "repo": "jomcgi-org/homelab",
        "task_budget_usd": 50,
        "turn_budget_usd": 10,
        "generation": 14,
        "problem_issues": {"enabled": True, "sources": {"quota_improvement": True}},
    }
    with Session(engine) as session:
        control = session.get(FactoryControl, "factory")
        control.state = "enabled"
        control.policy_json = json.dumps(configured)
        session.add(
            FactoryAudit(
                actor="test",
                action="intake_evaluated",
                created_at=START,
                detail_json=json.dumps(
                    {
                        "generation": 14,
                        "github": "completed",
                        "truncated": False,
                        "candidates": {"delivery": 0, "advisory": 0},
                    }
                ),
            )
        )
        session.commit()
    inventory = []
    monkeypatch.setattr(improvements, "_inventory", lambda: inventory)
    monkeypatch.setattr(improvements, "_quota", lambda: quota)
    monkeypatch.setattr(
        improvements,
        "_goals",
        lambda db: [{"id": 7, "statement": "honest connector explanations"}],
    )
    original_audit = controls._audit

    def timed_audit(session, *args, **kwargs):
        original_audit(session, *args, **kwargs)
        for row in session.new:
            if isinstance(row, FactoryAudit):
                row.created_at = clock["now"]

    monkeypatch.setattr(controls, "_audit", timed_audit)
    monkeypatch.setattr(producer, "github_list", lambda *args: [])
    writes = []

    def write(repo, payload):
        writes.append((repo, payload))
        return {"number": 8000, "body": payload["body"]}

    monkeypatch.setattr(producer, "github_write", write)
    batch = {
        "review_digest": improvements.context()["review_digest"],
        "rules_reviewed": True,
        "dedup_reviewed": True,
        "subscription_caps": {
            "account-a": {
                "subscription_only": True,
                "overage_disabled": True,
                "evidence": "reviewed subscription cap configuration",
            }
        },
        "findings": [
            {
                "problem_key": "scotland-connector/wet-hour-explanation",
                "title": "connector: explain rain frequency in qualifying walk hours",
                "kind": "bug",
                "goal_id": 7,
                "owner": "factory refinement",
                "ready": True,
                "observed_at": START.isoformat(),
                "evidence": "82db9a0f: internal/connector/trip.go returns WetHours; walks verdict omits frequency",
                "reproduction": "Inspect WetHours increment at >= 0.1 mm and compare the walk why string",
                "benefit": "Expose frequent light rain without changing user thresholds",
                "acceptance_tests": "Synthetic eight qualifying wet hours reports rain in 8 of 8; threshold verdict unchanged",
            }
        ],
    }
    return improvements, engine, clock, configured, batch, inventory, quota, writes


def test_improvement_submission_uses_existing_writer_and_no_ready_label(improvements):
    service, _engine, _clock, policy, batch, _inventory, _quota, writes = improvements
    result = service.submit(batch, actor="operator:test")
    assert result["mode"] == "audit_candidate"
    assert len(result["drafts"]) == 1
    assert not writes
    producer.problem_issues_tick(policy)
    assert len(writes) == 1
    assert writes[0][0] == "jomcgi-org/homelab"
    assert writes[0][1]["labels"] == []
    assert "Synthetic eight" in writes[0][1]["body"]
    producer.problem_issues_tick(policy)
    assert len(writes) == 1
    assert service.submit(batch, actor="operator:test")["reason"] == "cooldown"


def test_improvement_empty_attempt_is_durable_and_cools_down(improvements):
    service, engine, _clock, policy, batch, _inventory, _quota, writes = improvements
    batch["findings"] = []
    assert (
        service.submit(batch, actor="operator:test")["reason"]
        == "no_actionable_findings"
    )
    with Session(engine) as session:
        rows = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "improvement_attempt")
        ).all()
        assert len(rows) == 1
    assert service.submit(batch, actor="operator:test")["reason"] == "cooldown"
    producer.problem_issues_tick(policy)
    assert not writes


@pytest.mark.parametrize(
    "change", ["quota", "backlog", "owner", "goal", "policy", "inventory", "expiry"]
)
def test_improvement_rechecks_before_write(improvements, change, monkeypatch):
    service, engine, clock, policy, batch, inventory, quota, writes = improvements
    service.submit(batch, actor="operator:test")
    if change == "quota":
        quota["grants"]["account-a"]["windows"][0]["used_percent"] = 100
    elif change == "inventory":
        inventory.append({"number": 44, "title": "new open work", "body": ""})
    elif change == "expiry":
        clock["now"] += timedelta(seconds=901)
    elif change == "goal":
        monkeypatch.setattr(service, "_goals", lambda db: [])
    else:
        with Session(engine) as session:
            if change == "policy":
                session.get(FactoryControl, "factory").state = "paused"
            elif change == "owner":
                receipt = session.exec(select(FactoryReceipt)).first()
                receipt.repo = service.REPO
                receipt.state = "admitted"
            else:
                row = session.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.action == "intake_evaluated"
                    )
                ).first()
                row.detail_json = json.dumps(
                    {
                        "generation": 14,
                        "github": "completed",
                        "truncated": False,
                        "candidates": {"delivery": 1, "advisory": 0},
                    }
                )
            session.commit()
    producer.problem_issues_tick(policy)
    assert not writes


def test_improvement_context_must_be_reviewed_and_source_enabled(improvements):
    service, engine, _clock, _policy, batch, _inventory, _quota, writes = improvements
    batch["review_digest"] = "0" * 64
    with pytest.raises(ValueError, match="context_changed"):
        service.submit(batch, actor="operator:test")
    batch["review_digest"] = service.context()["review_digest"]
    with Session(engine) as session:
        session.get(FactoryControl, "factory").state = "paused"
        session.commit()
    with pytest.raises(ValueError, match="source_disabled"):
        service.submit(batch, actor="operator:test")
    assert not writes


def test_improvement_duplicate_open_pr_is_not_enqueued(improvements):
    service, _engine, _clock, policy, batch, inventory, _quota, writes = improvements
    inventory.append(
        {"kind": "pr", "number": 42, "title": batch["findings"][0]["title"], "body": ""}
    )
    batch["review_digest"] = service.context()["review_digest"]
    assert (
        service.submit(batch, actor="operator:test")["reason"] == "duplicate_or_owned"
    )
    producer.problem_issues_tick(policy)
    assert not writes


def test_improvement_uncertain_write_is_not_repeated(improvements, monkeypatch):
    service, _engine, clock, policy, batch, _inventory, _quota, writes = improvements
    service.submit(batch, actor="operator:test")

    def uncertain(repo, payload):
        writes.append(payload)
        raise httpx.ReadTimeout("unknown result")

    monkeypatch.setattr(producer, "github_write", uncertain)
    producer.problem_issues_tick(policy)
    clock["now"] += timedelta(minutes=5)
    producer.problem_issues_tick(policy)
    assert len(writes) == 1


def test_improvement_diversity_needs_policy_and_no_fabricated_forecast(improvements):
    service, _engine, _clock, _policy, batch, _inventory, _quota, writes = improvements
    batch["findings"][0].update(
        problem_key="scotland-connector/walk-diversity",
        ready=False,
        title="connector: choose deterministic walk diversity policy",
        evidence="82db9a0f: ranking is verdict, good hours, distance, ID",
        acceptance_tests="Define policy before agent-ready; no numerical Sunday forecast supplied",
    )
    assert (
        service.submit(batch, actor="operator:test")["reason"]
        == "no_actionable_findings"
    )
    assert not writes


def test_improvement_concurrent_batches_have_one_durable_candidate(improvements):
    service, _engine, _clock, policy, batch, _inventory, _quota, writes = improvements
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: service.submit(batch, actor="operator:test"), range(2))
        )
    assert sorted(result["reason"] for result in results) == ["candidates", "cooldown"]
    producer.problem_issues_tick(policy)
    assert len(writes) == 1


def test_improvement_history_requires_merged_useful_delivery(improvements):
    service, engine, _clock, _policy, batch, _inventory, _quota, _writes = improvements
    key = "a" * 64
    with Session(engine) as session:
        session.add_all(
            [
                FactoryAudit(
                    actor="test",
                    action="improvement_attempt",
                    created_at=START - timedelta(days=i),
                    detail_json=dumps(
                        {
                            "fingerprint": key if i == 2 else str(i) * 64,
                            "reason": "candidates",
                        }
                    ),
                )
                for i in (2, 3, 4)
            ]
        )
        session.add(
            FactoryAudit(
                actor="test",
                action="problem_issue_created",
                created_at=START - timedelta(days=1),
                detail_json=dumps({"fingerprint": key, "issue_number": 8000}),
            )
        )
        receipt = session.exec(select(FactoryReceipt)).first()
        receipt.repo = service.REPO
        receipt.issue_number = 8000
        receipt.state = "succeeded"
        session.commit()
    assert (
        service.submit(batch, actor="operator:test")["reason"]
        == "usefulness_review_required"
    )
    with Session(engine) as session:
        session.add(
            FactoryAudit(
                actor="test",
                action="owned_pr_merged",
                task_id="t-1",
                created_at=START,
                detail_json=dumps({"pr_number": 8001, "head_sha": "b" * 40}),
            )
        )
        session.commit()
    assert service.submit(batch, actor="operator:test")["reason"] == "candidates"


def test_improvement_refinement_success_is_not_useful_delivery(improvements):
    service, engine, _clock, _policy, batch, _inventory, _quota, _writes = improvements
    key = "a" * 64
    with Session(engine) as session:
        session.add_all(
            [
                FactoryAudit(
                    actor="test",
                    action="improvement_attempt",
                    created_at=START - timedelta(days=i),
                    detail_json=dumps({"fingerprint": key}),
                )
                for i in (2, 3, 4)
            ]
        )
        session.add_all(
            [
                FactoryAudit(
                    actor="test",
                    action="problem_issue_created",
                    detail_json=dumps({"fingerprint": key, "issue_number": 8000}),
                ),
                FactoryAudit(
                    actor="test",
                    action="owned_pr_merged",
                    task_id="t-1",
                    detail_json=dumps({"pr_number": 8001, "head_sha": "b" * 40}),
                ),
            ]
        )
        receipt = session.exec(select(FactoryReceipt)).first()
        receipt.repo, receipt.issue_number, receipt.task_class = (
            service.REPO,
            8000,
            "refine",
        )
        session.commit()
    assert (
        service.submit(batch, actor="operator:test")["reason"]
        == "usefulness_review_required"
    )


def test_new_important_ready_issue_wins_over_recent_empty_sweep(improvements):
    service, _engine, _clock, policy, batch, inventory, _quota, writes = improvements
    inventory.extend(
        [
            {
                "kind": "issue",
                "number": 7001,
                "title": "existing bug",
                "labels": [{"name": "agent-ready"}, {"name": "bug"}],
                "assignees": [],
            },
            {
                "kind": "issue",
                "number": 7002,
                "title": "new critical bug",
                "labels": [{"name": "agent-ready"}, {"name": "critical"}],
                "assignees": [],
            },
        ]
    )
    batch["review_digest"] = service.context()["review_digest"]
    result = service.submit(batch, actor="operator:test")
    assert result["reason"] == "existing_ready_work"
    assert result["ready_issue_numbers"] == [7002, 7001]
    producer.problem_issues_tick(policy)
    assert not writes


@pytest.mark.parametrize(
    "limits",
    [
        {"cooldown_seconds": 3599},
        {"max_findings": 21},
        {"max_unproven_attempts": 4},
        {"unknown": 1},
    ],
)
def test_improvement_policy_limits_reject_unbounded_values(limits):
    with pytest.raises(ValueError):
        controls._validate_problem_issues({"improvement_limits": limits})


def test_improvement_cooldown_is_configurable_without_changing_intake(improvements):
    service, engine, _clock, policy, batch, _inventory, _quota, writes = improvements
    with Session(engine) as session:
        session.add(
            FactoryAudit(
                actor="test",
                action="improvement_attempt",
                created_at=START - timedelta(hours=2),
                detail_json=dumps({"reason": "no_actionable_findings"}),
            )
        )
        session.commit()
    assert service.submit(batch, actor="operator:test")["reason"] == "cooldown"
    policy["problem_issues"]["improvement_limits"] = {
        "cooldown_seconds": 3600,
        "max_findings": 1,
    }
    with Session(engine) as session:
        session.get(FactoryControl, "factory").policy_json = dumps(policy)
        session.commit()
    batch["review_digest"] = service.context()["review_digest"]
    assert service.submit(batch, actor="operator:test")["reason"] == "candidates"
    producer.problem_issues_tick(policy)
    assert len(writes) == 1
    assert "intake" not in policy  # generation tuning does not alter intake policy


def test_improvement_ineligible_context_does_not_use_scouting_trial(improvements):
    service, engine, _clock, _policy, batch, _inventory, quota, _writes = improvements
    quota["available"] = False
    for _ in range(4):
        assert (
            service.submit(batch, actor="operator:test")["reason"]
            == "no_verified_spare_subscription"
        )
    with Session(engine) as session:
        assert not session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "improvement_attempt")
        ).all()


def test_improvement_operator_review_reopens_trial_without_forgetting_duplicates(
    improvements,
):
    service, engine, _clock, policy, batch, _inventory, _quota, _writes = improvements
    from factory.orchestration.factory_improvement_preview import fingerprint

    duplicate = fingerprint(service.REPO, batch["findings"][0]["problem_key"])
    with Session(engine) as session:
        rows = [
            FactoryAudit(
                actor="test",
                action="improvement_attempt",
                created_at=START - timedelta(days=i),
                detail_json=dumps({"fingerprint": duplicate, "reason": "candidates"}),
            )
            for i in (4, 3, 2)
        ]
        session.add_all(rows)
        session.commit()
        review_id = rows[-1].id
    assert (
        service.submit(batch, actor="operator:test")["reason"]
        == "usefulness_review_required"
    )
    policy["problem_issues"]["improvement_review"] = {
        "through_audit_id": review_id,
        "reason": "Reviewed failed attempts; new reproduction evidence is available",
    }
    with Session(engine) as session:
        session.get(FactoryControl, "factory").policy_json = dumps(policy)
        session.commit()
    batch["review_digest"] = service.context()["review_digest"]
    result = service.submit(batch, actor="operator:test")
    assert result["reason"] == "no_actionable_findings"
    assert result["skipped"] == ["duplicate_or_owned"]


def test_improvement_review_requires_existing_attempt_and_reason(improvements):
    service, engine, _clock, policy, _batch, _inventory, _quota, _writes = improvements
    with pytest.raises(ValueError, match="requires a reason"):
        controls._validate_problem_issues(
            {"improvement_review": {"through_audit_id": 1, "reason": ""}}
        )
    policy["problem_issues"]["improvement_review"] = {
        "through_audit_id": 999999,
        "reason": "reviewed",
    }
    with Session(engine) as session:
        session.get(FactoryControl, "factory").policy_json = dumps(policy)
        session.commit()
    with pytest.raises(ValueError, match="exact_attempt_audit"):
        service.context()


def test_improvement_old_duplicate_survives_bounded_history(improvements):
    service, engine, _clock, policy, batch, _inventory, _quota, _writes = improvements
    from factory.orchestration.factory_improvement_preview import fingerprint

    with Session(engine) as session:
        for i in range(1002):
            row = FactoryAudit(
                actor="test",
                action="improvement_attempt",
                created_at=START - timedelta(days=1003 - i),
                detail_json=dumps(
                    {
                        "fingerprint": fingerprint(
                            service.REPO, batch["findings"][0]["problem_key"]
                        )
                        if i == 0
                        else None,
                        "reason": "candidates" if i == 0 else "no_actionable_findings",
                    }
                ),
            )
            session.add(row)
        session.commit()
        session.refresh(row)
        policy["problem_issues"]["improvement_review"] = {
            "through_audit_id": row.id,
            "reason": "reviewed evidence",
        }
        session.get(FactoryControl, "factory").policy_json = dumps(policy)
        session.commit()
    current = service.context()
    assert len(current["improvement_history"]) == 20
    batch["review_digest"] = current["review_digest"]
    result = service.submit(batch, actor="operator:test")
    assert result["reason"] == "duplicate_or_owned"
    assert result["drafts"] == []


def test_scout_context_excludes_only_exact_durable_owner(improvements):
    service, engine, _clock, _policy, _batch, _inventory, _quota, _writes = improvements
    with Session(engine) as session:
        row = session.exec(select(FactoryReceipt)).first()
        row.repo = service.REPO
        row.actor = "factory:improvement-scout"
        row.task_class = "advisory-diagnosis"
        row.state = "admitted"
        receipt_id = row.id
        session.commit()
    with pytest.raises(ValueError, match="scout_claim_missing"):
        service.context(scout_receipt_id=receipt_id)
    with controls._locked_session() as (session, _):
        controls._audit(
            session, "factory:improvement-scout", "scout_claim", receipt_id=receipt_id
        )
    assert service.context()["intake_complete"] is False
    assert service.context(scout_receipt_id=receipt_id)["intake_complete"] is True
    with Session(engine) as session:
        session.add(
            FactoryReceipt(
                repo=service.REPO,
                issue_number=999,
                title="Other work",
                body="",
                url="https://github.com/jomcgi-org/homelab/issues/999",
                actor="operator",
                state="queued",
            )
        )
        session.commit()
    current = service.context(scout_receipt_id=receipt_id)
    assert not current["intake_complete"]
    assert [owner["issue_number"] for owner in current["owners"]] == [999]


def test_scout_review_requires_successful_independent_sessions(improvements):
    service, engine, _clock, _policy, batch, _inventory, _quota, _writes = improvements
    from factory.orchestration.models import SwarmNodeRun

    SwarmNodeRun.__table__.create(engine)
    with Session(engine) as session:
        row = session.exec(select(FactoryReceipt)).first()
        row.repo, row.actor, row.task_class = (
            service.REPO,
            "factory:improvement-scout",
            "advisory-diagnosis",
        )
        receipt_id, task_id = row.id, row.task_id
        collect = SwarmNodeRun(
            task_id=task_id,
            node_key="scout_collect_1",
            attempt=1,
            status="succeeded",
            session_id=1,
        )
        review = SwarmNodeRun(
            task_id=task_id,
            node_key="scout_review_1",
            attempt=1,
            status="succeeded",
            session_id=1,
        )
        session.add_all([collect, review])
        session.commit()
        session.refresh(review)
        review_id = review.id
    with controls._locked_session() as (session, _):
        controls._audit(
            session, "factory:improvement-scout", "scout_claim", receipt_id=receipt_id
        )
        controls._audit(
            session,
            "factory:improvement-scout",
            "scout_review",
            task_id=task_id,
            receipt_id=receipt_id,
            approved=True,
            review_digest=batch["review_digest"],
            findings_digest=digest(batch["findings"]),
            run_id=review_id,
        )
    with Session(engine) as session:
        with pytest.raises(ValueError, match="scout_review_not_verified"):
            service._verify_scout_review(session, receipt_id, batch)
        session.get(SwarmNodeRun, review_id).session_id = 2
        session.commit()
        service._verify_scout_review(session, receipt_id, batch)
        session.get(SwarmNodeRun, review_id).status = "failed"
        session.commit()
        with pytest.raises(ValueError, match="scout_review_not_verified"):
            service._verify_scout_review(session, receipt_id, batch)
