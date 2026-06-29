"""Tests for chat.goosecracker: the owner gate, transcript accumulation, and
session dispatch (ADR 024 Task 4). DB-backed tests run against in-memory SQLite
with the chat schema stripped, and dispatch.submit is mocked so no agent thread
is created."""

from unittest.mock import patch

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


def test_start_session_records_transcript_and_dispatches(engine):
    with (
        patch("chat.goosecracker.get_engine", return_value=engine),
        patch("chat.goosecracker.agent_api.submit") as submit,
    ):
        submit.return_value = {"thread_id": "t1", "action": "create"}
        goosecracker.start_session("thread-1", "  build a clock  ")

    submit.assert_called_once_with(
        "build a clock",
        recipe="artifact",
        tier="artifact",
        discord_thread="thread-1",
    )
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "thread-1")
        assert row is not None
        assert row.transcript == "build a clock"


def test_continue_session_appends_and_resubmits_full_transcript(engine):
    with (
        patch("chat.goosecracker.get_engine", return_value=engine),
        patch("chat.goosecracker.agent_api.submit") as submit,
    ):
        submit.return_value = {"thread_id": "t", "action": "create"}
        goosecracker.start_session("thread-1", "build a clock")
        submit.reset_mock()
        goosecracker.continue_session("thread-1", "make it red")

    submit.assert_called_once_with(
        "build a clock\n\nmake it red",
        recipe="artifact",
        tier="artifact",
        discord_thread="thread-1",
    )
    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "thread-1")
        assert row.transcript == "build a clock\n\nmake it red"


def test_continue_session_unknown_thread_returns_none(engine):
    with (
        patch("chat.goosecracker.get_engine", return_value=engine),
        patch("chat.goosecracker.agent_api.submit") as submit,
    ):
        result = goosecracker.continue_session("nope", "hi")
    assert result is None
    submit.assert_not_called()


def test_is_goosecracker_thread(engine):
    with (
        patch("chat.goosecracker.get_engine", return_value=engine),
        patch("chat.goosecracker.agent_api.submit", return_value={}),
    ):
        goosecracker.start_session("thread-1", "build a clock")
        assert goosecracker.is_goosecracker_thread("thread-1") is True
        assert goosecracker.is_goosecracker_thread("other") is False
