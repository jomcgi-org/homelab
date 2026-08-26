"""BDD coverage for the qwen drainer's database-backed seams."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session

import swarm.drainer as drainer
import swarm.health as health
from agent import routine_jobs
from agent.config import DrainerSettings
from agent_sessions import store
from agent_sessions.transport import EmberSessionGone
from swarm.module import MODULE


class FakeDBOS:
    workflow_id = "drainer-bdd-workflow"


def _create_session(
    session: Session, local_session_id: str, ember_session_id: str | None = None
) -> int:
    row = store.create_session(
        session,
        local_session_id,
        "/workspace",
        "main",
        model="qwen",
        repo="jomcgi-org/homelab",
    )
    assert row.id is not None
    if ember_session_id is not None:
        store.set_ember_session(
            session,
            row.id,
            ember_session_id,
            "ember-token",
            None,
        )
    return row.id


def _drainer_settings(**overrides) -> DrainerSettings:
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


def _register_health_job(
    session: Session,
    *,
    name: str,
    age_seconds: int,
    kind: str = "qwen-drain",
    locked_by: str | None = None,
    locked_at: datetime | None = None,
    ttl_secs: int = 300,
) -> None:
    assert routine_jobs.register_job(
        name=name,
        kind=kind,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )
    if locked_by is None:
        return
    session.execute(
        text(
            """
            UPDATE claude_agent.routine_jobs
               SET locked_by = :locked_by,
                   locked_at = :locked_at,
                   ttl_secs = :ttl_secs
             WHERE name = :name
            """
        ),
        {
            "name": name,
            "locked_by": locked_by,
            "locked_at": locked_at,
            "ttl_secs": ttl_secs,
        },
    )
    session.commit()


def _health(session: Session) -> dict:
    return health._drainer_health_core(session, "qwen-drain", 2700)


def test_module_registers_private_drainer_advisory():
    assert MODULE.register_health_advisory == {"drainer": health.drainer_health}
    assert MODULE.register_public is None


@pytest.mark.asyncio
async def test_disabled_drainer_is_ok_without_database_read(monkeypatch):
    monkeypatch.setattr(
        health,
        "load_drainer_settings",
        lambda: _drainer_settings(enabled=False),
    )

    def fail_read(*_args):
        raise AssertionError("database must not be read")

    monkeypatch.setattr(health, "_read_drainer_health", fail_read)

    result = await health.drainer_health()

    assert result["ok"] is True
    assert result["stalled"] is False
    assert result["status"] == "disabled"
    assert result["lag_seconds"] == 0


def test_drainer_health_has_no_lag_without_due_claimable_rows(agent_db: Session):
    _register_health_job(agent_db, name="future", age_seconds=-60)

    result = _health(agent_db)

    assert result["ok"] is True
    assert result["stalled"] is False
    assert result["lag_seconds"] == 0
    assert routine_jobs.claim_job("health-check", 300, kind="qwen-drain") is None


def test_drainer_health_ignores_other_routine_kinds(agent_db: Session):
    _register_health_job(
        agent_db,
        name="old-other-kind",
        age_seconds=3600,
        kind="other",
    )

    result = _health(agent_db)

    assert result["ok"] is True
    assert result["lag_seconds"] == 0
    assert routine_jobs.claim_job("health-check", 300, kind="qwen-drain") is None


def test_drainer_health_excludes_lock_without_locked_at(agent_db: Session):
    _register_health_job(
        agent_db,
        name="incomplete-lock",
        age_seconds=3600,
        locked_by="worker",
        locked_at=None,
    )

    result = _health(agent_db)

    assert result["ok"] is True
    assert result["lag_seconds"] == 0
    assert routine_jobs.claim_job("health-check", 300, kind="qwen-drain") is None


def test_drainer_health_excludes_live_lock(agent_db: Session):
    _register_health_job(
        agent_db,
        name="live-lock",
        age_seconds=3600,
        locked_by="worker",
        locked_at=datetime.now(timezone.utc) - timedelta(seconds=60),
    )

    result = _health(agent_db)

    assert result["ok"] is True
    assert result["lag_seconds"] == 0
    assert routine_jobs.claim_job("health-check", 300, kind="qwen-drain") is None


def test_drainer_health_reports_old_unlocked_job_as_stalled(agent_db: Session):
    _register_health_job(agent_db, name="old-unlocked", age_seconds=3600)

    result = _health(agent_db)

    assert result["ok"] is False
    assert result["stalled"] is True
    assert result["lag_seconds"] > result["threshold_seconds"]
    assert result["reason"].startswith("oldest claimable drainer job is ")
    assert "exceeds threshold 2700 seconds" in result["reason"]
    claimed = routine_jobs.claim_job("health-check", 300, kind="qwen-drain")
    assert claimed is not None
    assert claimed["name"] == "old-unlocked"


def test_drainer_health_reports_old_expired_lock_as_stalled(agent_db: Session):
    _register_health_job(
        agent_db,
        name="expired-lock",
        age_seconds=3600,
        locked_by="dead-worker",
        locked_at=datetime.now(timezone.utc) - timedelta(seconds=600),
    )

    result = _health(agent_db)

    assert result["ok"] is False
    assert result["stalled"] is True
    assert "oldest claimable drainer job" in result["reason"]
    claimed = routine_jobs.claim_job("health-check", 300, kind="qwen-drain")
    assert claimed is not None
    assert claimed["name"] == "expired-lock"


def test_drainer_health_reports_recent_unlocked_job_as_ok(agent_db: Session):
    _register_health_job(agent_db, name="recent-unlocked", age_seconds=600)

    result = _health(agent_db)

    assert result["ok"] is True
    assert result["stalled"] is False
    assert 590 < result["lag_seconds"] < 610
    assert result["reason"].startswith("oldest claimable drainer job is ")
    claimed = routine_jobs.claim_job("health-check", 300, kind="qwen-drain")
    assert claimed is not None
    assert claimed["name"] == "recent-unlocked"


def test_destroy_drainer_session_with_no_row(agent_db: Session):
    assert (
        drainer.destroy_drainer_session.__wrapped__(None, "missing-drainer-session")
        is False
    )


def test_destroy_drainer_session_without_ember_binding(agent_db: Session):
    session_id = _create_session(agent_db, "drainer-without-ember")
    pending = store.create_pending_message(agent_db, session_id, "queued work")
    pending_seq = pending.seq

    assert (
        drainer.destroy_drainer_session.__wrapped__(None, "drainer-without-ember")
        is False
    )

    agent_db.expire_all()
    assert store.get_pending_message(agent_db, session_id, pending_seq) is None


@pytest.mark.parametrize("session_gone", [False, True])
def test_destroy_drainer_session_clears_ember_binding(
    monkeypatch, agent_db: Session, session_gone: bool
):
    from agent_sessions import mcp

    ember_session_id = f"ember-drainer-{session_gone}"
    local_session_id = f"drainer-with-ember-{session_gone}"
    session_id = _create_session(agent_db, local_session_id, ember_session_id)
    destroyed = []

    async def destroy_session(value: str) -> None:
        destroyed.append(value)
        if session_gone:
            raise EmberSessionGone("session already gone")

    monkeypatch.setattr(mcp._transport, "destroy_session", destroy_session)

    assert (
        drainer.destroy_drainer_session.__wrapped__(session_id, local_session_id)
        is True
    )
    assert destroyed == [ember_session_id]

    agent_db.expire_all()
    row = store.get_session(agent_db, session_id)
    assert row is not None
    assert row.ember_session_id is None


def test_errored_one_shot_finishes_once_and_stays_not_due(
    monkeypatch, agent_db: Session
):
    routine_jobs.register_job(
        name="drainer-one-shot-error",
        kind="qwen-drain",
        payload={"prompt": "fail this turn"},
        next_run_at=datetime.now(timezone.utc),
    )
    settings = {
        "enabled": True,
        "max_jobs_per_cycle": 3,
        "turn_timeout_seconds": 1800,
        "job_kind": "qwen-drain",
        "repo": "jomcgi-org/homelab",
        "branch": "main",
    }
    completions = []
    complete_job = drainer.finish_drainer_job.__wrapped__

    def finish(name: str, status: str, summary: str) -> bool:
        completions.append((name, status, summary))
        return complete_job(name, status, summary)

    def fail_turn(*_args):
        raise RuntimeError("turn failed")

    monkeypatch.setattr(drainer, "DBOS", FakeDBOS)
    monkeypatch.setattr(drainer, "pin_drainer_settings", lambda: settings)
    monkeypatch.setattr(
        drainer, "claim_drainer_job", drainer.claim_drainer_job.__wrapped__
    )
    monkeypatch.setattr(drainer, "finish_drainer_job", finish)
    monkeypatch.setattr(drainer, "start_agent_session", lambda *_args: 17)
    monkeypatch.setattr(drainer, "_await_turn", fail_turn)
    monkeypatch.setattr(drainer, "notify_drainer_failure", lambda *_args: None)
    monkeypatch.setattr(drainer, "destroy_drainer_session", lambda *_args: True)

    assert drainer.drain_cycle.__wrapped__() == {
        "status": "complete",
        "processed": 1,
    }
    assert completions == [("drainer-one-shot-error", "error", "turn failed")]
    assert not any(
        row["name"] == "drainer-one-shot-error"
        for row in routine_jobs.list_jobs(due_only=True)
    )
