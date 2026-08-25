"""BDD coverage for the qwen drainer's database-backed seams."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session

import swarm.drainer as drainer
from agent import routine_jobs
from agent_sessions import mcp, store
from agent_sessions.transport import EmberSessionGone


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
