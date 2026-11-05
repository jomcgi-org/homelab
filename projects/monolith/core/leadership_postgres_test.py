"""Lease ownership against the migrated PostgreSQL fixture, without SAVEPOINTs."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlmodel import create_engine

from core import leadership


@pytest.fixture
def leases(pg):
    identity = uuid4().hex
    keys = []
    engines = []

    def engine_for(actor):
        engine = create_engine(
            pg.url,
            poolclass=NullPool,
            connect_args={
                "application_name": f"lease-{identity}-{actor}",
                "connect_timeout": 2,
                "options": (
                    "-c lock_timeout=2000 -c statement_timeout=3000 "
                    "-c idle_in_transaction_session_timeout=4000"
                ),
            },
        )
        engines.append(engine)
        return engine

    def key():
        value = f"test-{identity}-{len(keys)}"
        keys.append(value)
        return value

    observer = engine_for("observer")
    try:
        yield SimpleNamespace(
            key=key,
            observer=observer,
            engine_for=engine_for,
            application_name=lambda actor: f"lease-{identity}-{actor}",
        )
    finally:
        try:
            with observer.begin() as connection:
                for value in keys:
                    connection.execute(
                        text(
                            "DELETE FROM scheduler.leader_lease WHERE lease_key = :key"
                        ),
                        {"key": value},
                    )
        finally:
            for engine in engines:
                engine.dispose()


def _row(leases, key):
    # Each observation uses a new committed connection, including helper checks.
    with leases.observer.begin() as connection:
        return connection.execute(
            text("SELECT * FROM scheduler.leader_lease WHERE lease_key = :key"),
            {"key": key},
        ).one_or_none()


def _acquire(connection, key, holder):
    return connection.execute(
        text(leadership._ACQUIRE_SQL),
        {"key": key, "holder": holder, "ttl": leadership.LEASE_TTL},
    ).scalar_one_or_none()


def _committed_acquire(engine, key, holder):
    with engine.begin() as connection:
        return _acquire(connection, key, holder)


def _release(leases, key, holder):
    with leases.observer.begin() as connection:
        return connection.execute(
            text(leadership._RELEASE_SQL), {"key": key, "holder": holder}
        ).rowcount


def _set_age(leases, key, seconds):
    with leases.observer.begin() as connection:
        connection.execute(
            text("""
                UPDATE scheduler.leader_lease
                   SET heartbeat_at = now() - make_interval(secs => :age)
                 WHERE lease_key = :key
            """),
            {"key": key, "age": seconds},
        )
    return _row(leases, key)


def _wait_for_lock(leases, blocker):
    deadline = monotonic() + 1.5
    poll = Event()
    while monotonic() < deadline:
        with leases.observer.begin() as connection:
            waiter = connection.execute(
                text("""
                    SELECT pid FROM pg_stat_activity
                     WHERE application_name = :actor
                       AND wait_event_type = 'Lock'
                       AND :blocker = ANY(pg_blocking_pids(pid))
                """),
                {"actor": leases.application_name("second"), "blocker": blocker},
            ).scalar_one_or_none()
        if waiter is not None:
            assert waiter != blocker
            return True
        # Poll an observed lock condition; this does not simulate lease expiry.
        poll.wait(0.01)
    return False


@pytest.mark.parametrize("stale", [False, True], ids=["absent", "stale"])
def test_contenders_have_one_committed_winner(leases, stale):
    key = leases.key()
    first_engine = leases.engine_for("first")
    second_engine = leases.engine_for("second")
    assert 4 < leadership.LEASE_TTL
    if stale:
        assert _committed_acquire(leases.observer, key, "old") == "old"
        _set_age(leases, key, leadership.LEASE_TTL + 60)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with first_engine.connect() as first:
            transaction = first.begin()
            try:
                blocker = first.execute(text("SELECT pg_backend_pid()")).scalar_one()
                winner = _acquire(first, key, "first-holder")
                second = pool.submit(
                    _committed_acquire, second_engine, key, "second-holder"
                )
                try:
                    blocked = _wait_for_lock(leases, blocker)
                finally:
                    # Release the first transaction even if observing the wait fails.
                    transaction.commit()
                loser = second.result(timeout=4)
            finally:
                if transaction.is_active:
                    transaction.rollback()
        assert blocked, "second contender never waited on the first PostgreSQL backend"
        assert [winner, loser] == ["first-holder", None]
    persisted = _row(leases, key)
    assert persisted.lease_key == key
    assert persisted.holder == winner


def test_fresh_foreign_holder_is_denied_without_changes(leases):
    key = leases.key()
    assert _committed_acquire(leases.observer, key, "owner") == "owner"
    before = _row(leases, key)
    assert _committed_acquire(leases.observer, key, "foreign") is None
    assert _row(leases, key) == before


def test_fresh_owner_renews_heartbeat(leases):
    key = leases.key()
    assert _committed_acquire(leases.observer, key, "owner") == "owner"
    before = _set_age(leases, key, 1)
    assert _committed_acquire(leases.observer, key, "owner") == "owner"
    after = _row(leases, key)
    assert after.holder == before.holder
    assert after.heartbeat_at > before.heartbeat_at


def test_takeover_protects_successor_from_old_release(leases):
    key = leases.key()
    assert _committed_acquire(leases.observer, key, "old") == "old"
    before = _set_age(leases, key, leadership.LEASE_TTL + 60)
    assert _committed_acquire(leases.observer, key, "successor") == "successor"
    successor = _row(leases, key)
    assert successor.holder == "successor"
    assert successor.heartbeat_at > before.heartbeat_at
    assert _release(leases, key, "old") == 0
    assert _row(leases, key) == successor
    assert _release(leases, key, "successor") == 1
    assert _row(leases, key) is None


def test_keys_acquire_and_release_independently(leases):
    first, second = leases.key(), leases.key()
    assert _committed_acquire(leases.observer, first, "first") == "first"
    assert _committed_acquire(leases.observer, second, "second") == "second"
    before = _row(leases, second)
    assert _release(leases, first, "first") == 1
    assert _row(leases, first) is None
    assert _row(leases, second) == before


def test_helpers_commit_ownership_and_release(leases, monkeypatch):
    key = leases.key()
    engine = leases.engine_for("helper")
    # Identities are sequential and pytest restores both overrides on failure.
    monkeypatch.setattr(leadership, "get_engine", lambda: engine)
    monkeypatch.setattr(leadership, "HOLDER_ID", "old")
    assert leadership._acquire_or_renew(key) is True
    assert _row(leases, key).holder == "old"
    before = _set_age(leases, key, 1)
    assert leadership._acquire_or_renew(key) is True
    renewed = _row(leases, key)
    assert renewed.holder == "old"
    assert renewed.heartbeat_at > before.heartbeat_at

    monkeypatch.setattr(leadership, "HOLDER_ID", "successor")
    assert leadership._acquire_or_renew(key) is False
    assert _row(leases, key) == renewed
    _set_age(leases, key, leadership.LEASE_TTL + 60)
    assert leadership._acquire_or_renew(key) is True
    successor = _row(leases, key)
    assert successor.holder == "successor"
    monkeypatch.setattr(leadership, "HOLDER_ID", "old")
    leadership._release(key)
    assert _row(leases, key) == successor
    monkeypatch.setattr(leadership, "HOLDER_ID", "successor")
    leadership._release(key)
    assert _row(leases, key) is None
