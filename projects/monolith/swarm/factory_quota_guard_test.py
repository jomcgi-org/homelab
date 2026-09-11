"""Reviewer routing: fallback, restore, hysteresis, waiting, unknown readings."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import swarm.factory_controls as controls
import swarm.factory_quota_guard as guard
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import SwarmTask

POLICY = {
    "quota_guard": {"claude_7d_pause_percent": 85, "claude_7d_resume_percent": 75},
    "allowed_models": ["opus", "astra", "luna"],
    "conductor_model": "opus",
    "reviewer_model": "opus",
    "worker_model": "luna",
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'guard.db'}",
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
    guard.reset_cache()
    yield engine
    guard.reset_cache()
    engine.dispose()


def observe(monkeypatch, used, *, age=30.0, window="7d", observed=True):
    payload = {
        "providers": {
            "claude": {
                "observed": observed,
                "age_seconds": age,
                "windows": [
                    {"name": "5h", "used_percent": 10.0},
                    {"name": window, "used_percent": used},
                ],
            }
        }
    }
    monkeypatch.setattr(guard, "reading", lambda **_kwargs: guard._window(payload))


def actions(db, *names):
    with Session(db) as session:
        return [
            row.action
            for row in session.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action.in_(names))
                .order_by(FactoryAudit.id)
            ).all()
        ]


def open_quota(monkeypatch, **overrides):
    """Every provider has room unless a test says otherwise."""
    import swarm.model_pool as model_pool

    quota = {"codex": {"headline_used_percent": 10.0, "age_seconds": 5.0}}
    quota.update(overrides)
    monkeypatch.setattr(model_pool, "quota_summary", lambda: quota)
    return quota


def test_a_quiet_window_reviews_on_opus(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 40.0)
    verdict = guard.observe(POLICY)
    assert verdict["action"] == "reviewer_restored"
    assert verdict["model"] == "opus" and verdict["window_high"] is False
    assert guard.reviewer_for(POLICY, "bug-fix")["model"] == "opus"
    # Opus reviewing a quiet window is the ordinary state, not an event.
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == []


def test_a_spent_window_reviews_on_the_next_pool_member(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 90.0)
    verdict = guard.observe(POLICY)
    assert verdict["action"] == "reviewer_fallback"
    assert verdict["model"] == "astra" and verdict["window_high"] is True
    assert verdict["used_percent"] == 90.0
    assert guard.reviewer_for(POLICY, "bug-fix")["model"] == "astra"
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == ["reviewer_fallback"]


def test_the_fallback_is_audited_once_not_every_tick(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 90.0)
    for _ in range(3):
        assert guard.observe(POLICY)["model"] == "astra"
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == ["reviewer_fallback"]


def test_between_the_thresholds_the_fallback_holds(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 90.0)
    assert guard.observe(POLICY)["model"] == "astra"
    observe(monkeypatch, 80.0)
    verdict = guard.observe(POLICY)
    assert verdict["model"] == "astra" and verdict["window_high"] is True
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == ["reviewer_fallback"]


def test_below_the_resume_threshold_opus_comes_back_once(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 90.0)
    guard.observe(POLICY)
    observe(monkeypatch, 74.0)
    assert guard.observe(POLICY)["model"] == "opus"
    assert guard.observe(POLICY)["model"] == "opus"
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == [
        "reviewer_fallback",
        "reviewer_restored",
    ]


def test_a_spent_window_with_no_fallback_left_waits(db, monkeypatch):
    open_quota(monkeypatch, codex={"exhausted": True, "age_seconds": 5.0})
    observe(monkeypatch, 90.0)
    verdict = guard.observe(POLICY)
    assert verdict["action"] == "review_waiting" and verdict["model"] is None
    assert guard.reviewer_for(POLICY, "bug-fix")["model"] is None
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == ["review_waiting"]


def test_judgment_work_waits_for_opus_rather_than_falling_back(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 90.0)
    guard.observe(POLICY)
    assert guard.reviewer_for(POLICY, "bug-fix")["model"] == "astra"
    judgment = guard.reviewer_for(POLICY, "judgment-analysis")
    assert judgment["model"] is None
    assert {entry["model"] for entry in judgment["skipped"]} == {"opus", "astra"}


def test_judgment_work_reviews_on_opus_while_the_window_is_quiet(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 40.0)
    guard.observe(POLICY)
    assert guard.reviewer_for(POLICY, "judgment-analysis")["model"] == "opus"


def test_an_unreadable_window_never_starts_a_fallback(db, monkeypatch):
    open_quota(monkeypatch)
    monkeypatch.setattr(guard, "reading", lambda **_kwargs: None)
    for _ in range(3):
        verdict = guard.observe(POLICY)
        assert verdict["window_high"] is False and verdict["model"] == "opus"
    assert actions(db, "quota_guard_unknown") == ["quota_guard_unknown"]
    # Nothing had fallen back, so nothing is restored.
    assert actions(db, "reviewer_restored") == []


def test_an_unreadable_window_does_not_end_a_fallback(db, monkeypatch):
    """Snapping back to Opus on a broker outage would spend the rest blind."""
    open_quota(monkeypatch)
    observe(monkeypatch, 95.0)
    assert guard.observe(POLICY)["model"] == "astra"
    monkeypatch.setattr(guard, "reading", lambda **_kwargs: None)
    verdict = guard.observe(POLICY)
    assert verdict["window_high"] is True and verdict["model"] == "astra"
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == [
        "reviewer_fallback",
        "quota_guard_unknown",
    ]


def test_the_unknown_audit_is_hourly(db, monkeypatch):
    open_quota(monkeypatch)
    monkeypatch.setattr(guard, "reading", lambda **_kwargs: None)
    guard.observe(POLICY)
    with Session(db) as session:
        row = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "quota_guard_unknown")
        ).one()
        row.created_at = controls._now().replace(tzinfo=None) - timedelta(hours=2)
        session.add(row)
        session.commit()
    guard.observe(POLICY)
    assert actions(db, "quota_guard_unknown") == [
        "quota_guard_unknown",
        "quota_guard_unknown",
    ]


def test_a_stale_observation_never_starts_a_fallback(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 99.0, age=4000.0)
    verdict = guard.observe(POLICY)
    assert verdict["window_high"] is False and verdict["model"] == "opus"
    assert actions(db, "quota_guard_unknown") == ["quota_guard_unknown"]


def test_a_missing_seven_day_window_reads_as_nothing(monkeypatch):
    assert (
        guard._window(
            {
                "providers": {
                    "claude": {
                        "observed": True,
                        "windows": [{"name": "5h", "used_percent": 99.0}],
                    }
                }
            }
        )
        is None
    )


def test_an_unobserved_provider_reads_as_nothing():
    assert guard._window({"providers": {"claude": {"observed": False}}}) is None
    assert guard._window({}) is None
    assert guard._window(None) is None


def test_a_sub_one_percent_reading_is_not_re_normalised():
    """The sidecar already converted the fraction; doing it again read 0.86
    percent of the weekly window as 86 percent and tripped the fallback on the
    first day of every window."""
    observed = guard._window(
        {
            "providers": {
                "claude": {
                    "observed": True,
                    "age_seconds": 5.0,
                    "windows": [{"name": "7d", "used_percent": 0.86}],
                }
            }
        }
    )
    assert observed["used_percent"] == pytest.approx(0.86)


def test_a_quiet_window_at_a_fraction_of_a_percent_keeps_opus(db, monkeypatch):
    open_quota(monkeypatch)
    observe(monkeypatch, 0.9)
    verdict = guard.observe(POLICY)
    assert verdict["model"] == "opus" and verdict["window_high"] is False
    assert actions(db, *controls.REVIEWER_ROUTING_ACTIONS) == []


def test_an_expired_window_is_ignored():
    assert (
        guard._window(
            {
                "providers": {
                    "claude": {
                        "observed": True,
                        "windows": [
                            {"name": "7d", "used_percent": 99.0, "expired": True}
                        ],
                    }
                }
            }
        )
        is None
    )
