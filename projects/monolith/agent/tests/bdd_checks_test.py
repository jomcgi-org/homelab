"""BDD tests for ``agent.checks`` against a real PostgreSQL.

The DB-backed checks read from tables outside the ``claude_agent``
schema (``scheduler.scheduled_jobs`` and ``knowledge.{raw_inputs,
atom_raw_provenance}``) which the conftest's ``agent_db`` fixture
intentionally does not clean — its contract is "agent tables only."
This file owns its own ``checks_db`` fixture that layers per-test
seed/cleanup for the cluster tables on top of ``agent_db``'s env and
engine setup, so the two fixtures stay decoupled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session

from agent import checks


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def checks_db(agent_db: Session):
    """Layer scheduler + knowledge cleanup on top of the ``agent_db`` fixture.

    ``agent_db`` already configures ``DATABASE_URL`` and clears the
    engine cache; we just need to wipe the cluster tables this module
    reads from before and after each test.
    """
    cluster_tables = (
        "scheduler.scheduled_jobs",
        "knowledge.atom_raw_provenance",
        "knowledge.raw_inputs",
    )

    def _clean() -> None:
        for table in cluster_tables:
            agent_db.execute(text(f"DELETE FROM {table}"))
        agent_db.commit()

    _clean()
    try:
        yield agent_db
    finally:
        _clean()


def _insert_scheduled_job(
    session: Session,
    *,
    name: str,
    interval_secs: int = 300,
    next_run_at: datetime | None = None,
    locked_by: str | None = None,
    locked_at: datetime | None = None,
    ttl_secs: int = 1200,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO scheduler.scheduled_jobs
                (name, interval_secs, next_run_at, locked_by, locked_at, ttl_secs)
            VALUES
                (:name, :interval_secs, :next_run_at, :locked_by, :locked_at, :ttl_secs)
            """
        ),
        {
            "name": name,
            "interval_secs": interval_secs,
            "next_run_at": next_run_at or _now_utc(),
            "locked_by": locked_by,
            "locked_at": locked_at,
            "ttl_secs": ttl_secs,
        },
    )
    session.commit()


def _insert_raw_input(
    session: Session, *, raw_id: str, path: str, source: str = "test"
) -> int:
    row = session.execute(
        text(
            """
            INSERT INTO knowledge.raw_inputs
                (raw_id, path, source, content_hash)
            VALUES
                (:raw_id, :path, :source, :content_hash)
            RETURNING id
            """
        ),
        {
            "raw_id": raw_id,
            "path": path,
            "source": source,
            "content_hash": raw_id,
        },
    ).first()
    session.commit()
    assert row is not None
    return row[0]


def _insert_dead_letter(
    session: Session,
    *,
    raw_fk: int,
    error: str = "boom",
    retry_count: int = 3,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO knowledge.atom_raw_provenance
                (raw_fk, derived_note_id, gardener_version, error, retry_count)
            VALUES
                (:raw_fk, 'failed', 'test-v1', :error, :retry_count)
            """
        ),
        {"raw_fk": raw_fk, "error": error, "retry_count": retry_count},
    )
    session.commit()


class TestCheckStuckJobs:
    def test_returns_only_jobs_locked_longer_than_threshold(
        self, checks_db: Session
    ) -> None:
        now = _now_utc()
        # Stuck: locked 30 minutes ago.
        _insert_scheduled_job(
            checks_db,
            name="stuck",
            locked_by="host-1",
            locked_at=now - timedelta(minutes=30),
        )
        # Recent lock, not stuck.
        _insert_scheduled_job(
            checks_db,
            name="recent",
            locked_by="host-1",
            locked_at=now - timedelta(minutes=2),
        )
        # No lock at all.
        _insert_scheduled_job(checks_db, name="idle")

        result = checks.check_stuck_jobs(threshold_mins=10)

        names = {row["name"] for row in result}
        assert names == {"stuck"}
        assert result[0]["locked_by"] == "host-1"

    def test_threshold_zero_returns_all_locked(self, checks_db: Session) -> None:
        now = _now_utc()
        _insert_scheduled_job(
            checks_db,
            name="just-now",
            locked_by="host-1",
            locked_at=now - timedelta(seconds=1),
        )
        _insert_scheduled_job(checks_db, name="never-locked")

        result = checks.check_stuck_jobs(threshold_mins=0)

        assert {row["name"] for row in result} == {"just-now"}


class TestCheckOrphanJobs:
    def test_returns_only_rows_without_handler(self, checks_db: Session) -> None:
        from scheduler.api import _registry

        # Seed two rows; register a handler for only one.
        _insert_scheduled_job(checks_db, name="known")
        _insert_scheduled_job(checks_db, name="orphan")

        async def _noop(_session: Session) -> None:
            return None

        _registry["known"] = _noop
        try:
            result = checks.check_orphan_jobs()
        finally:
            _registry.pop("known", None)

        assert {row["name"] for row in result} == {"orphan"}


class TestCheckDeadLetters:
    def test_returns_only_exhausted_raws(self, checks_db: Session) -> None:
        retriable_id = _insert_raw_input(
            checks_db, raw_id="r1", path="_raw/2026/05/01/a.md"
        )
        exhausted_id = _insert_raw_input(
            checks_db, raw_id="r2", path="_raw/2026/05/01/b.md"
        )
        _insert_dead_letter(checks_db, raw_fk=retriable_id, retry_count=1)
        _insert_dead_letter(
            checks_db, raw_fk=exhausted_id, error="kaput", retry_count=3
        )

        result = checks.check_dead_letters()

        assert len(result) == 1
        item = result[0]
        assert item["id"] == exhausted_id
        assert item["path"] == "_raw/2026/05/01/b.md"
        assert item["source"] == "test"
        assert item["error"] == "kaput"
        assert item["retry_count"] == 3
        assert item["last_failed_at"] is not None

    def test_respects_limit(self, checks_db: Session) -> None:
        for i in range(5):
            raw_id = _insert_raw_input(
                checks_db,
                raw_id=f"r-{i}",
                path=f"_raw/2026/05/01/n-{i}.md",
            )
            _insert_dead_letter(checks_db, raw_fk=raw_id, retry_count=3)

        assert len(checks.check_dead_letters(limit=2)) == 2
        assert len(checks.check_dead_letters(limit=10)) == 5

    def test_empty_when_no_dead_letters(self, checks_db: Session) -> None:
        assert checks.check_dead_letters() == []
