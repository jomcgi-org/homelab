"""Exact-event problem issue production and failure-path regressions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import factory.orchestration.factory_controls as controls
import factory.orchestration.factory_problem_issues as producer
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
    state = {"issues": list(issues or []), "writes": [], "reads": []}

    def read(_repo, suffix):
        state["reads"].append(suffix)
        return list(state["issues"])

    def write(_repo, payload):
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


def test_uncertain_write_reconciles_marker_after_backoff_without_reposting(
    db, monkeypatch
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
    uncertain_audit = details(audits(engine, "problem_issue_write_uncertain"))[0]
    assert datetime.fromisoformat(uncertain_audit["next_retry_at"]) == clock[
        "now"
    ] + timedelta(minutes=2)

    producer.problem_issues_tick(selected)
    assert len(state["reads"]) == 1
    clock["now"] += timedelta(minutes=2)
    producer.problem_issues_tick(selected)
    assert len(state["writes"]) == 1
    assert details(audits(engine, "problem_issue_reconciled"))[-1]["issue_numbers"] == [
        7331
    ]


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


def test_issue_lookup_is_bounded_to_two_pages_of_one_hundred(db, monkeypatch):
    engine, _clock = db
    selected = policy()
    enable_after_watermark(engine, selected)
    add_event(engine, workflow="paged")
    calls = []

    def read(_repo, suffix):
        calls.append(suffix)
        return [{"number": number, "body": ""} for number in range(100)]

    monkeypatch.setattr(producer, "github_list", read)
    monkeypatch.setattr(
        producer,
        "github_write",
        lambda _repo, payload: {"number": 8000, **payload},
    )
    producer.problem_issues_tick(selected)
    assert len(calls) == 2
    assert "per_page=100&page=1" in calls[0]
    assert "per_page=100&page=2" in calls[1]
    capped = details(audits(engine, "problem_issue_issue_scan_capped"))
    assert capped[-1]["pages"] == 2 and capped[-1]["per_page"] == 100
