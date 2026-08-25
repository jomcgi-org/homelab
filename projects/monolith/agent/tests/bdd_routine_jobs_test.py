"""BDD tests for ``agent.routine_jobs`` against a real PostgreSQL.

The claim path uses ``SELECT FOR UPDATE SKIP LOCKED`` and the schema relies
on JSONB / schema-qualified tables — neither translates to SQLite cleanly,
so all tests run against the real test Postgres provided by ``agent_db``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from agent import routine_jobs


def _force_lock(
    session: Session, name: str, holder: str, locked_at: datetime, ttl_secs: int
) -> None:
    session.execute(
        text(
            """
            UPDATE claude_agent.routine_jobs
               SET locked_by = :holder,
                   locked_at = :locked_at,
                   ttl_secs = :ttl
             WHERE name = :name
            """
        ),
        {
            "name": name,
            "holder": holder,
            "locked_at": locked_at,
            "ttl": ttl_secs,
        },
    )
    session.commit()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class TestRegisterAndList:
    def test_register_then_list_returns_row(self, agent_db: Session):
        ok = routine_jobs.register_job(
            name="job-a",
            kind="check",
            interval_secs=300,
            payload={"hello": "world"},
            next_run_at=_now_utc(),
            created_by="test",
        )
        assert ok is True

        rows = routine_jobs.list_jobs()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "job-a"
        assert row["routine_kind"] == "check"
        assert row["interval_secs"] == 300
        assert row["payload"] == {"hello": "world"}
        assert row["created_by"] == "test"

    def test_register_duplicate_raises(self, agent_db: Session):
        routine_jobs.register_job(name="dup", kind="check")
        with pytest.raises(IntegrityError):
            routine_jobs.register_job(name="dup", kind="check")


class TestClaimByName:
    def test_specific_claim_then_second_returns_none(self, agent_db: Session):
        routine_jobs.register_job(
            name="targeted",
            kind="check",
            payload={"prompt": "check the queue", "reasoning": True},
            next_run_at=_now_utc(),
        )

        first = routine_jobs.claim_job(holder="r1", ttl_secs=60, name="targeted")
        assert first is not None
        assert first["name"] == "targeted"
        assert first["locked_by"] == "r1"
        assert first["ttl_secs"] == 60
        assert first["payload"] == {
            "prompt": "check the queue",
            "reasoning": True,
        }

        second = routine_jobs.claim_job(holder="r2", ttl_secs=60, name="targeted")
        assert second is None

        assert routine_jobs.complete_job(
            name="targeted", status="ok", summary="drained"
        )
        completed = next(
            row for row in routine_jobs.list_jobs() if row["name"] == "targeted"
        )
        assert completed["last_status"] == "ok"
        assert completed["last_summary"] == "drained"
        assert completed["locked_by"] is None


class TestClaimNextDue:
    def test_filters_by_kind(self, agent_db: Session):
        now = _now_utc()
        # Two due rows of different kinds; we filter to kind="check".
        routine_jobs.register_job(
            name="reg-1", kind="register", next_run_at=now - timedelta(minutes=5)
        )
        routine_jobs.register_job(
            name="check-1", kind="check", next_run_at=now - timedelta(minutes=3)
        )
        routine_jobs.register_job(
            name="check-2", kind="check", next_run_at=now - timedelta(minutes=1)
        )

        claimed = routine_jobs.claim_job(holder="r1", ttl_secs=60, kind="check")
        assert claimed is not None
        # ORDER BY next_run_at ASC: check-1 (older) wins over check-2.
        assert claimed["name"] == "check-1"
        assert claimed["routine_kind"] == "check"

    def test_skips_live_locks_claims_expired(self, agent_db: Session):
        now = _now_utc()
        routine_jobs.register_job(
            name="live", kind="check", next_run_at=now - timedelta(minutes=10)
        )
        routine_jobs.register_job(
            name="expired", kind="check", next_run_at=now - timedelta(minutes=10)
        )

        # `live` has a fresh 60s lock; `expired` has a 1s lock taken 10m ago.
        _force_lock(
            agent_db,
            "live",
            holder="other",
            locked_at=now - timedelta(seconds=5),
            ttl_secs=60,
        )
        _force_lock(
            agent_db,
            "expired",
            holder="other",
            locked_at=now - timedelta(minutes=10),
            ttl_secs=1,
        )

        claimed = routine_jobs.claim_job(holder="r1", ttl_secs=60, kind="check")
        assert claimed is not None
        assert claimed["name"] == "expired"
        assert claimed["locked_by"] == "r1"


class TestComplete:
    def test_repeating_advances_next_run_at(self, agent_db: Session):
        now = _now_utc()
        routine_jobs.register_job(
            name="repeat",
            kind="check",
            interval_secs=600,
            next_run_at=now,
        )
        claimed = routine_jobs.claim_job(holder="r1", ttl_secs=60, name="repeat")
        assert claimed is not None
        original_next = claimed["next_run_at"]

        ok = routine_jobs.complete_job(name="repeat", status="ok", summary="done")
        assert ok is True

        rows = routine_jobs.list_jobs()
        row = next(r for r in rows if r["name"] == "repeat")
        assert row["locked_by"] is None
        assert row["locked_at"] is None
        assert row["last_status"] == "ok"
        assert row["last_summary"] == "done"
        assert row["next_run_at"] is not None
        # interval_secs=600 means next_run_at should be ~10min after original.
        assert row["next_run_at"] > original_next + timedelta(seconds=300)

    def test_oneoff_leaves_next_run_at_unchanged(self, agent_db: Session):
        now = _now_utc()
        routine_jobs.register_job(
            name="oneoff",
            kind="register",
            interval_secs=None,
            next_run_at=now,
        )
        claimed = routine_jobs.claim_job(holder="r1", ttl_secs=60, name="oneoff")
        assert claimed is not None
        original_next = claimed["next_run_at"]

        ok = routine_jobs.complete_job(name="oneoff", status="ok")
        assert ok is True

        rows = routine_jobs.list_jobs()
        row = next(r for r in rows if r["name"] == "oneoff")
        assert row["interval_secs"] is None
        assert row["next_run_at"] == original_next
        assert row["locked_by"] is None


class TestTrigger:
    def test_trigger_sets_next_run_at_to_now(self, agent_db: Session):
        future = _now_utc() + timedelta(hours=2)
        routine_jobs.register_job(name="future", kind="check", next_run_at=future)

        ok = routine_jobs.trigger_job("future")
        assert ok is True

        rows = routine_jobs.list_jobs()
        row = next(r for r in rows if r["name"] == "future")
        # next_run_at should now be roughly current time, well below the original future.
        assert row["next_run_at"] < future - timedelta(hours=1)
        # And due (now or earlier).
        assert row["next_run_at"] <= _now_utc() + timedelta(seconds=5)


class TestDeregister:
    def test_deregister_removes_row(self, agent_db: Session):
        routine_jobs.register_job(name="goner", kind="check")
        assert any(r["name"] == "goner" for r in routine_jobs.list_jobs())

        ok = routine_jobs.deregister_job("goner")
        assert ok is True
        assert not any(r["name"] == "goner" for r in routine_jobs.list_jobs())
