"""Unit tests for scheduler/service.py."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from scheduler import service
from scheduler.api import ScheduledJob, _registry


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


@pytest.fixture(autouse=True)
def _clear_registry():
    _registry.clear()
    yield
    _registry.clear()


def _seed(session: Session, name: str, *, next_run_at: datetime) -> None:
    session.add(
        ScheduledJob(
            name=name,
            interval_secs=60,
            next_run_at=next_run_at,
            ttl_secs=300,
        )
    )
    session.commit()


class TestListJobs:
    def test_returns_jobs_sorted_by_name(self, session):
        now = datetime.now(timezone.utc)
        _seed(session, "b.job", next_run_at=now)
        _seed(session, "a.job", next_run_at=now)

        jobs = service.list_jobs(session)
        assert [j.name for j in jobs] == ["a.job", "b.job"]

    def test_returns_empty_when_no_jobs(self, session):
        assert service.list_jobs(session) == []

    def test_has_handler_reflects_registry(self, session):
        async def _h(s: Session) -> None:
            return None

        _registry["registered.job"] = _h
        _seed(session, "registered.job", next_run_at=datetime.now(timezone.utc))
        _seed(session, "orphan.job", next_run_at=datetime.now(timezone.utc))

        by_name = {j.name: j for j in service.list_jobs(session)}
        assert by_name["registered.job"].has_handler is True
        assert by_name["orphan.job"].has_handler is False


class TestGetJob:
    def test_returns_view_for_existing_job(self, session):
        _seed(session, "j", next_run_at=datetime.now(timezone.utc))
        view = service.get_job(session, "j")
        assert view is not None
        assert view.name == "j"
        assert view.interval_secs == 60

    def test_returns_none_for_missing_job(self, session):
        assert service.get_job(session, "nope") is None


class TestMarkForImmediateRun:
    def test_advances_next_run_at_to_now(self, session):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        _seed(session, "j", next_run_at=future)

        before = datetime.now(timezone.utc)
        view = service.mark_for_immediate_run(session, "j")
        after = datetime.now(timezone.utc)

        assert view is not None
        # Compare in a tz-naive way: SQLite drops tzinfo on round-trip.
        next_run = view.next_run_at
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        assert before <= next_run <= after

    def test_returns_none_for_missing_job(self, session):
        assert service.mark_for_immediate_run(session, "nope") is None

    def test_idempotent_within_same_second(self, session):
        _seed(session, "j", next_run_at=datetime.now(timezone.utc) + timedelta(hours=1))
        first = service.mark_for_immediate_run(session, "j")
        second = service.mark_for_immediate_run(session, "j")
        assert first is not None and second is not None
        # Both calls succeed without error; second's next_run_at >= first's.
        first_t = (
            first.next_run_at.replace(tzinfo=timezone.utc)
            if first.next_run_at.tzinfo is None
            else first.next_run_at
        )
        second_t = (
            second.next_run_at.replace(tzinfo=timezone.utc)
            if second.next_run_at.tzinfo is None
            else second.next_run_at
        )
        assert second_t >= first_t
