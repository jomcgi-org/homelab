"""Unit tests for the retained ScheduledJob model and row persistence."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from scheduler.api import ScheduledJob


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    """File-backed SQLite session with schema stripped (SQLite has no schemas)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scheduler.db'}",
        connect_args={"check_same_thread": False},
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
