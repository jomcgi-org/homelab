"""Hermetic tests for the gap-drain shadow workflows.

No Temporal test server, no DB, no network: psycopg's ``connect`` is patched to a
fake returning canned ``knowledge.gaps`` rows, the research harness subprocess is
mocked, and the NATS client is a fake. Coroutines are driven with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import temporalio.workflow

import projects.lakehouse.orchestrator.workflows.gap_drain as gd
from projects.lakehouse.orchestrator.workflows.gap_drain import (
    ACTIVITIES,
    DEFAULT_SWEEP_LIMIT,
    WORKFLOWS,
    GapContext,
    GapDrainResult,
    GapDrainSweepWorkflow,
    GapDrainWorkflow,
    find_ready_gaps,
    run_research_session,
    workflow_id_for,
)


# --- fakes ----------------------------------------------------------------


class FakeCursor:
    def __init__(self, gap_rows):
        self._gap_rows = gap_rows
        self._pending: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        assert "FROM knowledge.gaps" in sql
        # SCOPE GUARD: the sweep is read-only — it must never UPDATE/mutate.
        upper = sql.upper()
        assert "UPDATE" not in upper
        assert "INSERT" not in upper
        assert "DELETE" not in upper
        self._pending = list(self._gap_rows)

    def fetchall(self):
        return self._pending


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor


@contextmanager
def patched_psycopg(gap_rows):
    import psycopg

    fake = FakeConn(FakeCursor(gap_rows))
    with (
        patch.object(psycopg, "connect", return_value=fake),
        patch.dict(
            gd.os.environ,
            {"DATABASE_URL": "postgresql://app:app@localhost:5432/monolith"},
        ),
    ):
        yield


class FakeNatsClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def publish(self, subject, payload, *, msg_id=None, headers=None) -> None:
        self.calls.append({"subject": subject, "payload": payload, "msg_id": msg_id})

    async def close(self) -> None:
        self.closed = True


# --- workflow_id_for ------------------------------------------------------


def test_workflow_id_for_is_deterministic():
    assert workflow_id_for("42") == "gap-drain-42"
    assert workflow_id_for("42") == workflow_id_for("42")


# --- find_ready_gaps (read-only) -----------------------------------------


def test_find_ready_gaps_builds_dicts():
    gap_rows = [
        (42, "wong-zakai", "context for 42", "external"),
        (43, "another-term", "", "external"),
    ]
    with patched_psycopg(gap_rows):
        gaps = asyncio.run(find_ready_gaps(10))

    assert [g["gap_id"] for g in gaps] == ["42", "43"]
    assert gaps[0]["term"] == "wong-zakai"
    assert gaps[0]["context"] == "context for 42"
    assert gaps[0]["gap_class"] == "external"
    # gap_id is stringified for use as the workflow-id suffix / event entity_id.
    assert isinstance(gaps[0]["gap_id"], str)


def test_find_ready_gaps_empty():
    with patched_psycopg([]):
        assert asyncio.run(find_ready_gaps()) == []


def test_find_ready_gaps_requires_database_url():
    import psycopg

    with (
        patch.object(psycopg, "connect"),
        patch.dict(gd.os.environ, {}, clear=True),
        pytest.raises(RuntimeError, match="DATABASE_URL"),
    ):
        asyncio.run(find_ready_gaps())


# --- run_research_session -------------------------------------------------


def _ctx(gap_id="42"):
    return GapContext(
        gap_id=gap_id, term="wong-zakai", context="ctx", gap_class="external"
    )


def test_run_research_session_skips_when_no_harness_configured():
    # No DATABASE_URL needed: run_research_session never touches Postgres.
    fake = FakeNatsClient()
    with (
        patch.dict(gd.os.environ, {}, clear=True),
        patch.object(gd, "NatsClient", return_value=fake),
    ):
        result = asyncio.run(run_research_session(_ctx()))

    assert isinstance(result, GapDrainResult)
    assert result.status == "skipped"
    # A gap event is still published recording the (skipped) outcome.
    assert fake.connected and fake.closed
    assert fake.calls[0]["subject"] == "events.knowledge.gap"
    assert fake.calls[0]["msg_id"] == "42-v1"
    decoded = json.loads(fake.calls[0]["payload"].decode("utf-8"))
    assert decoded["entity_type"] == "gap"
    assert decoded["event_type"] == "processed"
    assert decoded["payload"]["status"] == "skipped"


def test_run_research_session_invokes_harness_subprocess():
    fake = FakeNatsClient()

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"research output", b"")

    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    with (
        patch.dict(gd.os.environ, {gd.HARNESS_COMMAND_ENV: "/usr/bin/gap-harness"}),
        patch.object(gd, "NatsClient", return_value=fake),
        patch("asyncio.create_subprocess_exec", fake_exec),
        patch.object(gd.activity, "heartbeat"),
    ):
        result = asyncio.run(run_research_session(_ctx("99")))

    assert result.status == "researched"
    assert result.detail == "research output"
    # Harness invoked with the command + gap context JSON on argv (no web wiring).
    assert captured["cmd"][0] == "/usr/bin/gap-harness"
    gap_json = json.loads(captured["cmd"][1])
    assert gap_json["gap_id"] == "99"
    assert gap_json["term"] == "wong-zakai"
    assert fake.calls[0]["msg_id"] == "99-v1"


def test_run_research_session_failed_harness():
    fake = FakeNatsClient()

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return (b"", b"harness error")

    async def fake_exec(*cmd, **kwargs):
        return FakeProc()

    with (
        patch.dict(gd.os.environ, {gd.HARNESS_COMMAND_ENV: "/usr/bin/gap-harness"}),
        patch.object(gd, "NatsClient", return_value=fake),
        patch("asyncio.create_subprocess_exec", fake_exec),
        patch.object(gd.activity, "heartbeat"),
    ):
        result = asyncio.run(run_research_session(_ctx()))

    assert result.status == "failed"
    assert "harness error" in result.detail
    # Outcome event still published on failure.
    assert fake.calls[0]["subject"] == "events.knowledge.gap"


# --- workflow registration ------------------------------------------------


def test_workflows_are_defn_and_exported():
    for wf_cls in (GapDrainWorkflow, GapDrainSweepWorkflow):
        assert temporalio.workflow._Definition.from_class(wf_cls) is not None
    assert WORKFLOWS == [GapDrainWorkflow, GapDrainSweepWorkflow]


def test_activities_exported_and_decorated():
    import temporalio.activity

    assert find_ready_gaps in ACTIVITIES
    assert run_research_session in ACTIVITIES
    assert temporalio.activity._Definition.from_callable(find_ready_gaps)
    assert temporalio.activity._Definition.from_callable(run_research_session)


def test_default_sweep_limit_matches_research_batch_ceiling():
    # Shadow definition mirrors the live research pipeline's batch ceiling.
    assert DEFAULT_SWEEP_LIMIT == 10
