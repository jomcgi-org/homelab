"""BDD tests for ``agent.locks`` against a real PostgreSQL.

The locks module relies on ``ON CONFLICT ... DO UPDATE WHERE`` and
``gen_random_uuid()``; both are PG-specific and can't be faithfully
mocked or translated to SQLite without testing rewritten code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session

from agent import locks


def _force_expire(session: Session, key: str) -> None:
    """Push an existing lock's expiry into the past so it looks expired."""
    session.execute(
        text(
            "UPDATE claude_agent.agent_locks SET expires_at = now() - interval '1 second' WHERE key = :key"
        ),
        {"key": key},
    )
    session.commit()


class TestAcquire:
    def test_fresh_key_acquires(self, agent_db: Session):
        result = locks.acquire("k1", "holder-a", ttl_secs=60)

        assert result.acquired is True
        assert result.lock_id is not None
        assert result.expires_at is not None
        assert result.expires_at > datetime.now(timezone.utc)

    def test_live_lock_refuses_other_holder(self, agent_db: Session):
        first = locks.acquire("k2", "holder-a", ttl_secs=60)
        assert first.acquired is True

        second = locks.acquire("k2", "holder-b", ttl_secs=60)

        assert second.acquired is False
        assert second.lock_id is None
        assert second.expires_at is None

    def test_expired_lock_can_be_stolen(self, agent_db: Session):
        first = locks.acquire("k3", "holder-a", ttl_secs=60)
        assert first.acquired is True
        _force_expire(agent_db, "k3")

        second = locks.acquire("k3", "holder-b", ttl_secs=60)

        assert second.acquired is True
        assert second.lock_id is not None
        assert second.lock_id != first.lock_id


class TestExtend:
    def test_extend_pushes_expiry(self, agent_db: Session):
        first = locks.acquire("k4", "holder-a", ttl_secs=10)
        assert first.acquired is True
        original_expiry = first.expires_at
        assert original_expiry is not None

        new_expiry = locks.extend(first.lock_id, ttl_secs=600)

        assert new_expiry is not None
        assert new_expiry > original_expiry + timedelta(seconds=300)

    def test_extend_unknown_lock_returns_none(self, agent_db: Session):
        assert locks.extend(uuid4(), ttl_secs=60) is None


class TestRelease:
    def test_release_deletes_then_idempotent(self, agent_db: Session):
        first = locks.acquire("k5", "holder-a", ttl_secs=60)
        assert first.acquired is True

        assert locks.release(first.lock_id) is True
        assert locks.release(first.lock_id) is False


class TestListActive:
    def test_only_unexpired_with_prefix_filter(self, agent_db: Session):
        # Two live locks, one expired; one matches the prefix, one doesn't.
        live_match = locks.acquire("foo:1", "holder-a", ttl_secs=60)
        live_other = locks.acquire("bar:1", "holder-b", ttl_secs=60)
        expired = locks.acquire("foo:2", "holder-c", ttl_secs=60)
        assert live_match.acquired and live_other.acquired and expired.acquired
        _force_expire(agent_db, "foo:2")

        all_active = locks.list_active()
        keys_all = {row["key"] for row in all_active}
        assert keys_all == {"foo:1", "bar:1"}

        foo_only = locks.list_active(prefix="foo:")
        keys_foo = [row["key"] for row in foo_only]
        assert keys_foo == ["foo:1"]
