"""Feedback races against separate connections and the real chart migrations.

The bdd_test pg fixture starts disposable PostgreSQL, never a live database.
Events pause SQL at the race boundary; pg_blocking_pids proves the competitor
actually reached a blocked statement before the first transaction is released.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlmodel import Session, create_engine, select

from grimoire import feedback
from grimoire.models import Feedback


@pytest.fixture
def database(pg):
    engine = create_engine(pg.url)
    prefix = f"feedback-{uuid4().hex}"
    try:
        yield engine, prefix
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM grimoire.feedback WHERE author_email LIKE :prefix"),
                {"prefix": prefix + "%"},
            )
        engine.dispose()


def seed(engine, email, times):
    with Session(engine) as session:
        session.add_all(
            Feedback(author_email=email, page="/", text="Fixture", created_at=when)
            for when in times
        )
        session.commit()


def submit(engine, email, *, label="", pids=None):
    with Session(engine) as session:
        connection = session.connection()
        info = connection.info
        info["feedback_worker"] = label
        # Bound failed tests; all sessions are closed even on a 429.
        connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
        pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        if pids is not None:
            pids.put(pid)
        try:
            feedback.submit_feedback(
                feedback.FeedbackRequest(text="Fixture", page="/"),
                None,
                email,
                session,
            )
            return 201
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.detail == (
                "You have sent 10 notes in the last day. Try again tomorrow."
            )
            return exc.status_code
        finally:
            info.pop("feedback_worker", None)


def wait_for_block_or_crossing(engine, pid, crossed):
    """Return only after PostgreSQL reports a waiter or the race is exposed."""
    deadline = monotonic() + 5
    with engine.connect() as observer:
        while monotonic() < deadline:
            if crossed.is_set():
                return False
            if observer.execute(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            ).scalar_one():
                return True
            sleep(0.01)
    pytest.fail("competing connection neither blocked nor crossed the boundary")


def test_simultaneous_tenth_note_serializes_count_through_commit(database):
    engine, email = database
    seed(engine, email, [datetime.now(UTC)] * 9)
    counted, release, crossed = Event(), Event(), Event()
    pids = Queue()

    def after_count(connection, _cursor, statement, _parameters, _context, _many):
        if "count(" not in statement or "grimoire.feedback" not in statement:
            return
        worker = connection.info.get("feedback_worker")
        if worker == "first":
            counted.set()
            assert release.wait(10), "first count was never released"
        elif worker == "second":
            crossed.set()

    event.listen(engine, "after_cursor_execute", after_count)
    try:
        with ThreadPoolExecutor(max_workers=3) as workers:
            first = workers.submit(submit, engine, email, label="first", pids=pids)
            first_pid = pids.get(timeout=5)
            try:
                assert counted.wait(5)
                second = workers.submit(
                    submit, engine, email, label="second", pids=pids
                )
                second_pid = pids.get(timeout=5)
                assert first_pid != second_pid
                blocked = wait_for_block_or_crossing(engine, second_pid, crossed)
                # Other senders must proceed while this sender holds its lock.
                other = workers.submit(submit, engine, email + "-other")
                assert other.result(timeout=5) == 201
            finally:
                release.set()
            outcomes = [first.result(timeout=5), second.result(timeout=5)]
            assert blocked, "second count ran before the first insert committed"
            assert sorted(outcomes) == [201, 429]
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", after_count)
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(Feedback).where(Feedback.author_email == email)
                ).all()
            )
            == 10
        )
        # Commit and the rejected request's close must both release xact locks.
        assert session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
            {"key": f"grimoire-feedback:{email}"},
        ).scalar_one()


def test_rolling_boundary_and_sender_isolation(database, monkeypatch):
    engine, email = database
    now = datetime.now(UTC)
    monkeypatch.setattr(feedback, "utc_now", lambda: now)
    boundary = now - timedelta(hours=24)
    # Exactly 24h old is outside the window; one microsecond newer counts.
    seed(engine, email, [now] * 9 + [boundary, boundary - timedelta(microseconds=1)])
    seed(engine, email + "-full", [boundary + timedelta(microseconds=1)] * 10)
    assert submit(engine, email) == 201
    assert submit(engine, email) == 429
    assert submit(engine, email + "-full") == 429
    assert submit(engine, email + "-fresh") == 201


@pytest.mark.parametrize("first_action", ["file", "dismiss"])
@pytest.mark.parametrize("second_action", ["file", "dismiss"])
def test_resolution_is_serialized(database, monkeypatch, first_action, second_action):
    engine, email = database
    seed(engine, email, [datetime.now(UTC)])
    with Session(engine) as session:
        row_id = session.exec(
            select(Feedback.id).where(Feedback.author_email == email)
        ).one()
    locked, release, crossed = Event(), Event(), Event()
    calls, pids = [], Queue()

    def create_issue(repo, title, body, labels, **kwargs):
        calls.append((repo, title, body, labels))
        return 4321

    monkeypatch.setattr(feedback, "create_issue", create_issue)

    def after_lock(connection, _cursor, statement, _parameters, _context, _many):
        if "grimoire.feedback" not in statement or "FOR UPDATE" not in statement:
            return
        if connection.info.get("feedback_worker") == "first":
            locked.set()
            assert release.wait(10)
        else:
            crossed.set()

    def resolve(action, label):
        with Session(engine) as session:
            connection = session.connection()
            info = connection.info
            info["feedback_worker"] = label
            connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
            pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            try:
                if action == "file":
                    feedback.file_feedback(
                        UUID(row_id),
                        feedback.FeedbackIssueRequest(title="Public", body="Sanitized"),
                        None,
                        session,
                    )
                else:
                    feedback.dismiss_feedback(UUID(row_id), None, session)
                return 200
            except HTTPException as exc:
                assert exc.status_code == 409
                assert (
                    exc.detail
                    == f"feedback is already {'filed' if first_action == 'file' else 'dismissed'}"
                )
                return 409
            finally:
                info.pop("feedback_worker", None)

    event.listen(engine, "after_cursor_execute", after_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(resolve, first_action, "first")
            first_pid = pids.get(timeout=5)
            try:
                assert locked.wait(5)
                second = workers.submit(resolve, second_action, "second")
                second_pid = pids.get(timeout=5)
                assert first_pid != second_pid
                blocked = wait_for_block_or_crossing(engine, second_pid, crossed)
            finally:
                release.set()
            assert first.result(timeout=5) == 200
            assert second.result(timeout=5) == 409
            assert blocked
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", after_lock)
    assert calls == (
        [
            (
                feedback.GITHUB_REPO,
                "Public",
                "Sanitized\n\nFiled from Grimoire feedback.",
                ["grimoire", "feedback"],
            )
        ]
        if first_action == "file"
        else []
    )


def test_feedback_migration_has_no_public_reader_privileges(database):
    engine, _email = database
    with engine.connect() as connection:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not connection.execute(
                text(
                    "SELECT has_table_privilege('public_reader', 'grimoire.feedback', :privilege)"
                ),
                {"privilege": privilege},
            ).scalar_one()
