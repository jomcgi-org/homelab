"""Unit tests for scheduler/api.py — _complete_job, _fail_job, _release_lock.

These functions are pure SQLModel state transitions that work with any SQL
backend; tested here with in-memory SQLite so no Postgres is required.

The advisory-lock claim query (_claim_next_job) uses Postgres-specific SQL
(make_interval, FOR UPDATE SKIP LOCKED) and is tested separately in
scheduler_claim_test.py using mocks.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from scheduler.api import ScheduledJob, _complete_job, _fail_job, _release_lock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with schema stripped (SQLite has no schemas)."""
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
    with Session(engine) as session:
        yield session

    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _make_locked_job(
    session: Session,
    *,
    name: str = "test-job",
    interval_secs: int = 60,
    ttl_secs: int = 300,
    next_run_at: datetime | None = None,
    last_status: str | None = None,
) -> ScheduledJob:
    """Insert a ScheduledJob row currently held by a worker."""
    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        name=name,
        interval_secs=interval_secs,
        next_run_at=next_run_at or now,
        ttl_secs=ttl_secs,
        locked_by="test-worker",
        locked_at=now,
        last_status=last_status,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# _complete_job — successful run state transition
# ---------------------------------------------------------------------------


class TestCompleteJob:
    def test_clears_locked_by(self, session):
        """_complete_job sets locked_by=None after successful execution."""
        job = _make_locked_job(session, name="j1")
        _complete_job(session, job, override=None)

        refreshed = session.get(ScheduledJob, "j1")
        assert refreshed is not None
        assert refreshed.locked_by is None

    def test_clears_locked_at(self, session):
        """_complete_job sets locked_at=None after successful execution."""
        job = _make_locked_job(session, name="j2")
        _complete_job(session, job, override=None)

        refreshed = session.get(ScheduledJob, "j2")
        assert refreshed is not None
        assert refreshed.locked_at is None

    def test_sets_last_status_ok(self, session):
        """_complete_job marks last_status='ok'."""
        job = _make_locked_job(session, name="j3")
        _complete_job(session, job, override=None)

        refreshed = session.get(ScheduledJob, "j3")
        assert refreshed is not None
        assert refreshed.last_status == "ok"

    def test_sets_last_run_at_to_approximately_now(self, session):
        """_complete_job records last_run_at within a tight window around now."""
        before = datetime.now(timezone.utc)
        job = _make_locked_job(session, name="j4")
        _complete_job(session, job, override=None)
        after = datetime.now(timezone.utc)

        refreshed = session.get(ScheduledJob, "j4")
        assert refreshed is not None
        assert refreshed.last_run_at is not None
        last_run = refreshed.last_run_at.replace(tzinfo=timezone.utc)
        assert before <= last_run <= after

    def test_advances_next_run_at_by_interval_without_override(self, session):
        """Without an override, next_run_at = now + interval_secs."""
        job = _make_locked_job(session, name="j5", interval_secs=120)
        before = datetime.now(timezone.utc)
        _complete_job(session, job, override=None)
        after = datetime.now(timezone.utc)

        refreshed = session.get(ScheduledJob, "j5")
        assert refreshed is not None
        next_run = refreshed.next_run_at.replace(tzinfo=timezone.utc)
        assert (
            before + timedelta(seconds=120)
            <= next_run
            <= after + timedelta(seconds=120)
        )

    def test_uses_override_for_next_run_at(self, session):
        """When an override datetime is provided, next_run_at is set to it exactly."""
        job = _make_locked_job(session, name="j6", interval_secs=60)
        override_dt = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        _complete_job(session, job, override=override_dt)

        refreshed = session.get(ScheduledJob, "j6")
        assert refreshed is not None
        stored = refreshed.next_run_at.replace(tzinfo=timezone.utc)
        assert stored == override_dt

    def test_override_ignores_interval(self, session):
        """The override datetime is used regardless of the interval_secs value."""
        job = _make_locked_job(session, name="j7", interval_secs=86400)
        override_dt = datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        _complete_job(session, job, override=override_dt)

        refreshed = session.get(ScheduledJob, "j7")
        assert refreshed is not None
        stored = refreshed.next_run_at.replace(tzinfo=timezone.utc)
        assert stored == override_dt

    def test_persists_changes_to_db(self, session):
        """_complete_job calls session.commit() so changes survive session expiry."""
        job = _make_locked_job(session, name="j8")
        _complete_job(session, job, override=None)

        # Expire the session's identity map to force a DB round-trip
        session.expire_all()
        reloaded = session.get(ScheduledJob, "j8")
        assert reloaded is not None
        assert reloaded.last_status == "ok"
        assert reloaded.locked_by is None


# ---------------------------------------------------------------------------
# _fail_job — error state transition
# ---------------------------------------------------------------------------


class TestFailJob:
    def test_clears_locked_by(self, session):
        """_fail_job sets locked_by=None even on failure."""
        job = _make_locked_job(session, name="f1")
        _fail_job(session, job, error="something went wrong")

        refreshed = session.get(ScheduledJob, "f1")
        assert refreshed is not None
        assert refreshed.locked_by is None

    def test_clears_locked_at(self, session):
        """_fail_job sets locked_at=None even on failure."""
        job = _make_locked_job(session, name="f2")
        _fail_job(session, job, error="something went wrong")

        refreshed = session.get(ScheduledJob, "f2")
        assert refreshed is not None
        assert refreshed.locked_at is None

    def test_sets_error_status_prefix(self, session):
        """_fail_job sets last_status to 'error: <message>'."""
        job = _make_locked_job(session, name="f3")
        _fail_job(session, job, error="db timeout")

        refreshed = session.get(ScheduledJob, "f3")
        assert refreshed is not None
        assert refreshed.last_status is not None
        assert refreshed.last_status.startswith("error:")
        assert "db timeout" in refreshed.last_status

    def test_truncates_long_error_at_200_chars(self, session):
        """_fail_job truncates error strings exceeding 200 characters."""
        long_error = "x" * 300
        job = _make_locked_job(session, name="f4")
        _fail_job(session, job, error=long_error)

        refreshed = session.get(ScheduledJob, "f4")
        assert refreshed is not None
        assert refreshed.last_status is not None
        # "error: " prefix (7 chars) + at most 200 chars
        assert len(refreshed.last_status) <= 7 + 200

    def test_exact_200_char_error_stored_in_full(self, session):
        """A 200-character error is stored without truncation (boundary condition)."""
        exact_error = "e" * 200
        job = _make_locked_job(session, name="f5")
        _fail_job(session, job, error=exact_error)

        refreshed = session.get(ScheduledJob, "f5")
        assert refreshed is not None
        assert refreshed.last_status == f"error: {exact_error}"

    def test_advances_next_run_at_by_interval(self, session):
        """_fail_job advances next_run_at by interval_secs so the job is retried."""
        job = _make_locked_job(session, name="f6", interval_secs=60)
        before = datetime.now(timezone.utc)
        _fail_job(session, job, error="transient error")
        after = datetime.now(timezone.utc)

        refreshed = session.get(ScheduledJob, "f6")
        assert refreshed is not None
        next_run = refreshed.next_run_at.replace(tzinfo=timezone.utc)
        assert (
            before + timedelta(seconds=60) <= next_run <= after + timedelta(seconds=60)
        )

    def test_sets_last_run_at(self, session):
        """_fail_job records last_run_at even when the handler failed."""
        before = datetime.now(timezone.utc)
        job = _make_locked_job(session, name="f7")
        _fail_job(session, job, error="oops")
        after = datetime.now(timezone.utc)

        refreshed = session.get(ScheduledJob, "f7")
        assert refreshed is not None
        assert refreshed.last_run_at is not None
        last_run = refreshed.last_run_at.replace(tzinfo=timezone.utc)
        assert before <= last_run <= after

    def test_empty_error_string_stored(self, session):
        """An empty error string produces last_status='error: '."""
        job = _make_locked_job(session, name="f8")
        _fail_job(session, job, error="")

        refreshed = session.get(ScheduledJob, "f8")
        assert refreshed is not None
        assert refreshed.last_status == "error: "

    def test_persists_changes_to_db(self, session):
        """_fail_job calls session.commit() so changes survive session expiry."""
        job = _make_locked_job(session, name="f9")
        _fail_job(session, job, error="commit check")

        session.expire_all()
        reloaded = session.get(ScheduledJob, "f9")
        assert reloaded is not None
        assert reloaded.locked_by is None
        assert reloaded.last_status is not None
        assert "commit check" in reloaded.last_status


# ---------------------------------------------------------------------------
# _release_lock — lock release without advancing the schedule
# ---------------------------------------------------------------------------


class TestReleaseLock:
    def test_clears_locked_by(self, session):
        """_release_lock sets locked_by=None."""
        job = _make_locked_job(session, name="r1")
        _release_lock(session, job)

        refreshed = session.get(ScheduledJob, "r1")
        assert refreshed is not None
        assert refreshed.locked_by is None

    def test_clears_locked_at(self, session):
        """_release_lock sets locked_at=None."""
        job = _make_locked_job(session, name="r2")
        _release_lock(session, job)

        refreshed = session.get(ScheduledJob, "r2")
        assert refreshed is not None
        assert refreshed.locked_at is None

    def test_preserves_next_run_at(self, session):
        """_release_lock does not modify next_run_at — schedule stays intact."""
        original_next = datetime(2030, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        job = _make_locked_job(session, name="r3", next_run_at=original_next)

        _release_lock(session, job)

        refreshed = session.get(ScheduledJob, "r3")
        assert refreshed is not None
        stored_next = refreshed.next_run_at.replace(tzinfo=timezone.utc)
        assert stored_next == original_next

    def test_preserves_last_status(self, session):
        """_release_lock does not overwrite last_status from a prior run."""
        job = _make_locked_job(session, name="r4", last_status="ok")

        _release_lock(session, job)

        refreshed = session.get(ScheduledJob, "r4")
        assert refreshed is not None
        assert refreshed.last_status == "ok"

    def test_does_not_set_last_run_at(self, session):
        """_release_lock leaves last_run_at=None — no handler was executed."""
        job = _make_locked_job(session, name="r5")

        _release_lock(session, job)

        refreshed = session.get(ScheduledJob, "r5")
        assert refreshed is not None
        assert refreshed.last_run_at is None

    def test_persists_changes_to_db(self, session):
        """_release_lock commits so the lock release survives session expiry."""
        job = _make_locked_job(session, name="r6")
        _release_lock(session, job)

        session.expire_all()
        reloaded = session.get(ScheduledJob, "r6")
        assert reloaded is not None
        assert reloaded.locked_by is None
        assert reloaded.locked_at is None
