"""BDD tests for the agent backstop + warm-base registry (ADR 022, Phase 4)."""

from __future__ import annotations

from sqlalchemy import text

from agent import backstop, base_snapshots


def _insert_thread(session, thread_id, state, last_active_sql):
    session.execute(
        text(
            f"""
            INSERT INTO claude_agent.agent_threads
                (thread_id, state, repo, branch, node, arch, last_active_at)
            VALUES (:id, :state, 'homelab', 'main', 'node-4', 'amd64', {last_active_sql})
            """
        ),
        {"id": thread_id, "state": state},
    )
    session.commit()


def test_find_stuck_threads_only_flags_long_running(agent_db):
    # RUNNING but active recently -> not stuck.
    _insert_thread(agent_db, "t-fresh", "RUNNING", "now()")
    # RUNNING with no activity for 2 hours -> stuck.
    _insert_thread(agent_db, "t-stuck", "RUNNING", "now() - interval '2 hours'")
    # IDLE long ago -> not a backstop concern (GC handles idle).
    _insert_thread(agent_db, "t-idle", "IDLE", "now() - interval '2 hours'")

    stuck = backstop.find_stuck_threads(agent_db, threshold_secs=3600)
    ids = {s["thread_id"] for s in stuck}
    assert ids == {"t-stuck"}


def test_sweep_summary_shape(agent_db):
    _insert_thread(agent_db, "t-stuck", "RUNNING", "now() - interval '3 hours'")
    summary = backstop.sweep(threshold_secs=3600)
    assert summary["stuck_count"] == 1
    assert summary["stuck_threads"][0]["thread_id"] == "t-stuck"
    assert isinstance(summary["stuck_threads"][0]["last_active_at"], str)


def test_request_rebuild_upserts_and_flags_pending(agent_db):
    first = base_snapshots.request_rebuild("homelab", "amd64", "sha-aaa")
    assert first["base_ref"] == "base-homelab-amd64"
    assert first["rebuild_pending"] is True  # never built

    # A second request at a new sha updates the same row (unique repo+arch).
    second = base_snapshots.request_rebuild("homelab", "amd64", "sha-bbb")
    assert second["base_ref"] == "base-homelab-amd64"
    assert second["requested_sha"] == "sha-bbb"

    bases = base_snapshots.list_bases()
    assert len(bases) == 1
    assert bases[0]["requested_sha"] == "sha-bbb"


def test_request_rebuild_distinct_per_arch(agent_db):
    base_snapshots.request_rebuild("homelab", "amd64", "sha-1")
    base_snapshots.request_rebuild("homelab", "arm64", "sha-1")
    bases = base_snapshots.list_bases()
    refs = {b["base_ref"] for b in bases}
    assert refs == {"base-homelab-amd64", "base-homelab-arm64"}


def test_base_serialize_renders_datetimes(agent_db):
    base_snapshots.request_rebuild("homelab", "amd64", "sha-1")
    row = base_snapshots.list_bases()[0]
    out = base_snapshots.serialize(row)
    assert isinstance(out["created_at"], str)
    assert out["built_at"] is None
