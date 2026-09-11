"""Autonomous intake selection, bounds, and audit regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import swarm.factory_controls as controls
import swarm.factory_intake_loop as intake_loop
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import SwarmTask

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


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
    monkeypatch.setattr(intake_loop, "_now", lambda: NOW)
    # intake_state reads the clock from factory_controls, where it lives
    # so the board can render the block without linking the reconciler.
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    yield engine
    engine.dispose()


def policy(**intake):
    return {
        "repo": "owner/repo",
        "intake": {"enabled": True, "exclude_labels": [], **intake},
    }


def issue(number: int, labels=(), **overrides):
    value = {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": "open",
        "assignees": [],
        "labels": [{"name": label} for label in labels],
        "created_at": f"2026-09-{number:02d}T00:00:00Z",
    }
    value.update(overrides)
    return value


def fake_pages(monkeypatch, issues, pulls=()):
    calls = []

    def github_list(_repo, suffix):
        calls.append(suffix)
        return list(pulls) if suffix.startswith("pulls?") else list(issues)

    monkeypatch.setattr(intake_loop, "github_list", github_list)
    return calls


def audits(db, action):
    with Session(db) as session:
        return session.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == action)
            .order_by(FactoryAudit.id)
        ).all()


@pytest.mark.parametrize(
    ("labels", "refine", "expected"),
    [
        ({"security-finding"}, False, ("judgment-analysis", "label security-finding")),
        ({"needs-thought"}, False, ("judgment-analysis", "label needs-thought")),
        ({"bug"}, False, ("bug-fix", "label bug")),
        ({"documentation"}, False, ("docs", "label documentation")),
        ({"todo"}, False, ("mechanical-refactor", "label todo")),
        ({"unrecognised"}, False, ("bug-fix", "default")),
        ({"bug"}, True, ("refine", "refine candidate")),
    ],
)
def test_derive_task_class(labels, refine, expected):
    assert intake_loop.derive_task_class(labels, refine=refine) == expected


def test_derive_task_class_uses_label_precedence():
    assert intake_loop.derive_task_class({"needs-thought", "bug"}, refine=False) == (
        "judgment-analysis",
        "label needs-thought",
    )


def test_disabled_intake_has_no_github_or_audit(db, monkeypatch):
    monkeypatch.setattr(
        intake_loop,
        "github_list",
        lambda *_args: pytest.fail("disabled intake made a GitHub call"),
    )
    assert intake_loop.intake_tick({"repo": "owner/repo"}, generation=0) is None
    assert audits(db, "intake_idle") == []


@pytest.mark.parametrize("state", ["queued", "admitted", "uncertain"])
def test_current_generation_work_blocks_intake(db, monkeypatch, state):
    fake_pages(monkeypatch, [issue(1, ["agent-ready"])])
    with Session(db) as session:
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=99,
                generation=3,
                title="busy",
                body="",
                url="https://github.com/owner/repo/issues/99",
                actor="test",
                state=state,
            )
        )
        session.commit()
    assert intake_loop.intake_tick(policy(), generation=3) is None


def test_ranking_prefers_delivery_label_rank_then_age(db, monkeypatch):
    fake_pages(
        monkeypatch,
        [issue(1), issue(2, ["bug"]), issue(3, ["critical"])],
    )
    result = intake_loop.intake_tick(
        policy(labels=["agent-ready", "bug", "critical"], refine_enabled=True),
        generation=0,
    )
    assert result["receipt"]["issue_number"] == 3
    assert result["receipt"]["task_class"] == "bug-fix"


def test_delivery_without_rank_beats_critical_refine(db, monkeypatch):
    fake_pages(monkeypatch, [issue(1, ["critical"]), issue(2, ["agent-ready"])])
    result = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True), generation=0
    )
    assert result["receipt"]["issue_number"] == 2


def test_all_exclusion_reasons_are_first_match_counts(db, monkeypatch):
    listed = [
        issue(1, pull_request={}),
        issue(2, state="closed"),
        issue(3, assignees=[{"login": "owner"}]),
        issue(4, ["blocked"]),
        issue(12, ["agent-ready"]),
        issue(6, ["agent-ready"]),
        issue(7, ["agent-ready"]),
        issue(8),
    ]
    fake_pages(monkeypatch, listed, [{"title": "Fix", "body": "Fixes #12"}])
    with Session(db) as session:
        session.add_all(
            [
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=6,
                    generation=9,
                    title="cooldown",
                    body="",
                    url="https://github.com/owner/repo/issues/6",
                    actor="test",
                    state="failed",
                    updated_at=NOW - timedelta(hours=1),
                ),
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=7,
                    generation=0,
                    title="seen",
                    body="",
                    url="https://github.com/owner/repo/issues/7",
                    actor="test",
                    state="succeeded",
                ),
            ]
        )
        session.commit()
    assert (
        intake_loop.intake_tick(
            policy(exclude_labels=["blocked"], refine_enabled=False), generation=0
        )
        is None
    )
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["listed"] == 8
    assert detail["excluded"] == {
        "pull_request": 1,
        "not_open": 1,
        "assigned": 1,
        "excluded_label": 1,
        "linked_pr": 1,
        "cooldown": 1,
        "already_received": 1,
        "refine_disabled": 1,
    }


def test_expired_cooldown_is_admissible(db, monkeypatch):
    fake_pages(monkeypatch, [issue(6, ["agent-ready"])])
    with Session(db) as session:
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=6,
                generation=9,
                title="old failure",
                body="",
                url="https://github.com/owner/repo/issues/6",
                actor="test",
                state="failed",
                updated_at=NOW - timedelta(hours=25),
            )
        )
        session.commit()
    assert intake_loop.intake_tick(policy(cooldown_hours=24), generation=0)["ok"]


def test_daily_cap_and_expiry(db, monkeypatch):
    calls = fake_pages(monkeypatch, [issue(1, ["agent-ready"])])
    with Session(db) as session:
        session.add_all(
            [
                FactoryAudit(
                    actor="factory:intake",
                    action="intake_admitted",
                    created_at=NOW - timedelta(hours=1),
                )
                for _ in range(2)
            ]
        )
        session.commit()
    assert intake_loop.intake_tick(policy(max_per_day=2), generation=0) is None
    assert calls == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail == {"admitted_today": 2, "max_per_day": 2, "reason": "daily_cap"}
    with Session(db) as session:
        for row in session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "intake_admitted")
        ).all():
            row.created_at = NOW - timedelta(hours=25)
            session.add(row)
        session.commit()
    assert intake_loop.intake_tick(policy(max_per_day=2), generation=0)["ok"]


def test_exactly_one_admission_and_bounded_evidence(db, monkeypatch):
    fake_pages(monkeypatch, [issue(n, ["agent-ready"]) for n in range(1, 26)])
    result = intake_loop.intake_tick(policy(), generation=0)
    assert result["created"]
    with Session(db) as session:
        assert len(session.exec(select(FactoryReceipt)).all()) == 1
    detail = json.loads(audits(db, "intake_admitted")[0].detail_json)
    assert len(detail["candidates"]) == 20
    assert detail["excluded"] == {}


def test_admission_and_audit_carry_derived_class(db, monkeypatch):
    fake_pages(
        monkeypatch,
        [
            issue(1, ["agent-ready", "documentation"]),
            issue(2, ["agent-ready", "todo"]),
        ],
    )
    result = intake_loop.intake_tick(policy(), generation=0)
    assert result["receipt"]["task_class"] == "docs"
    detail = json.loads(audits(db, "intake_admitted")[0].detail_json)
    assert detail["task_class"] == "docs"
    assert detail["class_reason"] == "label documentation"
    assert [candidate["task_class"] for candidate in detail["candidates"]] == [
        "docs",
        "mechanical-refactor",
    ]


def test_idle_audit_is_hourly(db, monkeypatch):
    fake_pages(monkeypatch, [])
    assert intake_loop.intake_tick(policy(), generation=0) is None
    assert intake_loop.intake_tick(policy(), generation=0) is None
    assert len(audits(db, "intake_idle")) == 1
    with Session(db) as session:
        row = session.exec(select(FactoryAudit)).one()
        row.created_at = NOW - timedelta(minutes=61)
        session.add(row)
        session.commit()
    assert intake_loop.intake_tick(policy(), generation=0) is None
    assert len(audits(db, "intake_idle")) == 2


def test_refine_enabled_sets_class_while_labelled_stays_delivery(db, monkeypatch):
    fake_pages(monkeypatch, [issue(1)])
    refined = intake_loop.intake_tick(policy(refine_enabled=True), generation=0)
    assert refined["receipt"]["task_class"] == "refine"
    with Session(db) as session:
        session.exec(select(FactoryReceipt)).one().state = "succeeded"
        session.commit()
    fake_pages(monkeypatch, [issue(2, ["agent-ready"])])
    delivered = intake_loop.intake_tick(policy(refine_enabled=True), generation=0)
    assert delivered["receipt"]["task_class"] == "bug-fix"


def test_intake_state_reports_policy_usage_and_latest_audits(db):
    with Session(db) as session:
        session.add_all(
            [
                FactoryAudit(
                    actor="factory:intake",
                    action="intake_admitted",
                    detail_json='{"issue_number":1}',
                    created_at=NOW - timedelta(hours=1),
                ),
                FactoryAudit(
                    actor="factory:intake",
                    action="intake_idle",
                    detail_json='{"listed":0}',
                    created_at=NOW - timedelta(minutes=1),
                ),
            ]
        )
        session.commit()
    state = intake_loop.intake_state(policy(max_per_day=7))
    assert state["policy"]["enabled"] is True
    assert state["admitted_today"] == 1 and state["max_per_day"] == 7
    assert state["last_admitted"]["detail"] == {"issue_number": 1}
    assert state["last_idle"]["detail"] == {"listed": 0}
