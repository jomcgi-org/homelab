"""BDD tests for the concurrent scheduler dispatcher.

Uses a real PostgreSQL instance because ``FOR UPDATE SKIP LOCKED`` and the
advisory-lock semantics are PG-specific and cannot be faithfully mocked.

The key concurrency oracle is wall-clock time: if N jobs each sleep S seconds
and the dispatcher finishes in ~S seconds (not ~N*S), they ran in parallel.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from scheduler.api import (
    ScheduledJob,
    dispatch_due_jobs,
    register_job,
)


def _due_now(session: Session, name: str) -> None:
    """Move a job's next_run_at into the past so it's immediately due."""
    job = session.get(ScheduledJob, name)
    assert job is not None, f"Job {name} was not registered"
    job.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.add(job)
    session.commit()


class TestConcurrentDispatch:
    async def test_due_jobs_run_in_parallel(self, scheduler_db: Session):
        """Three jobs each sleeping 0.4s should finish in <1s when dispatched
        with max_concurrent=3 — proving they ran concurrently, not serially."""
        completed: list[str] = []

        def _make_handler(name: str):
            async def handler(_: Session) -> None:
                await asyncio.sleep(0.4)
                completed.append(name)

            return handler

        for name in ("job-a", "job-b", "job-c"):
            register_job(
                scheduler_db,
                name=name,
                interval_secs=3600,
                handler=lambda s, _name=name: _make_handler(_name)(s),
            )
            _due_now(scheduler_db, name)

        start = time.monotonic()
        count = await dispatch_due_jobs(max_concurrent=3)
        elapsed = time.monotonic() - start

        assert count == 3, f"Expected 3 jobs dispatched, got {count}"
        assert set(completed) == {"job-a", "job-b", "job-c"}
        # Serial would be ~1.2s (3 x 0.4s). Parallel is ~0.4s.
        # Generous ceiling of 0.9s to tolerate CI jitter but still catch serial runs.
        assert elapsed < 0.9, (
            f"Jobs ran serially (elapsed={elapsed:.2f}s); expected ~0.4s for parallel"
        )

    async def test_bounded_concurrency(self, scheduler_db: Session):
        """With max_concurrent=2 and 4 due jobs, total time is ~2 waves of 0.3s,
        not 1 wave (too permissive) or 4 waves (not concurrent at all)."""

        def _make_handler():
            async def handler(_: Session) -> None:
                await asyncio.sleep(0.3)

            return handler

        for name in ("b-1", "b-2", "b-3", "b-4"):
            register_job(
                scheduler_db,
                name=name,
                interval_secs=3600,
                handler=lambda s, _h=_make_handler(): _h(s),
            )
            _due_now(scheduler_db, name)

        start = time.monotonic()
        count = await dispatch_due_jobs(max_concurrent=2)
        elapsed = time.monotonic() - start

        assert count == 4
        # 2 waves of 0.3s = 0.6s. Allow [0.5s, 1.1s]:
        # < 0.5s means we exceeded max_concurrent=2 (would be ~0.3s for full parallel).
        # > 1.1s means we ran serially or slower than expected.
        assert 0.5 < elapsed < 1.1, (
            f"elapsed={elapsed:.2f}s; expected ~0.6s for 2 waves of 2-at-a-time"
        )

    async def test_failed_job_does_not_block_others(self, scheduler_db: Session):
        """When one job raises, the other still runs and is marked ok; the
        failing one gets last_status='error: ...' and its next_run_at advances."""

        async def good_handler(_: Session) -> None:
            await asyncio.sleep(0.1)

        async def bad_handler(_: Session) -> None:
            raise RuntimeError("boom")

        register_job(scheduler_db, name="good", interval_secs=120, handler=good_handler)
        register_job(scheduler_db, name="bad", interval_secs=120, handler=bad_handler)
        _due_now(scheduler_db, "good")
        _due_now(scheduler_db, "bad")

        count = await dispatch_due_jobs(max_concurrent=2)
        assert count == 2

        scheduler_db.expire_all()
        good = scheduler_db.get(ScheduledJob, "good")
        bad = scheduler_db.get(ScheduledJob, "bad")
        assert good is not None and bad is not None
        assert good.last_status == "ok"
        assert bad.last_status is not None
        assert bad.last_status.startswith("error: boom")
        # Both locks released
        assert good.locked_by is None
        assert bad.locked_by is None
        # Both rescheduled into the future
        now = datetime.now(timezone.utc)
        good_next = (
            good.next_run_at.replace(tzinfo=timezone.utc)
            if good.next_run_at.tzinfo is None
            else good.next_run_at
        )
        bad_next = (
            bad.next_run_at.replace(tzinfo=timezone.utc)
            if bad.next_run_at.tzinfo is None
            else bad.next_run_at
        )
        assert good_next > now
        assert bad_next > now

    async def test_no_double_claim_under_concurrent_dispatch(
        self, scheduler_db: Session
    ):
        """Two dispatcher coroutines racing on the same rows must not cause
        any job to run twice (FOR UPDATE SKIP LOCKED contract)."""
        runs: list[str] = []

        def _make_handler(name: str):
            async def handler(_: Session) -> None:
                await asyncio.sleep(0.05)
                runs.append(name)

            return handler

        names = [f"race-{i}" for i in range(6)]
        for name in names:
            register_job(
                scheduler_db,
                name=name,
                interval_secs=3600,
                handler=lambda s, _name=name: _make_handler(_name)(s),
            )
            _due_now(scheduler_db, name)

        # Simulate two pods dispatching simultaneously.
        results = await asyncio.gather(
            dispatch_due_jobs(max_concurrent=3),
            dispatch_due_jobs(max_concurrent=3),
        )

        assert sum(results) == len(names), (
            f"Expected exactly {len(names)} total dispatches, got {sum(results)}"
        )
        assert sorted(runs) == sorted(names), "No job ran twice"

    async def test_returns_zero_when_no_jobs_due(self, scheduler_db: Session):
        """When no jobs are due, dispatch_due_jobs returns 0 without blocking."""
        scheduler_db.add(
            ScheduledJob(
                name="future",
                interval_secs=60,
                next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        scheduler_db.commit()

        count = await dispatch_due_jobs(max_concurrent=5)
        assert count == 0

        scheduler_db.expire_all()
        future = scheduler_db.get(ScheduledJob, "future")
        assert future is not None
        assert future.last_run_at is None
        assert future.locked_by is None


@pytest.fixture(autouse=True)
def _autouse_scheduler_db(scheduler_db):
    """Make scheduler_db always active so tests don't need to request it
    explicitly — cleanup is wired via its teardown."""
    yield
