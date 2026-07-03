"""Tests for chat.goosecracker: the owner gate, transcript accumulation, and
session dispatch (ADR 024 Task 4). DB-backed tests run against in-memory SQLite
with the chat schema stripped. goosecracker.api is injected as a fake module so
the test does not pull the real executor (and so no run is dispatched)."""

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import goosecracker
from chat.models import (
    DiscordOutbox,
    GoosecrackerSession,
    GoosecrackerSteering,
    Message,
)

# Sentinel so _make_agent_session can distinguish "caller passed None" from
# "caller passed nothing" for the running_since / runner_instance overrides.
_UNSET = object()


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


def test_set_then_take_progress_message_consumes_once(engine):
    """set records the live message id; take reads AND clears it, so a second
    take returns '' (consume-on-read: the id is settled at most once)."""
    with Session(engine) as session:
        session.add(GoosecrackerSession(discord_thread="t-99"))
        session.commit()
    with patch("chat.goosecracker.get_engine", return_value=engine):
        assert goosecracker.take_progress_message("t-99") == ""
        goosecracker.set_progress_message("t-99", "555")
        assert goosecracker.take_progress_message("t-99") == "555"
        # Consumed: a later turn in the same run reads empty and posts its own.
        assert goosecracker.take_progress_message("t-99") == ""


def test_progress_message_helpers_noop_without_row(engine):
    """No session row (an artifact run with no persisted session): set is a no-op
    and take returns '' so the runner falls back to posting the result as a new
    message rather than editing a message that does not exist."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.set_progress_message("missing", "555")  # no raise
        assert goosecracker.take_progress_message("missing") == ""


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
    # No parent channel supplied -> empty (runner then skips the concierge).
    assert row.parent_channel_id == ""


def test_start_agent_session_round_trips_parent_channel(engine, fake_api):
    """The parent channel id round-trips: start_agent_session stores it on the
    row and parent_channel_for_thread reads it back. This is the runner's only
    path to the channel-scoped context for a conversational reply, so a break
    here would silently drop every reply to the deterministic fallback."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.start_agent_session(
            "thread-pc", "homelab", "fix the bug", "parent-chan-9"
        )
        with Session(engine) as session:
            row = session.get(GoosecrackerSession, "thread-pc")
        assert row.parent_channel_id == "parent-chan-9"
        # Read back through the getter the runner actually calls.
        assert goosecracker.parent_channel_for_thread("thread-pc") == "parent-chan-9"
        # Unknown thread -> "" so the runner falls back to the deterministic summary.
        assert goosecracker.parent_channel_for_thread("nope") == ""
    # The first turn is stamped live (owned by this process, timestamped) so a
    # reply arriving during it queues rather than being reclaimed as "stale".
    assert row.runner_instance == goosecracker.INSTANCE_TOKEN
    assert row.running_since is not None
    assert row.inflight_task == "fix the bug"
    assert not goosecracker._is_stale(row, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Caller-provided context injection (ADR 040)
# ---------------------------------------------------------------------------


def _msg(session, channel_id, username, content, msg_id):
    session.add(
        Message(
            id=msg_id,
            discord_message_id=str(msg_id),
            channel_id=channel_id,
            user_id=username,
            username=username,
            content=content,
            embedding=[0.0] * 1024,
        )
    )
    session.commit()


class TestBuildInjectedContext:
    def test_bundles_parent_channel_transcript_and_readme(self, engine, fake_api):
        with Session(engine) as session:
            session.add(
                GoosecrackerSession(
                    discord_thread="thr-1",
                    recipe="agent",
                    tier="",
                    repo="homelab",
                    parent_channel_id="chan-1",
                )
            )
            session.commit()
            _msg(session, "chan-1", "alice", "hello", 1)
            _msg(session, "chan-1", "bob", "world", 2)

        with patch("chat.goosecracker.get_engine", return_value=engine):
            bundle = goosecracker.build_injected_context("thr-1", tier="")

        assert set(bundle) == {"README.md", "transcript.md"}
        assert "hello" in bundle["transcript.md"]
        assert "world" in bundle["transcript.md"]
        assert "chan-1" in bundle["README.md"]
        assert "injected-context" in bundle["README.md"].lower()

    def test_unknown_thread_returns_empty_bundle(self, engine, fake_api):
        with patch("chat.goosecracker.get_engine", return_value=engine):
            bundle = goosecracker.build_injected_context("unknown-thread", tier="")
        assert bundle == {}


# ---------------------------------------------------------------------------
# Agent conversational path: continue_session queuing + dispatching
# ---------------------------------------------------------------------------


def _make_agent_session(
    engine,
    thread_id: str,
    repo: str = "homelab",
    running: bool = False,
    pending: str = "",
    pending_message_ids: str = "",
    inflight_task: str = "",
    inflight_ack_ids: str = "",
    running_since: object = _UNSET,
    runner_instance: object = _UNSET,
) -> None:
    """Insert a GoosecrackerSession row for an agent thread directly.

    A running session defaults to a *live* turn (running_since=now, owned by this
    process) so the queue tests exercise the queue path rather than the stale
    reclaim path. Pass ``running_since=None`` / a foreign ``runner_instance`` to
    simulate an orphaned/stale turn.
    """
    now = datetime.now(timezone.utc)
    if running_since is _UNSET:
        running_since = now if running else None
    if runner_instance is _UNSET:
        runner_instance = goosecracker.INSTANCE_TOKEN if running else ""
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
                pending_message_ids=pending_message_ids,
                inflight_task=inflight_task,
                inflight_ack_ids=inflight_ack_ids,
                running_since=running_since,
                runner_instance=runner_instance,
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

    # running should be True (set before dispatch), and the dispatch path
    # appends the turn to the transcript (unlike the steering path).
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "agent-t1")
    assert row.running
    assert row.pending == ""
    assert row.transcript == "initial task\n\nnow do this"


def test_continue_agent_session_steers_when_running(engine, fake_api):
    """When recipe=agent and running=True, continue_session enqueues steering
    (ADR 035 Phase 2) and returns action='steering' without dispatching or
    touching pending. The transcript is untouched: fetch_steering owns that
    write (with attribution) when the running guest consumes the row, so
    continue_session must not double it."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        _make_agent_session(engine, "agent-t2", running=True)
        result = goosecracker.continue_session(
            "agent-t2", "extra instruction", "msg-99", author_id="U1"
        )

    assert result == {"action": "steering"}
    fake_api.submit.assert_not_called()

    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "agent-t2")
        steering = session.exec(select(GoosecrackerSteering)).all()
    assert row.running
    assert row.pending == ""
    assert row.transcript == "initial task"  # unchanged by this call
    assert len(steering) == 1
    assert steering[0].thread_id == "agent-t2"
    assert steering[0].message_id == "msg-99"
    assert steering[0].author_id == "U1"
    assert steering[0].text == "extra instruction"
    assert steering[0].delivered is False


def test_continue_agent_session_steering_falls_back_to_session_tier(engine, fake_api):
    """A blank tier argument falls back to the session's own tier."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        _make_agent_session(engine, "agent-t2b", running=True)
        goosecracker.continue_session("agent-t2b", "steer this", author_id="U1")

    with Session(engine) as session:
        steering = session.exec(select(GoosecrackerSteering)).all()
    assert steering[0].tier == ""  # _make_agent_session defaults tier to ""


def test_continue_agent_session_multiple_steers_insert_separate_rows(engine, fake_api):
    """Multiple steering replies each become their own row, not a joined string
    (unlike the legacy pending queue)."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        _make_agent_session(engine, "agent-t3", running=True)
        goosecracker.continue_session("agent-t3", "first reply", author_id="U1")
        goosecracker.continue_session("agent-t3", "second reply", author_id="U2")

    with Session(engine) as session:
        steering = session.exec(
            select(GoosecrackerSteering).order_by(GoosecrackerSteering.id)
        ).all()
    assert [s.text for s in steering] == ["first reply", "second reply"]
    assert [s.author_id for s in steering] == ["U1", "U2"]


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
    """When pending is non-empty, drain returns (task, ack_ids), clears pending,
    promotes the batch into the in-flight slot, and keeps running=True so the
    runner dispatches the next turn."""
    _make_agent_session(
        engine,
        "d-t1",
        running=True,
        pending="do something extra",
        pending_message_ids="m1\nm2",
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        drained = goosecracker.drain_agent_queue("d-t1")

    assert drained == ("do something extra", ["m1", "m2"])
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "d-t1")
    assert row.pending == ""
    assert row.pending_message_ids == ""
    assert row.inflight_task == "do something extra"
    assert row.inflight_ack_ids == "m1\nm2"
    assert row.runner_instance == goosecracker.INSTANCE_TOKEN
    assert row.running  # runner handles dispatching the next turn


def test_drain_agent_queue_clears_running_when_empty(engine):
    """When pending is empty, drain returns None and fully idles the row so the
    thread accepts new replies again."""
    _make_agent_session(engine, "d-t2", running=True, pending="")
    with patch("chat.goosecracker.get_engine", return_value=engine):
        task = goosecracker.drain_agent_queue("d-t2")

    assert task is None
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "d-t2")
    assert not row.running
    assert row.running_since is None
    assert row.inflight_task == ""


def test_drain_agent_queue_returns_none_for_unknown_thread(engine):
    """No session row for the thread: drain returns None gracefully."""
    with patch("chat.goosecracker.get_engine", return_value=engine):
        assert goosecracker.drain_agent_queue("no-such-thread") is None


# ---------------------------------------------------------------------------
# Self-heal: stale / orphaned running turns
# ---------------------------------------------------------------------------


def test_continue_agent_session_reclaims_stale_running_turn(engine, fake_api):
    """A running turn owned by a dead process (foreign runner_instance) is not
    treated as live: the new reply reclaims it, folding the dead turn's inflight
    task + this message into a fresh dispatch so nothing is lost."""
    _make_agent_session(
        engine,
        "stale-t1",
        running=True,
        runner_instance="dead-process",
        inflight_task="prev work",
        inflight_ack_ids="m1",
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        result = goosecracker.continue_session("stale-t1", "resume please", "m2")

    assert result.get("action") == "dispatched"
    call = fake_api.submit.call_args
    assert call.args[0] == "prev work\nresume please"  # dead task folded in
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "stale-t1")
    assert row.runner_instance == goosecracker.INSTANCE_TOKEN  # re-owned
    assert row.inflight_ack_ids == "m1"  # prior queued msg still tracked
    assert row.running


def test_continue_agent_session_stale_by_timeout_is_reclaimed(engine, fake_api):
    """A running turn older than STALE_AFTER with no live owner is reclaimed even
    when the runner_instance matches (process lived but the turn died silently)."""
    old = datetime.now(timezone.utc) - goosecracker.STALE_AFTER - timedelta(minutes=1)
    _make_agent_session(
        engine,
        "stale-t2",
        running=True,
        runner_instance=goosecracker.INSTANCE_TOKEN,
        running_since=old,
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        result = goosecracker.continue_session("stale-t2", "still there?", "m3")

    assert result.get("action") == "dispatched"
    fake_api.submit.assert_called_once()


def test_mark_inflight_running_enqueues_running_reactions(engine):
    """mark_inflight_running flips each in-flight message ⏳→👀 via outbox rows."""
    _make_agent_session(engine, "r-t1", running=True, inflight_ack_ids="m1\nm2")
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.mark_inflight_running("r-t1")

    with Session(engine) as session:
        rows = session.query(DiscordOutbox).order_by(DiscordOutbox.id).all()
    # Two messages x (remove ⏳, add 👀) = 4 reaction rows, all on the thread.
    assert len(rows) == 4
    assert {r.target_message_id for r in rows} == {"m1", "m2"}
    assert all(r.channel_id == "r-t1" for r in rows)
    removes = [r for r in rows if r.reaction_remove]
    adds = [r for r in rows if not r.reaction_remove]
    assert all(r.reaction == goosecracker.REACTION_QUEUED for r in removes)
    assert all(r.reaction == goosecracker.REACTION_RUNNING for r in adds)


def test_mark_inflight_running_noop_without_acks(engine):
    """No queued messages -> no reaction rows (first turn rides progress reply)."""
    _make_agent_session(engine, "r-t2", running=True, inflight_ack_ids="")
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.mark_inflight_running("r-t2")
    with Session(engine) as session:
        assert session.query(DiscordOutbox).count() == 0


def test_ack_inflight_success_adds_done_and_clears(engine):
    """On success, ack_inflight adds ✅ (and removes transient markers) per acked
    message, then clears the in-flight slot."""
    _make_agent_session(
        engine, "a-t1", running=True, inflight_task="work", inflight_ack_ids="m1"
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.ack_inflight("a-t1", True)

    with Session(engine) as session:
        rows = session.query(DiscordOutbox).all()
        row = session.get(GoosecrackerSession, "a-t1")
    added = [r for r in rows if not r.reaction_remove]
    assert [r.reaction for r in added] == [goosecracker.REACTION_DONE]
    assert row.inflight_task == ""
    assert row.inflight_ack_ids == ""


def test_ack_inflight_failure_adds_cross(engine):
    """On failure, the terminal reaction is ❌."""
    _make_agent_session(engine, "a-t2", running=True, inflight_ack_ids="m1")
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.ack_inflight("a-t2", False)
    with Session(engine) as session:
        added = [r for r in session.query(DiscordOutbox).all() if not r.reaction_remove]
    assert [r.reaction for r in added] == [goosecracker.REACTION_FAILED]


def test_reclaim_orphaned_agent_sessions_redispatches(engine, fake_api):
    """A running agent session owned by a dead process is re-dispatched and
    re-owned; a live one (this process's token) is left alone."""
    _make_agent_session(
        engine,
        "orphan",
        running=True,
        runner_instance="dead-process",
        inflight_task="half-done work",
        inflight_ack_ids="m1",
        pending="a queued follow-up",
        pending_message_ids="m2",
    )
    _make_agent_session(
        engine,
        "live",
        running=True,
        runner_instance=goosecracker.INSTANCE_TOKEN,
        inflight_task="in progress",
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        reclaimed = goosecracker.reclaim_orphaned_agent_sessions()

    assert reclaimed == 1
    call = fake_api.submit.call_args
    # Dead turn's inflight + pending folded losslessly into the re-dispatch.
    assert call.args[0] == "half-done work\na queued follow-up"
    assert call.kwargs["discord_thread"] == "orphan"
    with Session(engine) as session:
        orphan = session.get(GoosecrackerSession, "orphan")
        live = session.get(GoosecrackerSession, "live")
    assert orphan.runner_instance == goosecracker.INSTANCE_TOKEN  # re-owned
    assert orphan.inflight_ack_ids == "m1\nm2"
    assert orphan.pending == ""
    assert live.inflight_task == "in progress"  # untouched


def test_drain_agent_queue_aborts_when_reowned(engine):
    """If the turn was re-owned by another process (leadership handover), drain
    returns None WITHOUT touching pending/running - the new owner drives it."""
    _make_agent_session(
        engine,
        "reowned",
        running=True,
        runner_instance="other-process",
        pending="queued work",
        pending_message_ids="m1",
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        assert goosecracker.drain_agent_queue("reowned") is None
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "reowned")
    # Untouched: the new owner still has the queued work to run.
    assert row.pending == "queued work"
    assert row.running


def test_ack_inflight_skips_when_reowned(engine):
    """ack_inflight is a no-op when another process owns the turn (no double-post,
    no clobber of the new owner's in-flight slot)."""
    _make_agent_session(
        engine,
        "reowned-ack",
        running=True,
        runner_instance="other-process",
        inflight_task="work",
        inflight_ack_ids="m1",
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.ack_inflight("reowned-ack", True)
    with Session(engine) as session:
        assert session.query(DiscordOutbox).count() == 0
        row = session.get(GoosecrackerSession, "reowned-ack")
    assert row.inflight_ack_ids == "m1"  # left for the new owner


def test_force_idle_thread_cas(engine):
    """force_idle_thread idles a row this process owns, but leaves a re-owned row
    alone (never clobbers a turn another process now runs)."""
    _make_agent_session(
        engine, "mine", running=True, runner_instance=goosecracker.INSTANCE_TOKEN
    )
    _make_agent_session(engine, "theirs", running=True, runner_instance="other-process")
    with patch("chat.goosecracker.get_engine", return_value=engine):
        goosecracker.force_idle_thread("mine")
        goosecracker.force_idle_thread("theirs")
    with Session(engine) as session:
        assert not session.get(GoosecrackerSession, "mine").running
        assert session.get(GoosecrackerSession, "theirs").running  # untouched


def test_reclaim_orphaned_idles_when_nothing_to_run(engine, fake_api):
    """A foreign-owned running session with no recoverable task is idled, not
    re-dispatched (never dispatches an empty turn)."""
    _make_agent_session(
        engine, "empty-orphan", running=True, runner_instance="dead-process"
    )
    with patch("chat.goosecracker.get_engine", return_value=engine):
        reclaimed = goosecracker.reclaim_orphaned_agent_sessions()

    assert reclaimed == 0
    fake_api.submit.assert_not_called()
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "empty-orphan")
    assert not row.running


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
