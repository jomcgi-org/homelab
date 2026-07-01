"""BDD tests for the goosecracker run/result ledger (goose cutover PR C)."""

from __future__ import annotations

from sqlalchemy import text

from goosecracker import threads


def test_upsert_run_creates_running_row(ledger_db):
    result = threads.upsert_run(
        "sess-1",
        recipe="artifact",
        tier="",
        task="build a clock",
        discord_thread="dt-1",
    )
    assert result["action"] == "create"
    tid = result["thread_id"]

    row = threads.get_run(tid)
    assert row is not None
    assert row["state"] == "RUNNING"
    assert row["session_id"] == "sess-1"
    assert row["recipe"] == "artifact"
    assert row["task"] == "build a clock"
    assert row["discord_thread"] == "dt-1"
    assert row["result"] is None
    assert row["completed_at"] is None


def test_second_submit_same_session_resumes_one_row(ledger_db):
    first = threads.upsert_run(
        "sess-2", recipe="agent", tier="", task="one", discord_thread=""
    )
    threads.mark_completed("sess-2", "done once")

    second = threads.upsert_run(
        "sess-2", recipe="agent", tier="", task="two", discord_thread=""
    )
    assert second["action"] == "resume"
    assert second["thread_id"] == first["thread_id"]

    # One row per session, reset back to RUNNING with the prior result cleared.
    count = ledger_db.execute(
        text(
            "SELECT count(*) AS c FROM claude_agent.agent_threads "
            "WHERE session_id = 'sess-2'"
        )
    ).fetchone()
    assert count.c == 1
    row = threads.get_run(first["thread_id"])
    assert row["state"] == "RUNNING"
    assert row["task"] == "two"
    assert row["result"] is None
    assert row["completed_at"] is None


def test_mark_completed_stamps_result(ledger_db):
    created = threads.upsert_run(
        "sess-3", recipe="agent", tier="", task="t", discord_thread=""
    )
    threads.mark_completed("sess-3", "the output")

    row = threads.get_run(created["thread_id"])
    assert row["state"] == "COMPLETED"
    assert row["result"] == "the output"
    assert row["result_error"] is None
    assert row["completed_at"] is not None


def test_mark_failed_stamps_error(ledger_db):
    created = threads.upsert_run(
        "sess-4", recipe="agent", tier="", task="t", discord_thread=""
    )
    threads.mark_failed("sess-4", "boom")

    row = threads.get_run(created["thread_id"])
    assert row["state"] == "FAILED"
    assert row["result_error"] == "boom"
    assert row["completed_at"] is not None


def test_list_runs_filters_by_state(ledger_db):
    threads.upsert_run("sess-5", recipe="agent", tier="", task="a", discord_thread="")
    done = threads.upsert_run(
        "sess-6", recipe="agent", tier="", task="b", discord_thread=""
    )
    threads.mark_completed("sess-6", "ok")

    running = threads.list_runs(state="RUNNING")
    assert {r["session_id"] for r in running} == {"sess-5"}

    completed = threads.list_runs(state="COMPLETED")
    assert [r["thread_id"] for r in completed] == [done["thread_id"]]


def test_get_run_missing_returns_none(ledger_db):
    assert threads.get_run("ghost") is None


def test_serialize_renders_iso_timestamps(ledger_db):
    created = threads.upsert_run(
        "sess-7", recipe="agent", tier="", task="t", discord_thread=""
    )
    row = threads.get_run(created["thread_id"])
    out = threads.serialize(row)
    assert isinstance(out["created_at"], str)
    assert out["completed_at"] is None
