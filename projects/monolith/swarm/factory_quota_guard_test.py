"""The Claude-window guard: pause, resume, hysteresis, and unknown readings."""

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
    "quota_guard": {"claude_7d_pause_percent": 85, "claude_7d_resume_percent": 75}
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


def test_an_open_window_leaves_delivery_running(db, monkeypatch):
    observe(monkeypatch, 40.0)
    verdict = guard.evaluate(POLICY)
    assert verdict == {
        "paused": False,
        "state": "open",
        "used_percent": 40.0,
        "pause_percent": 85,
        "resume_percent": 75,
    }
    assert actions(db, *controls.QUOTA_GUARD_ACTIONS) == []


def test_the_pause_threshold_pauses_once_and_stays_paused(db, monkeypatch):
    observe(monkeypatch, 85.0)
    assert guard.evaluate(POLICY)["paused"]
    assert guard.evaluate(POLICY)["paused"]
    assert actions(db, *controls.QUOTA_GUARD_ACTIONS) == ["quota_guard_paused"]
    assert guard.delivery_paused()


def test_between_the_thresholds_the_pause_holds(db, monkeypatch):
    observe(monkeypatch, 90.0)
    assert guard.evaluate(POLICY)["paused"]
    observe(monkeypatch, 80.0)
    verdict = guard.evaluate(POLICY)
    assert verdict["paused"] and verdict["used_percent"] == 80.0
    assert actions(db, *controls.QUOTA_GUARD_ACTIONS) == ["quota_guard_paused"]


def test_below_the_resume_threshold_the_lane_reopens_once(db, monkeypatch):
    observe(monkeypatch, 90.0)
    guard.evaluate(POLICY)
    observe(monkeypatch, 74.0)
    assert not guard.evaluate(POLICY)["paused"]
    assert not guard.evaluate(POLICY)["paused"]
    assert actions(db, *controls.QUOTA_GUARD_ACTIONS) == [
        "quota_guard_paused",
        "quota_guard_resumed",
    ]
    assert not guard.delivery_paused()


def test_an_unreadable_window_is_not_a_pause_and_audits_hourly(db, monkeypatch):
    monkeypatch.setattr(guard, "reading", lambda **_kwargs: None)
    for _ in range(3):
        verdict = guard.evaluate(POLICY)
        assert verdict["paused"] is False and verdict["state"] == "unknown"
    assert actions(db, "quota_guard_unknown") == ["quota_guard_unknown"]
    with Session(db) as session:
        row = session.exec(select(FactoryAudit)).one()
        row.created_at = controls._now().replace(tzinfo=None) - timedelta(hours=2)
        session.add(row)
        session.commit()
    guard.evaluate(POLICY)
    assert actions(db, "quota_guard_unknown") == [
        "quota_guard_unknown",
        "quota_guard_unknown",
    ]


def test_a_stale_observation_is_unknown_rather_than_a_pause(db, monkeypatch):
    observe(monkeypatch, 99.0, age=4000.0)
    verdict = guard.evaluate(POLICY)
    assert verdict["paused"] is False and verdict["state"] == "unknown"
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


def test_a_fractional_utilisation_is_read_as_a_percentage():
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
    assert observed["used_percent"] == pytest.approx(86.0)


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
