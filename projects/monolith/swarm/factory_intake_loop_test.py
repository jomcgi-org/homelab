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
    # Both lanes open: the ceiling bounds their sum, so at one the advisory
    # lane could never hold anything.
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    yield engine
    engine.dispose()


def policy(**intake):
    return {
        "repo": "owner/repo",
        "max_tasks": {"delivery": 1, "advisory": 1},
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


def intake_receipt(session, number, task_class="bug-fix", **overrides):
    """One admission the daily cap may or may not count, already settled.

    Terminal so it holds no lane: the cap reads the whole rolling window,
    not just what is still running.
    """
    values = {
        "repo": "owner/repo",
        "issue_number": number,
        "generation": 0,
        "title": f"admitted {number}",
        "body": "",
        "url": f"https://github.com/owner/repo/issues/{number}",
        "actor": intake_loop.ACTOR,
        "task_class": task_class,
        "state": "succeeded",
        "created_at": NOW - timedelta(hours=1),
    }
    values.update(overrides)
    session.add(FactoryReceipt(**values))


def release_sweep(db):
    """Age the sweep clock so the next tick is allowed to read GitHub.

    _audit stamps rows from the real clock while the fixture pins the module
    clock, so a test cannot move time by moving NOW. Ageing the row is the
    same statement in the terms the gate actually reads.
    """
    with Session(db) as session:
        for row in session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "intake_swept")
        ).all():
            row.created_at = NOW - timedelta(hours=2)
            session.add(row)
        session.commit()


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
    assert intake_loop.intake_tick({"repo": "owner/repo"}, generation=0) == []
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
    assert intake_loop.intake_tick(policy(), generation=3) == []


def test_ranking_prefers_delivery_label_rank_then_age(db, monkeypatch):
    fake_pages(
        monkeypatch,
        [issue(1), issue(2, ["bug"]), issue(3, ["critical"])],
    )
    result = intake_loop.intake_tick(
        policy(labels=["agent-ready", "bug", "critical"], refine_enabled=True),
        generation=0,
    )
    assert result[0]["receipt"]["issue_number"] == 3
    assert result[0]["receipt"]["task_class"] == "bug-fix"


def test_delivery_without_rank_beats_critical_refine(db, monkeypatch):
    fake_pages(monkeypatch, [issue(1, ["critical"]), issue(2, ["agent-ready"])])
    result = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True), generation=0
    )
    assert result[0]["receipt"]["issue_number"] == 2


def test_all_exclusion_reasons_are_first_match_counts(db, monkeypatch):
    listed = [
        issue(1, pull_request={}),
        issue(2, state="closed"),
        issue(3, assignees=[{"login": "owner"}]),
        issue(4, ["blocked"]),
        issue(12, ["agent-ready"]),
        issue(6, ["agent-ready"]),
        issue(7, ["agent-ready"]),
        issue(9, ["agent-ready"]),
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
                    generation=4,
                    title="delivered",
                    body="",
                    url="https://github.com/owner/repo/issues/7",
                    actor="test",
                    state="succeeded",
                ),
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=9,
                    generation=0,
                    title="seen",
                    body="",
                    url="https://github.com/owner/repo/issues/9",
                    actor="test",
                    state="failed",
                    updated_at=NOW - timedelta(hours=48),
                ),
            ]
        )
        session.commit()
    assert (
        intake_loop.intake_tick(
            policy(exclude_labels=["blocked"], refine_enabled=False), generation=0
        )
        == []
    )
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["listed"] == 9
    assert detail["excluded"] == {
        "pull_request": 1,
        "not_open": 1,
        "assigned": 1,
        "excluded_label": 1,
        "linked_pr": 1,
        "delivered": 1,
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
    assert intake_loop.intake_tick(policy(cooldown_hours=24), generation=0)[0]["ok"]


def test_daily_cap_counts_deliveries_and_expires(db, monkeypatch):
    """The cap bounds delivery churn, so only delivery admissions fill it.

    A refine costs cents and buys no review, so an advisory admission inside
    the window is invisible to the cap however many of them there were.
    """
    fake_pages(monkeypatch, [issue(1, ["agent-ready"])])
    with Session(db) as session:
        intake_receipt(session, 90)
        intake_receipt(session, 91)
        for number in (92, 93, 94):
            intake_receipt(session, number, task_class="refine")
        # An operator allowlist admission is not autonomous intake's to count.
        intake_receipt(session, 95, actor="operator")
        session.commit()
    assert intake_loop.intake_tick(policy(max_per_day=2), generation=0) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail == {"admitted_today": 2, "max_per_day": 2, "reason": "daily_cap"}
    with Session(db) as session:
        for row in session.exec(
            select(FactoryReceipt).where(FactoryReceipt.issue_number.in_((90, 91)))
        ).all():
            row.created_at = NOW - timedelta(hours=25)
            session.add(row)
        session.commit()
    release_sweep(db)
    assert intake_loop.intake_tick(policy(max_per_day=2), generation=0)[0]["ok"]


def test_exactly_one_admission_and_bounded_evidence(db, monkeypatch):
    fake_pages(monkeypatch, [issue(n, ["agent-ready"]) for n in range(1, 26)])
    result = intake_loop.intake_tick(policy(), generation=0)
    assert result[0]["created"]
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
    assert result[0]["receipt"]["task_class"] == "docs"
    detail = json.loads(audits(db, "intake_admitted")[0].detail_json)
    assert detail["task_class"] == "docs"
    assert detail["class_reason"] == "label documentation"
    assert [candidate["task_class"] for candidate in detail["candidates"]] == [
        "docs",
        "mechanical-refactor",
    ]


def test_idle_audit_is_hourly(db, monkeypatch):
    fake_pages(monkeypatch, [])
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert len(audits(db, "intake_idle")) == 1
    with Session(db) as session:
        row = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "intake_idle")
        ).one()
        row.created_at = NOW - timedelta(minutes=61)
        session.add(row)
        session.commit()
    release_sweep(db)
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert len(audits(db, "intake_idle")) == 2


def test_refine_enabled_sets_class_while_labelled_stays_delivery(db, monkeypatch):
    fake_pages(monkeypatch, [issue(1)])
    refined = intake_loop.intake_tick(policy(refine_enabled=True), generation=0)
    assert refined[0]["receipt"]["task_class"] == "refine"
    with Session(db) as session:
        session.exec(select(FactoryReceipt)).one().state = "succeeded"
        session.commit()
    fake_pages(monkeypatch, [issue(2, ["agent-ready"])])
    delivered = intake_loop.intake_tick(policy(refine_enabled=True), generation=0)
    assert delivered[0]["receipt"]["task_class"] == "bug-fix"


def test_intake_state_reports_policy_usage_and_latest_audits(db):
    with Session(db) as session:
        # The board shows usage against the cap, so it counts what the cap
        # counts: delivery admissions intake made, and nothing else.
        intake_receipt(session, 90)
        intake_receipt(session, 91, task_class="refine")
        intake_receipt(session, 92, actor="operator")
        intake_receipt(session, 93, created_at=NOW - timedelta(hours=25))
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


def test_a_quiet_lane_sweeps_github_at_most_once_an_hour(db, monkeypatch):
    """The tick runs every 15 seconds; the sweep must not."""
    calls = fake_pages(monkeypatch, [])
    assert intake_loop.intake_tick(policy(), generation=0) == []
    first = len(calls)
    assert first > 0
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert len(calls) == first
    assert len(audits(db, "intake_idle")) == 1


def test_an_hour_later_the_sweep_runs_again(db, monkeypatch):
    calls = fake_pages(monkeypatch, [])
    assert intake_loop.intake_tick(policy(), generation=0) == []
    swept = len(calls)
    with Session(db) as session:
        row = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "intake_idle")
        ).one()
        row.created_at = NOW - timedelta(minutes=61)
        session.add(row)
        session.commit()
    release_sweep(db)
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert len(calls) > swept
    assert len(audits(db, "intake_idle")) == 2


def test_the_daily_cap_does_not_blind_the_sweep_for_an_hour(db, monkeypatch):
    """The idle row the cap writes must not be read as the sweep clock."""
    calls = fake_pages(monkeypatch, [issue(1, ["agent-ready"])])
    with Session(db) as session:
        intake_receipt(session, 90)
        session.commit()
    assert intake_loop.intake_tick(policy(max_per_day=1), generation=0) == []
    swept = len(calls)
    assert swept > 0
    assert json.loads(audits(db, "intake_idle")[0].detail_json)["reason"] == "daily_cap"
    # The idle row is a minute old. Only the sweep clock gates the read, so
    # raising the cap takes effect on the next tick rather than in an hour.
    release_sweep(db)
    assert intake_loop.intake_tick(policy(max_per_day=5), generation=0)[0]["ok"]
    assert len(calls) > swept


def test_a_settlement_releases_the_sweep_immediately(db, monkeypatch):
    calls = fake_pages(monkeypatch, [])
    assert intake_loop.intake_tick(policy(), generation=0) == []
    swept = len(calls)
    with Session(db) as session:
        marked = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "intake_swept")
        ).one()
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=41,
                generation=0,
                title="settled",
                body="",
                url="https://github.com/owner/repo/issues/41",
                actor="test",
                state="succeeded",
                updated_at=marked.created_at,
            )
        )
        session.commit()
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert len(calls) > swept


def test_a_github_failure_is_audited_not_only_logged(db, monkeypatch):
    calls = []

    def boom(_repo, _suffix):
        calls.append(_suffix)
        raise RuntimeError("403 rate limited")

    monkeypatch.setattr(intake_loop, "github_list", boom)
    assert intake_loop.intake_tick(policy(), generation=0) == []
    # A failed sweep still spends the clock, so the next tick does not retry
    # a rate limit every fifteen seconds.
    assert intake_loop.intake_tick(policy(), generation=0) == []
    assert len(calls) == 1
    release_sweep(db)
    assert intake_loop.intake_tick(policy(), generation=0) == []
    rows = audits(db, "intake_error")
    assert len(rows) == 1
    detail = json.loads(rows[0].detail_json)
    assert detail["stage"] == "listing" and detail["error"] == "RuntimeError"
    assert "403 rate limited" not in rows[0].detail_json


def test_a_truncated_sweep_says_so(db, monkeypatch):
    full = [issue(number) for number in range(1, intake_loop.PAGE_SIZE + 1)]

    def github_list(_repo, suffix):
        return [] if suffix.startswith("pulls?") else list(full)

    monkeypatch.setattr(intake_loop, "github_list", github_list)
    assert intake_loop.intake_tick(policy(refine_enabled=False), generation=0) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["truncated"] is True


def test_the_sweep_asks_for_the_oldest_first(db, monkeypatch):
    calls = fake_pages(monkeypatch, [])
    intake_loop.intake_tick(policy(), generation=0)
    assert all("sort=created&direction=asc" in suffix for suffix in calls)


def test_an_unreadable_created_at_sorts_last(db, monkeypatch):
    fake_pages(
        monkeypatch,
        [
            issue(1, ["agent-ready"], created_at="not a date"),
            issue(2, ["agent-ready"]),
        ],
    )
    result = intake_loop.intake_tick(policy(labels=["agent-ready"]), generation=0)
    assert result[0]["receipt"]["issue_number"] == 2


def test_a_refined_issue_is_delivered_in_the_same_generation(db, monkeypatch):
    """The whole point of scoping already_received to the class."""
    fake_pages(monkeypatch, [issue(9)])
    refined = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True), generation=0
    )
    assert refined[0]["receipt"]["task_class"] == "refine"
    with Session(db) as session:
        row = session.get(FactoryReceipt, refined[0]["receipt"]["id"])
        row.state = "succeeded"
        row.updated_at = NOW
        session.add(row)
        session.commit()
    release_sweep(db)
    # The refine pass applied agent-ready, so the next sweep sees a delivery.
    fake_pages(monkeypatch, [issue(9, ["agent-ready"])])
    delivered = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True), generation=0
    )
    assert delivered
    assert delivered[0]["receipt"]["task_class"] == "bug-fix"
    assert delivered[0]["receipt"]["id"] != refined[0]["receipt"]["id"]


def test_the_same_class_twice_in_one_generation_is_refused(db, monkeypatch):
    fake_pages(monkeypatch, [issue(9, ["agent-ready"])])
    first = intake_loop.intake_tick(policy(labels=["agent-ready"]), generation=0)
    with Session(db) as session:
        row = session.get(FactoryReceipt, first[0]["receipt"]["id"])
        # Failed with its cooldown spent, not succeeded: a delivered issue is
        # excluded before its class is ever read.
        row.state = "failed"
        row.updated_at = NOW - timedelta(hours=48)
        session.add(row)
        session.commit()
    release_sweep(db)
    fake_pages(monkeypatch, [issue(9, ["agent-ready"])])
    assert intake_loop.intake_tick(policy(labels=["agent-ready"]), generation=0) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["excluded"]["already_received"] == 1


def test_a_malformed_entry_is_not_a_closed_issue(db, monkeypatch):
    fake_pages(monkeypatch, ["not an object", issue(2, state="closed")])
    assert intake_loop.intake_tick(policy(), generation=0) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["excluded"] == {"malformed": 1, "not_open": 1}


def test_the_intake_actor_is_the_one_admission_keys_on():
    from swarm.factory_intake import INTAKE_ACTOR

    assert intake_loop.ACTOR == INTAKE_ACTOR


def test_one_candidate_per_lane_enters_on_the_same_tick(db, monkeypatch):
    fake_pages(monkeypatch, [issue(1, ["agent-ready"]), issue(2), issue(3)])
    admitted = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True), generation=0
    )
    assert [row["receipt"]["task_class"] for row in admitted] == ["bug-fix", "refine"]
    assert [row["receipt"]["issue_number"] for row in admitted] == [1, 2]
    with Session(db) as session:
        assert len(session.exec(select(FactoryReceipt)).all()) == 2


def test_a_full_lane_is_skipped_and_counted(db, monkeypatch):
    fake_pages(monkeypatch, [issue(1, ["agent-ready"]), issue(2)])
    first = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True), generation=0
    )
    assert len(first) == 2
    release_sweep(db)
    fake_pages(monkeypatch, [issue(3, ["agent-ready"]), issue(4)])
    assert (
        intake_loop.intake_tick(
            policy(labels=["agent-ready"], refine_enabled=True), generation=0
        )
        == []
    )


def test_a_shut_lane_leaves_the_other_one_working(db, monkeypatch):
    fake_pages(monkeypatch, [issue(1, ["agent-ready"]), issue(2)])
    admitted = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True),
        generation=0,
        lanes=("advisory",),
    )
    assert [row["receipt"]["task_class"] for row in admitted] == ["refine"]
    detail = json.loads(audits(db, "intake_admitted")[0].detail_json)
    assert detail["lane"] == "advisory"
    assert detail["excluded"]["lane_full"] == 1


def test_the_daily_cap_refuses_delivery_and_still_admits_advisory(db, monkeypatch):
    """The lanes are independent, and the cap bounds only one of them."""
    fake_pages(monkeypatch, [issue(1, ["agent-ready"]), issue(2)])
    with Session(db) as session:
        intake_receipt(session, 90)
        session.commit()
    admitted = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True, max_per_day=1),
        generation=0,
    )
    assert [row["receipt"]["task_class"] for row in admitted] == ["refine"]
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail == {"admitted_today": 1, "max_per_day": 1, "reason": "daily_cap"}
    # One idle row for the tick, not one per refused candidate.
    assert len(audits(db, "intake_idle")) == 1


def test_a_lane_filled_during_the_github_sweep_queues_nothing(db, monkeypatch):
    """The room that picked a candidate was measured before a sweep that takes
    seconds. Another replica can fill the lane in between."""
    fake_pages(monkeypatch, [issue(1, ["agent-ready"]), issue(2)])
    real_receive = intake_loop.receive_issue

    def fill_then_receive(*args, **kwargs):
        # Stand in for the replica that admitted while the sweep was running.
        with Session(db) as session:
            session.add(
                FactoryReceipt(
                    repo="owner/repo",
                    issue_number=99,
                    generation=0,
                    title="taken",
                    body="body",
                    url="https://github.com/owner/repo/issues/99",
                    actor=intake_loop.ACTOR,
                    task_class="refine",
                    state="admitted",
                )
            )
            session.commit()
        return real_receive(*args, **kwargs)

    monkeypatch.setattr(intake_loop, "receive_issue", fill_then_receive)
    admitted = intake_loop.intake_tick(
        policy(labels=["agent-ready"], refine_enabled=True), generation=0
    )
    # Delivery queued; the advisory lane filled behind it and queues nothing.
    assert [row["receipt"]["task_class"] for row in admitted] == ["bug-fix"]


def delivered_receipt(session, number, generation=1, **overrides):
    values = {
        "repo": "owner/repo",
        "issue_number": number,
        "generation": generation,
        "title": "delivered",
        "body": "",
        "url": f"https://github.com/owner/repo/issues/{number}",
        "actor": "test",
        "state": "succeeded",
        "updated_at": NOW - timedelta(days=3),
    }
    values.update(overrides)
    session.add(FactoryReceipt(**values))
    session.commit()


def test_a_delivered_issue_is_never_admitted_again(db, monkeypatch):
    """The #3877 defect: delivery left the issue open, so intake re-admitted it."""
    fake_pages(monkeypatch, [issue(11, ["agent-ready"])])
    with Session(db) as session:
        delivered_receipt(session, 11, generation=1)
    assert intake_loop.intake_tick(policy(), generation=2) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["excluded"] == {"delivered": 1}


def test_delivery_in_another_class_still_excludes_the_issue(db, monkeypatch):
    """The exclusion is the delivery, not the class or the generation it ran in."""
    fake_pages(monkeypatch, [issue(11, ["agent-ready"])])
    with Session(db) as session:
        delivered_receipt(session, 11, generation=1, task_class="docs")
    assert intake_loop.intake_tick(policy(), generation=7) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["excluded"] == {"delivered": 1}


def test_a_successful_refine_is_not_a_delivery(db, monkeypatch):
    """A refine settles succeeded when it has made the issue ready to deliver.

    Reading that as a delivery would block the delivery the refine just
    enabled, which is the whole reason receipt identity carries the class.
    """
    fake_pages(monkeypatch, [issue(11, ["agent-ready"])])
    with Session(db) as session:
        delivered_receipt(session, 11, generation=1, task_class="refine")
    assert intake_loop.intake_tick(policy(), generation=2)[0]["ok"]


def test_a_reopened_delivered_issue_stays_excluded(db, monkeypatch):
    """No cheap reopen signal exists, so a moved updated_at is not a licence.

    GitHub's issue listing has no reopen timestamp and updated_at moves on
    every comment, so reading it as a reopen would re-admit an issue somebody
    merely commented on. An operator names the issue in issue_numbers instead.
    """
    fake_pages(
        monkeypatch,
        [issue(11, ["agent-ready"], updated_at="2026-09-11T00:00:00Z")],
    )
    with Session(db) as session:
        delivered_receipt(session, 11, generation=1)
    assert intake_loop.intake_tick(policy(), generation=2) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["excluded"] == {"delivered": 1}


def test_a_failed_receipt_is_not_a_delivery(db, monkeypatch):
    """Only a succeeded receipt is a delivery; a failure serves its cooldown."""
    fake_pages(monkeypatch, [issue(11, ["agent-ready"])])
    with Session(db) as session:
        delivered_receipt(
            session,
            11,
            generation=1,
            state="failed",
            updated_at=NOW - timedelta(days=3),
        )
    assert intake_loop.intake_tick(policy(cooldown_hours=24), generation=2)[0]["ok"]


def test_an_issue_closed_on_github_is_never_a_candidate(db, monkeypatch):
    """The listing asks for open issues, and selection refuses anything else.

    A closed issue reaching the sweep would mean the state filter did not hold,
    and admitting it would open a delivery task for work that is already done.
    """
    fake_pages(monkeypatch, [issue(11, ["agent-ready"], state="closed")])
    assert intake_loop.intake_tick(policy(), generation=0) == []
    detail = json.loads(audits(db, "intake_idle")[0].detail_json)
    assert detail["excluded"] == {"not_open": 1}


def wide_policy(**intake):
    return {
        "repo": "owner/repo",
        "max_tasks": {"delivery": 4, "advisory": 8},
        "intake": {
            "enabled": True,
            "exclude_labels": [],
            "max_per_day": 50,
            **intake,
        },
    }


def held(db, state="queued"):
    with Session(db) as session:
        rows = session.exec(
            select(FactoryReceipt).where(FactoryReceipt.state == state)
        ).all()
        return {
            "delivery": sum(1 for row in rows if row.task_class != "refine"),
            "advisory": sum(1 for row in rows if row.task_class == "refine"),
        }


def test_a_ceiling_below_the_lanes_is_audited_once_an_hour(db, monkeypatch):
    """Chart configuration and posted policy can be set against each other."""
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    fake_pages(monkeypatch, [issue(1, ["agent-ready"])])
    intake_loop.intake_tick(wide_policy(), generation=0)
    recorded = [
        json.loads(row.detail_json) for row in audits(db, "lane_ceiling_below_lanes")
    ]
    assert recorded == [
        {"ceiling": 4, "delivery_max": 4, "advisory_max": 8, "lanes_sum": 12}
    ]
    release_sweep(db)
    fake_pages(monkeypatch, [issue(2, ["agent-ready"])])
    intake_loop.intake_tick(wide_policy(), generation=0)
    assert len(audits(db, "lane_ceiling_below_lanes")) == 1


def test_a_ceiling_that_covers_both_lanes_audits_nothing(db, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "12")
    fake_pages(monkeypatch, [issue(1, ["agent-ready"])])
    intake_loop.intake_tick(wide_policy(), generation=0)
    assert audits(db, "lane_ceiling_below_lanes") == []


def test_open_lanes_fill_over_consecutive_ticks_not_one_an_hour(db, monkeypatch):
    """A sweep takes one candidate per lane, so the hourly clock filled at one an hour."""
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "12")
    listed = [issue(number, ["agent-ready"]) for number in range(1, 7)] + [
        issue(number) for number in range(10, 20)
    ]
    calls = fake_pages(monkeypatch, listed)
    for _ in range(10):
        intake_loop.intake_tick(wide_policy(refine_enabled=True), generation=0)
    assert held(db) == {"delivery": 4, "advisory": 8}
    # Every tick that had room swept; the clock never held one back.
    assert len(calls) >= 16


def test_a_sweep_that_admits_nothing_waits_out_the_hour(db, monkeypatch):
    """The admission clause re-opens the sweep at most once per admission."""
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "12")
    calls = fake_pages(monkeypatch, [issue(1, ["blocked"])])
    assert (
        intake_loop.intake_tick(wide_policy(exclude_labels=["blocked"]), generation=0)
        == []
    )
    swept = len(calls)
    assert swept
    intake_loop.intake_tick(wide_policy(exclude_labels=["blocked"]), generation=0)
    assert len(calls) == swept


def test_one_admission_buys_exactly_one_extra_sweep(db, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "12")
    calls = fake_pages(monkeypatch, [issue(1, ["agent-ready"])])
    assert intake_loop.intake_tick(wide_policy(), generation=0)
    first = len(calls)
    # The admission re-opens the sweep once. That sweep finds only the issue it
    # already received, admits nothing, and the hourly clock governs again.
    intake_loop.intake_tick(wide_policy(), generation=0)
    second = len(calls)
    assert second > first
    intake_loop.intake_tick(wide_policy(), generation=0)
    assert len(calls) == second
