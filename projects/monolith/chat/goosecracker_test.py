"""Tests for chat.goosecracker: the owner gate, transcript accumulation, and
session dispatch (ADR 024 Task 4). DB-backed tests run against in-memory SQLite
with the chat schema stripped. goosecracker.api is injected as a fake module so
the test does not pull the real executor (and so no run is dispatched)."""

import sys
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat import goosecracker
from chat.models import GoosecrackerSession


# ---------------------------------------------------------------------------
# Owner gate (pure, env-driven)
# ---------------------------------------------------------------------------


class TestOwnerGate:
    def test_matches_configured_owner(self, monkeypatch):
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "12345")
        assert goosecracker.is_owner(12345) is True
        assert goosecracker.is_owner("12345") is True

    def test_rejects_non_owner(self, monkeypatch):
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "12345")
        assert goosecracker.is_owner(999) is False

    def test_fails_closed_when_unset(self, monkeypatch):
        monkeypatch.delenv("OWNER_DISCORD_USER_ID", raising=False)
        assert goosecracker.is_owner(12345) is False


# ---------------------------------------------------------------------------
# Transcript joining
# ---------------------------------------------------------------------------


class TestJoinTranscript:
    def test_first_turn_is_the_message(self):
        assert goosecracker._join_transcript("", "build a clock") == "build a clock"

    def test_appends_with_blank_line(self):
        joined = goosecracker._join_transcript("build a clock", "make it red")
        assert joined == "build a clock\n\nmake it red"


# ---------------------------------------------------------------------------
# Session dispatch (DB-backed)
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture(name="fake_api")
def fake_api_fixture():
    """Inject a fake goosecracker.api so goosecracker's lazy `from goosecracker.api
    import submit` binds to a mock, avoiding the real executor import chain."""
    fake = MagicMock()
    fake.submit.return_value = {
        "session": "thread-1",
        "thread_id": "t",
        "action": "create",
    }
    with patch.dict(sys.modules, {"goosecracker.api": fake}):
        yield fake


def test_start_session_records_transcript_and_dispatches(engine, fake_api):
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_session("thread-1", "  build a clock  ")

    fake_api.submit.assert_called_once_with(
        "build a clock",
        session="thread-1",
        recipe="artifact",
        tier="artifact",
        discord_thread="thread-1",
    )
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "thread-1")
        assert row is not None
        assert row.transcript == "build a clock"


def test_continue_session_appends_and_resubmits_full_transcript(engine, fake_api):
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_session("thread-1", "build a clock")
        fake_api.submit.reset_mock()
        goosecracker.continue_session("thread-1", "make it red")

    fake_api.submit.assert_called_once_with(
        "build a clock\n\nmake it red",
        session="thread-1",
        recipe="artifact",
        tier="artifact",
        discord_thread="thread-1",
    )
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "thread-1")
        assert row.transcript == "build a clock\n\nmake it red"


def test_continue_session_unknown_thread_returns_none(engine, fake_api):
    with patch("chat.goosecracker.get_engine", return_value=engine):
        result = goosecracker.continue_session("nope", "hi")
    assert result is None
    fake_api.submit.assert_not_called()


def test_is_goosecracker_thread(engine, fake_api):
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_session("thread-1", "build a clock")
        assert goosecracker.is_goosecracker_thread("thread-1") is True
        assert goosecracker.is_goosecracker_thread("other") is False


# ---------------------------------------------------------------------------
# ADR 026 Phase 2: continue_session resume gate
# ---------------------------------------------------------------------------


def test_continue_session_resume_path_when_session_exists(
    engine, fake_api, monkeypatch
):
    """When head_session returns a truthy etag (Model A), the task is the latest
    stripped message (not the full transcript)."""
    from artifact import s3

    monkeypatch.setattr(s3, "head_session", lambda _: "abc123")

    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_session("thread-1", "build a clock")
        fake_api.submit.reset_mock()
        goosecracker.continue_session("thread-1", "  make it red  ")

    fake_api.submit.assert_called_once_with(
        "make it red",
        session="thread-1",
        recipe="artifact",
        tier="artifact",
        discord_thread="thread-1",
    )


def test_continue_session_cold_path_when_no_session(engine, fake_api, monkeypatch):
    """When head_session returns None (Model B), the task is the full accumulated
    transcript."""
    from artifact import s3

    monkeypatch.setattr(s3, "head_session", lambda _: None)

    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_session("thread-1", "build a clock")
        fake_api.submit.reset_mock()
        goosecracker.continue_session("thread-1", "make it red")

    fake_api.submit.assert_called_once_with(
        "build a clock\n\nmake it red",
        session="thread-1",
        recipe="artifact",
        tier="artifact",
        discord_thread="thread-1",
    )


def test_start_agent_session_dispatches_with_agent_recipe(engine, fake_api):
    """start_agent_session submits with recipe='agent', tier='', and passes repo."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_agent_session("thread-2", "loom", "  add a login page  ")

    fake_api.submit.assert_called_once_with(
        "add a login page",
        session="thread-2",
        recipe="agent",
        tier="",
        repo="loom",
        discord_thread="thread-2",
    )


def test_start_agent_session_writes_session_row(engine, fake_api):
    """start_agent_session writes a GoosecrackerSession row with recipe='agent'
    and running=True so that follow-up replies route through the agent path."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_agent_session("thread-3", "homelab", "  fix the bug  ")

    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "thread-3")
    assert row is not None
    assert row.recipe == "agent"
    assert row.tier == ""
    assert row.repo == "homelab"
    assert row.transcript == "fix the bug"
    assert row.running
    assert row.pending == ""


# ---------------------------------------------------------------------------
# Agent conversational path: continue_session queuing + dispatching
# ---------------------------------------------------------------------------


def _make_agent_session(
    engine,
    thread_id: str,
    repo: str = "homelab",
    running: bool = False,
    pending: str = "",
) -> None:
    """Insert a GoosecrackerSession row for an agent thread directly."""
    with Session(engine) as session:
        session.add(
            GoosecrackerSession(
                discord_thread=thread_id,
                recipe="agent",
                tier="",
                repo=repo,
                transcript="initial task",
                running=running,
                pending=pending,
            )
        )
        session.commit()


def test_continue_agent_session_dispatches_when_idle(engine, fake_api):
    """When recipe=agent and running=False, continue_session dispatches and returns
    action='dispatched'."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        _make_agent_session(engine, "agent-t1", running=False)
        result = goosecracker.continue_session("agent-t1", "now do this")

    assert result is not None
    assert result.get("action") == "dispatched"
    fake_api.submit.assert_called_once()
    call = fake_api.submit.call_args
    assert call.args[0] == "now do this"  # task = message (no pending backlog)
    assert call.kwargs["recipe"] == "agent"
    assert call.kwargs["repo"] == "homelab"
    assert call.kwargs["discord_thread"] == "agent-t1"

    # running should be True (set before dispatch)
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "agent-t1")
    assert row.running
    assert row.pending == ""


def test_continue_agent_session_queues_when_running(engine, fake_api):
    """When recipe=agent and running=True, continue_session appends to pending and
    returns action='queued' without dispatching."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        _make_agent_session(engine, "agent-t2", running=True)
        result = goosecracker.continue_session("agent-t2", "extra instruction")

    assert result == {"action": "queued"}
    fake_api.submit.assert_not_called()

    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "agent-t2")
    assert row.running
    assert row.pending == "extra instruction"


def test_continue_agent_session_queues_multiple_appends_to_pending(engine, fake_api):
    """Multiple queued replies are newline-joined in pending."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        _make_agent_session(engine, "agent-t3", running=True, pending="first reply")
        result = goosecracker.continue_session("agent-t3", "second reply")

    assert result == {"action": "queued"}
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "agent-t3")
    assert row.pending == "first reply\nsecond reply"


def test_continue_agent_session_consumes_pending_on_dispatch(engine, fake_api):
    """When idle with a non-empty pending backlog, the full backlog + new message
    becomes the task, and pending is cleared after dispatch."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        _make_agent_session(engine, "agent-t4", running=False, pending="queued msg")
        result = goosecracker.continue_session("agent-t4", "new msg")

    assert result is not None
    assert result.get("action") == "dispatched"
    call = fake_api.submit.call_args
    assert call.args[0] == "queued msg\nnew msg"  # backlog + new joined by \n

    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "agent-t4")
    assert row.pending == ""
    assert row.running


def test_continue_session_falls_back_to_cold_on_head_session_error(
    engine, fake_api, monkeypatch
):
    """When head_session raises any exception, the handler logs and falls back
    to the cold (Model B) rebuild path using the full transcript."""
    from artifact import s3

    def _raise(_):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(s3, "head_session", _raise)

    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_session("thread-1", "build a clock")
        fake_api.submit.reset_mock()
        goosecracker.continue_session("thread-1", "make it red")

    fake_api.submit.assert_called_once_with(
        "build a clock\n\nmake it red",
        session="thread-1",
        recipe="artifact",
        tier="artifact",
        discord_thread="thread-1",
    )


def test_drain_agent_queue_returns_task_and_clears_pending(engine):
    """When pending is non-empty, drain returns the queued task, clears pending,
    and keeps running=True so the runner dispatches the next turn."""
    _make_agent_session(engine, "d-t1", running=True, pending="do something extra")
    with patch("chat.goosecracker.get_engine", return_value=engine):
        task = goosecracker.drain_agent_queue("d-t1")

    assert task == "do something extra"
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "d-t1")
    assert row.pending == ""
    assert row.running  # runner handles dispatching the next turn


def test_drain_agent_queue_clears_running_when_empty(engine):
    """When pending is empty, drain returns None and sets running=False so the
    thread accepts new replies again."""
    _make_agent_session(engine, "d-t2", running=True, pending="")
    with patch("chat.goosecracker.get_engine", return_value=engine):
        task = goosecracker.drain_agent_queue("d-t2")

    assert task is None
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "d-t2")
    assert not row.running


def test_drain_agent_queue_returns_none_for_unknown_thread(engine):
    """No session row for the thread: drain returns None gracefully."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        assert goosecracker.drain_agent_queue("no-such-thread") is None


def test_artifact_id_is_random_stable_and_not_the_thread_id(engine):
    """Capability URL (ADR 024 amendment): the artifact id is a random token
    stored on the thread's session row, reused across publishes, and never the
    enumerable Discord thread id."""
    thread = "1512814732392927463"
    with Session(engine) as s:
        s.add(GoosecrackerSession(discord_thread=thread, recipe="artifact"))
        s.commit()
    with patch("chat.goosecracker.get_engine", return_value=engine):
        first = goosecracker.artifact_id_for_thread(thread)
        second = goosecracker.artifact_id_for_thread(thread)
    assert first == second  # stable across re-publish (hot-reload)
    assert first != thread  # not the enumerable thread id
    assert len(first) >= 10  # unguessable capability token
