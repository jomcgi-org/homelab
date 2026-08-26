from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

from agent.config import DrainerSettings
import swarm.health as health
from swarm.module import MODULE


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drainer_health.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY,
                    routine_kind TEXT NOT NULL,
                    next_run_at TIMESTAMP,
                    locked_by TEXT,
                    locked_at TIMESTAMP,
                    ttl_secs INTEGER
                )
                """
            )
        )
    with Session(engine) as session:
        yield session


def _settings(**overrides) -> DrainerSettings:
    values = {
        "enabled": True,
        "max_jobs_per_cycle": 3,
        "turn_timeout_seconds": 1800,
        "stall_threshold_seconds": 2700,
        "job_kind": "qwen-drain",
        "repo": "jomcgi-org/homelab",
        "branch": "main",
    }
    values.update(overrides)
    return DrainerSettings(**values)


def _insert_job(
    session: Session,
    *,
    age_seconds: int,
    locked_by: str | None = None,
    lock_age_seconds: int | None = None,
    ttl_secs: int = 300,
    kind: str = "qwen-drain",
) -> None:
    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            INSERT INTO routine_jobs
                (name, routine_kind, next_run_at, locked_by, locked_at, ttl_secs)
            VALUES
                (:name, :kind, :next_run_at, :locked_by, :locked_at, :ttl_secs)
            """
        ),
        {
            "name": f"job-{age_seconds}-{locked_by}",
            "kind": kind,
            "next_run_at": now - timedelta(seconds=age_seconds),
            "locked_by": locked_by,
            "locked_at": (
                now - timedelta(seconds=lock_age_seconds)
                if lock_age_seconds is not None
                else None
            ),
            "ttl_secs": ttl_secs,
        },
    )
    session.commit()


def test_module_registers_private_drainer_advisory():
    assert MODULE.register_health_advisory == {"drainer": health.drainer_health}
    assert MODULE.register_public is None


@pytest.mark.asyncio
async def test_disabled_is_ok_without_database_read(monkeypatch):
    monkeypatch.setattr(
        health, "load_drainer_settings", lambda: _settings(enabled=False)
    )
    monkeypatch.setattr(
        health,
        "_read_drainer_health",
        lambda *_: (_ for _ in ()).throw(AssertionError("database must not be read")),
    )

    result = await health.drainer_health()

    assert result["ok"] is True
    assert result["stalled"] is False
    assert result["status"] == "disabled"
    assert result["lag_seconds"] == 0


def test_no_due_claimable_rows_is_ok(session):
    _insert_job(session, age_seconds=-60)

    result = health._drainer_health_core(session, "qwen-drain", 2700)

    assert result["ok"] is True
    assert result["stalled"] is False
    assert result["lag_seconds"] == 0


def test_live_lock_is_not_claimable(session):
    _insert_job(session, age_seconds=3600, locked_by="worker", lock_age_seconds=60)

    result = health._drainer_health_core(session, "qwen-drain", 2700)

    assert result["ok"] is True
    assert result["lag_seconds"] == 0


def test_old_unlocked_job_is_stalled(session):
    _insert_job(session, age_seconds=3600)

    result = health._drainer_health_core(session, "qwen-drain", 2700)

    assert result["ok"] is False
    assert result["stalled"] is True
    assert result["lag_seconds"] > result["threshold_seconds"]


def test_old_expired_lock_is_stalled(session):
    _insert_job(
        session,
        age_seconds=3600,
        locked_by="dead-worker",
        lock_age_seconds=600,
    )

    result = health._drainer_health_core(session, "qwen-drain", 2700)

    assert result["ok"] is False
    assert result["stalled"] is True
    assert "overdue" in result["reason"]


def test_recent_unlocked_job_is_ok_with_lag(session):
    _insert_job(session, age_seconds=600)

    result = health._drainer_health_core(session, "qwen-drain", 2700)

    assert result["ok"] is True
    assert result["stalled"] is False
    assert 590 < result["lag_seconds"] < 610
