"""Tests for chat.goosecracker mid-run steering (ADR 035 Phase 2):
enqueue_steering, fetch_steering, and their transcript-attribution and
delivered-flag semantics (Task 2.1), plus the unguessable per-session steering
token that keys the guest fetch endpoint (Phase 2 hardening). DB-backed tests
run against in-memory SQLite with the chat schema stripped, mirroring the
fixture in goosecracker_test.py."""

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


class TestSteeringToken:
    """ADR 035 Phase 2 hardening: the steering endpoint is keyed on an
    unguessable per-session token, not the guessable Discord thread id."""

    def test_assigns_and_persists_a_token(self, engine):
        _make_session(engine, "t1")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            token = goosecracker.ensure_steering_token("t1")

        assert token
        with Session(engine) as session:
            row = session.get(GoosecrackerSession, "t1")
        assert row.steering_token == token

    def test_idempotent(self, engine):
        _make_session(engine, "t1")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            first = goosecracker.ensure_steering_token("t1")
            second = goosecracker.ensure_steering_token("t1")

        assert first == second

    def test_unknown_thread_returns_empty(self, engine):
        with patch("chat.goosecracker.get_engine", return_value=engine):
            token = goosecracker.ensure_steering_token("no-such-thread")

        assert token == ""

    def test_resolves_token_to_its_thread(self, engine):
        _make_session(engine, "t1")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            token = goosecracker.ensure_steering_token("t1")

            resolved = goosecracker.thread_for_steering_token(token)

        assert resolved == "t1"

    def test_unknown_token_returns_none(self, engine):
        with patch("chat.goosecracker.get_engine", return_value=engine):
            assert goosecracker.thread_for_steering_token("not-a-real-token") is None

    def test_empty_token_returns_none(self, engine):
        with patch("chat.goosecracker.get_engine", return_value=engine):
            assert goosecracker.thread_for_steering_token("") is None

    def test_cross_thread_isolation(self, engine):
        """Two sessions get distinct tokens, and each token resolves only to
        its own thread: guest A's token can never fetch guest B's steering."""
        _make_session(engine, "t1")
        _make_session(engine, "t2")
        with patch("chat.goosecracker.get_engine", return_value=engine):
            token_a = goosecracker.ensure_steering_token("t1")
            token_b = goosecracker.ensure_steering_token("t2")

            assert token_a != token_b
            assert goosecracker.thread_for_steering_token(token_a) == "t1"
            assert goosecracker.thread_for_steering_token(token_b) == "t2"

            goosecracker.enqueue_steering("t1", "m1", "u1", "", "for t1 only")
            goosecracker.enqueue_steering("t2", "m2", "u2", "", "for t2 only")

            # The endpoint's actual flow: resolve the token to a thread, then
            # fetch that thread's steering. Token A must never surface t2's rows.
            thread_for_a = goosecracker.thread_for_steering_token(token_a)
            delivered_for_a = goosecracker.fetch_steering(thread_for_a)

        assert [d["text"] for d in delivered_for_a] == ["for t1 only"]
