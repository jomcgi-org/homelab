"""BDD tests for AgentWorkflow dispatch (ADR 022, Phase 5)."""

from __future__ import annotations

from sqlalchemy import text

from agent import dispatch


def test_submit_creates_pending_thread(agent_db):
    result = dispatch.submit("fix the flaky test", repo="homelab", discord_thread="dt-1")
    tid = result["thread_id"]
    assert result["action"] == "create"

    row = dispatch.status(tid)
    assert row is not None
    assert row["state"] == "PENDING"
    assert row["repo"] == "homelab"
    assert row["discord_thread"] == "dt-1"
    assert row["node"] == dispatch.DEFAULT_NODE


def test_submit_resolves_built_warm_base(agent_db):
    # A built base for homelab+amd64 exists -> new thread starts from it.
    agent_db.execute(
        text(
            """
            INSERT INTO claude_agent.agent_base_snapshots
                (base_ref, repo, arch, requested_sha, built_sha)
            VALUES ('base-homelab-amd64', 'homelab', 'amd64', 'sha-1', 'sha-1')
            """
        )
    )
    agent_db.commit()

    result = dispatch.submit("task", repo="homelab", arch="amd64")
    assert result["base_snapshot_ref"] == "base-homelab-amd64"
    row = dispatch.status(result["thread_id"])
    assert row["base_snapshot_ref"] == "base-homelab-amd64"


def test_submit_with_thread_id_resumes(agent_db):
    # Create then idle a thread.
    created = dispatch.submit("task", repo="homelab")
    tid = created["thread_id"]
    agent_db.execute(
        text("UPDATE claude_agent.agent_threads SET state = 'IDLE' WHERE thread_id = :id"),
        {"id": tid},
    )
    agent_db.commit()

    resumed = dispatch.submit("more work", thread_id=tid)
    assert resumed["action"] == "resume"
    assert resumed["ok"] is True

    row = dispatch.status(tid)
    assert row["wake_requested_at"] is not None


def test_wake_for_discord_thread(agent_db):
    created = dispatch.submit("task", repo="homelab", discord_thread="dt-42")
    tid = created["thread_id"]
    agent_db.execute(
        text("UPDATE claude_agent.agent_threads SET state = 'IDLE' WHERE thread_id = :id"),
        {"id": tid},
    )
    agent_db.commit()

    result = dispatch.wake_for_discord_thread("dt-42")
    assert result["ok"] is True
    assert result["thread_id"] == tid


def test_wake_for_unknown_discord_thread(agent_db):
    result = dispatch.wake_for_discord_thread("nope")
    assert result["ok"] is False


def test_status_missing_thread(agent_db):
    assert dispatch.status("ghost") is None
