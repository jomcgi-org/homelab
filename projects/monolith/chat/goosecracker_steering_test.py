"""Tests for chat.goosecracker mid-run steering (ADR 035 Phase 2 Task 2.1):
enqueue_steering, fetch_steering, and their transcript-attribution and
delivered-flag semantics. DB-backed tests run against in-memory SQLite with
the chat schema stripped, mirroring the fixture in goosecracker_test.py."""

from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat import goosecracker
from chat.models import GoosecrackerSession, GoosecrackerSteering


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


def _make_session(engine, thread_id: str, transcript: str = "initial task") -> None:
    with Session(engine) as session:
        session.add(
            GoosecrackerSession(
                discord_thread=thread_id,
                recipe="agent",
                transcript=transcript,
            )
        )
        session.commit()


class TestEnqueueSteering:
    def test_inserts_undelivered_row(self, engine):
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("t1", "m1", "u1", "beta", "  do this  ")

        with Session(engine) as session:
            rows = session.query(GoosecrackerSteering).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.thread_id == "t1"
        assert row.message_id == "m1"
        assert row.author_id == "u1"
        assert row.tier == "beta"
        assert row.text == "do this"  # stripped
        assert row.delivered is False

    def test_empty_text_is_noop(self, engine):
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("t1", "m1", "u1", "beta", "   ")

        with Session(engine) as session:
            assert session.query(GoosecrackerSteering).count() == 0


class TestFetchSteering:
    def test_returns_undelivered_in_order_and_marks_delivered(self, engine):
        _make_session(engine, "t1")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("t1", "m1", "u1", "beta", "first")
            goosecracker.enqueue_steering("t1", "m2", "u2", "beta", "second")

            delivered = goosecracker.fetch_steering("t1")

        assert [d["text"] for d in delivered] == ["first", "second"]
        assert [d["message_id"] for d in delivered] == ["m1", "m2"]
        assert delivered[0]["author_id"] == "u1"
        assert delivered[0]["tier"] == "beta"
        assert all("id" in d for d in delivered)

        with Session(engine) as session:
            rows = session.query(GoosecrackerSteering).all()
        assert all(r.delivered for r in rows)

    def test_second_fetch_returns_nothing(self, engine):
        _make_session(engine, "t1")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("t1", "m1", "u1", "", "hello")
            goosecracker.fetch_steering("t1")
            second = goosecracker.fetch_steering("t1")

        assert second == []

    def test_after_id_filters_older_rows(self, engine):
        _make_session(engine, "t1")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("t1", "m1", "u1", "", "first")
            goosecracker.enqueue_steering("t1", "m2", "u2", "", "second")

            with Session(engine) as session:
                first_id = (
                    session.query(GoosecrackerSteering)
                    .order_by(GoosecrackerSteering.id)
                    .first()
                    .id
                )

            delivered = goosecracker.fetch_steering("t1", after_id=first_id)

        assert [d["text"] for d in delivered] == ["second"]

    def test_appends_to_transcript_with_attribution(self, engine):
        _make_session(engine, "t1", transcript="build a clock")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("t1", "m1", "user-42", "", "make it red")
            goosecracker.fetch_steering("t1")

        with Session(engine) as session:
            row = session.get(GoosecrackerSession, "t1")
        assert "build a clock" in row.transcript
        assert "[steering from user-42]: make it red" in row.transcript

    def test_missing_session_row_still_delivers(self, engine):
        # No GoosecrackerSession row for "no-session" - fetch must not raise.
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("no-session", "m1", "u1", "", "hi")
            delivered = goosecracker.fetch_steering("no-session")

        assert [d["text"] for d in delivered] == ["hi"]

    def test_other_threads_not_returned(self, engine):
        _make_session(engine, "t1")
        _make_session(engine, "t2")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            goosecracker.enqueue_steering("t1", "m1", "u1", "", "for t1")
            goosecracker.enqueue_steering("t2", "m2", "u2", "", "for t2")

            delivered = goosecracker.fetch_steering("t1")

        assert [d["text"] for d in delivered] == ["for t1"]
