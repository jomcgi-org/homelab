"""BDD-style tests for the agent_threads catalog (ADR 022).

Exercises agent.threads against the real test Postgres (the catalog reads/writes
claude_agent.agent_threads, created by the chart migrations the pg fixture
applies).
"""

from __future__ import annotations

from sqlalchemy import text

from agent import threads


def _insert(session, thread_id, state, node="node-4", **extra):
    session.execute(
        text(
            """
            INSERT INTO claude_agent.agent_threads
                (thread_id, state, repo, branch, node, arch, ttl_secs)
            VALUES (:id, :state, :repo, :branch, :node, :arch, :ttl)
            """
        ),
        {
            "id": thread_id,
            "state": state,
            "repo": extra.get("repo", "homelab"),
            "branch": extra.get("branch", "main"),
            "node": node,
            "arch": extra.get("arch", "amd64"),
            "ttl": extra.get("ttl_secs", 86400),
        },
    )
    session.commit()


def test_list_threads_filters_by_state_and_node(agent_db):
    _insert(agent_db, "t-run", "RUNNING")
    _insert(agent_db, "t-idle", "IDLE")
    _insert(agent_db, "t-other-node", "RUNNING", node="node-9")

    all_node4 = threads.list_threads(node="node-4")
    ids = {r["thread_id"] for r in all_node4}
    assert ids == {"t-run", "t-idle"}

    idle = threads.list_threads(state="IDLE")
    assert [r["thread_id"] for r in idle] == ["t-idle"]


def test_get_thread(agent_db):
    _insert(agent_db, "t1", "RUNNING")
    got = threads.get_thread("t1")
    assert got is not None
    assert got["thread_id"] == "t1"
    assert got["repo"] == "homelab"
    assert threads.get_thread("nope") is None


def test_resume_idle_thread_sets_wake_request(agent_db):
    _insert(agent_db, "t-idle", "IDLE")
    result = threads.request_resume("t-idle")
    assert result["ok"] is True

    row = threads.get_thread("t-idle")
    assert row["wake_requested_at"] is not None


def test_resume_non_idle_thread_rejected(agent_db):
    _insert(agent_db, "t-run", "RUNNING")
    result = threads.request_resume("t-run")
    assert result["ok"] is False
    assert "RUNNING" in result["reason"]

    # And no wake stamped.
    row = threads.get_thread("t-run")
    assert row["wake_requested_at"] is None


def test_resume_missing_thread(agent_db):
    result = threads.request_resume("ghost")
    assert result["ok"] is False
    assert result["reason"] == "thread not found"


def test_serialize_renders_datetimes(agent_db):
    _insert(agent_db, "t1", "IDLE")
    threads.request_resume("t1")
    row = threads.get_thread("t1")
    out = threads.serialize(row)
    assert isinstance(out["created_at"], str)
    assert isinstance(out["last_active_at"], str)
    assert isinstance(out["wake_requested_at"], str)
