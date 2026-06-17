"""Unit tests for scheduler/api.py — ScheduledJob model + register_job()."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from scheduler.api import ScheduledJob, _registry, register_job


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


@pytest.fixture(autouse=True)
def _clear_registry():
    """Ensure a clean handler registry for each test."""
    _registry.clear()
    yield
    _registry.clear()


# ---------------------------------------------------------------------------
# ScheduledJob model — table metadata
# ---------------------------------------------------------------------------


class TestScheduledJobTableMetadata:
    def test_table_name(self):
        """ScheduledJob.__tablename__ is 'scheduled_jobs'."""
        assert ScheduledJob.__tablename__ == "scheduled_jobs"

    def test_schema_is_scheduler(self):
        """ScheduledJob table lives in the 'scheduler' schema."""
        args = ScheduledJob.__table_args__
        assert isinstance(args, dict)
        assert args.get("schema") == "scheduler"


# ---------------------------------------------------------------------------
# ScheduledJob model — field defaults
# ---------------------------------------------------------------------------


class TestScheduledJobDefaults:
    def test_optional_fields_default_to_none(self):
        """Optional fields (last_run_at, last_status, locked_by, locked_at) default to None."""
        job = ScheduledJob(
            name="test",
            interval_secs=60,
            next_run_at=datetime.now(timezone.utc),
        )
        assert job.last_run_at is None
        assert job.last_status is None
        assert job.locked_by is None
        assert job.locked_at is None

    def test_ttl_secs_defaults_to_1200(self):
        """ttl_secs defaults to 1200 (20m) when not provided."""
        job = ScheduledJob(
            name="test",
            interval_secs=60,
            next_run_at=datetime.now(timezone.utc),
        )
        assert job.ttl_secs == 1200

    def test_explicit_ttl_overrides_default(self):
        """Explicitly provided ttl_secs overrides the default."""
        job = ScheduledJob(
            name="test",
            interval_secs=60,
            next_run_at=datetime.now(timezone.utc),
            ttl_secs=600,
        )
        assert job.ttl_secs == 600

    def test_primary_key_is_name(self):
        """The name field is the primary key."""
        pk_cols = [c.name for c in ScheduledJob.__table__.primary_key.columns]
        assert pk_cols == ["name"]


# ---------------------------------------------------------------------------
# ScheduledJob model — persistence
# ---------------------------------------------------------------------------


class TestScheduledJobPersistence:
    def test_round_trip(self, session):
        """A ScheduledJob row round-trips through the database correctly."""
        now = datetime.now(timezone.utc)
        session.add(
            ScheduledJob(name="sync", interval_secs=120, next_run_at=now, ttl_secs=600)
        )
        session.commit()

        result = session.exec(select(ScheduledJob)).first()
        assert result is not None
        assert result.name == "sync"
        assert result.interval_secs == 120
        assert result.ttl_secs == 600


# ---------------------------------------------------------------------------
# register_job — new job
# ---------------------------------------------------------------------------


class TestRegisterJobNew:
    def test_adds_handler_to_registry(self, session):
        """register_job stores the handler in the in-memory _registry."""

        async def my_handler(s: Session) -> None:
            return None

        register_job(session, name="new-job", interval_secs=60, handler=my_handler)
        assert "new-job" in _registry
        assert _registry["new-job"] is my_handler

    def test_inserts_row_for_new_job(self, session):
        """register_job creates a new ScheduledJob row when one doesn't exist."""

        async def handler(s: Session) -> None:
            return None

        register_job(session, name="fresh", interval_secs=30, handler=handler)

        job = session.get(ScheduledJob, "fresh")
        assert job is not None
        assert job.interval_secs == 30
        assert job.ttl_secs == 1200
        assert job.next_run_at is not None

    def test_new_job_uses_custom_ttl(self, session):
        """register_job passes ttl_secs through for new jobs."""

        async def handler(s: Session) -> None:
            return None

        register_job(
            session, name="custom-ttl", interval_secs=60, handler=handler, ttl_secs=900
        )

        job = session.get(ScheduledJob, "custom-ttl")
        assert job is not None
        assert job.ttl_secs == 900


# ---------------------------------------------------------------------------
# register_job — existing job (upsert)
# ---------------------------------------------------------------------------


class TestRegisterJobExisting:
    def test_updates_interval_and_ttl(self, session):
        """register_job updates interval_secs and ttl_secs on an existing row."""
        now = datetime.now(timezone.utc)
        session.add(
            ScheduledJob(
                name="existing", interval_secs=60, next_run_at=now, ttl_secs=300
            )
        )
        session.commit()

        async def handler(s: Session) -> None:
            return None

        register_job(
            session,
            name="existing",
            interval_secs=120,
            handler=handler,
            ttl_secs=600,
        )

        job = session.get(ScheduledJob, "existing")
        assert job is not None
        assert job.interval_secs == 120
        assert job.ttl_secs == 600

    def test_preserves_next_run_at(self, session):
        """register_job does not change next_run_at when updating an existing job."""
        original_next = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        session.add(
            ScheduledJob(
                name="keep-timing",
                interval_secs=60,
                next_run_at=original_next,
                ttl_secs=300,
            )
        )
        session.commit()

        async def handler(s: Session) -> None:
            return None

        register_job(session, name="keep-timing", interval_secs=120, handler=handler)

        job = session.get(ScheduledJob, "keep-timing")
        assert job is not None
        # next_run_at should remain unchanged
        assert job.next_run_at.replace(tzinfo=timezone.utc) == original_next

    def test_preserves_last_run_at(self, session):
        """register_job does not overwrite last_run_at on an existing job."""
        now = datetime.now(timezone.utc)
        last = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        session.add(
            ScheduledJob(
                name="has-history",
                interval_secs=60,
                next_run_at=now,
                last_run_at=last,
                last_status="ok",
                ttl_secs=300,
            )
        )
        session.commit()

        async def handler(s: Session) -> None:
            return None

        register_job(session, name="has-history", interval_secs=90, handler=handler)

        job = session.get(ScheduledJob, "has-history")
        assert job is not None
        assert job.last_run_at is not None
        assert job.last_status == "ok"
