from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import dbos
import pytest
from dbos._error import DBOSException
from fastapi import HTTPException

from swarm import router as swarm_router
from swarm import runtime


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    monkeypatch.setattr(runtime, "_dbos", None)
    monkeypatch.setattr(runtime, "_launched", False)
    monkeypatch.setattr(runtime, "_read_client", None)
    monkeypatch.setattr(runtime, "_read_client_error", None)
    monkeypatch.setattr(runtime, "_read_client_retry_at", 0.0)
    monkeypatch.setattr(runtime, "_read_client_lock", threading.Lock())
    monkeypatch.setattr(runtime, "_enabled", lambda: True)
    monkeypatch.setenv("DATABASE_URL", "postgresql://dbos.example/monolith")


def test_read_client_passes_exact_dbos_client_kwargs(monkeypatch):
    calls = []
    client = object()

    def build_client(**kwargs):
        calls.append(kwargs)
        return client

    monkeypatch.setattr(dbos, "DBOSClient", build_client)

    assert runtime.read_client() is client
    assert calls == [
        {
            "system_database_url": "postgresql://dbos.example/monolith",
            "dbos_system_schema": "dbos",
            "system_database_pool_size": 2,
        }
    ]
    # DBOSClient 2.29.0 fixes max_overflow=0 and pool_timeout=30 internally.
    # They are not accepted constructor kwargs. Combined with the pool size
    # above, those defaults cap a follower at two concurrent database reads.


def test_read_client_failure_is_cached_until_retry_window(monkeypatch):
    clock = [100.0]
    attempts = []
    client = object()

    def build_client(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise DBOSException("database unavailable")
        return client

    monkeypatch.setattr(dbos, "DBOSClient", build_client)
    monkeypatch.setattr(runtime, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(swarm_router.config, "enabled", lambda: True)
    monkeypatch.setattr(runtime, "is_launched", lambda: False)

    with pytest.raises(HTTPException) as first:
        swarm_router._dbos_read()
    assert first.value.status_code == 503
    assert len(attempts) == 1

    with pytest.raises(HTTPException) as second:
        swarm_router._dbos_read()
    assert second.value.status_code == 503
    assert len(attempts) == 1

    clock[0] += runtime._READ_CLIENT_RETRY_SECONDS
    assert swarm_router._dbos_read() is client
    assert len(attempts) == 2


def test_read_client_constructs_outside_lock_and_destroys_race_loser(monkeypatch):
    started = threading.Barrier(3)
    release = threading.Event()
    created_lock = threading.Lock()
    created = []

    class Client:
        def __init__(self, number):
            self.number = number
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    def build_client(**kwargs):
        with created_lock:
            client = Client(len(created))
            created.append(client)
        started.wait(timeout=5)
        assert release.wait(timeout=5)
        return client

    monkeypatch.setattr(dbos, "DBOSClient", build_client)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(runtime.read_client) for _ in range(2)]
        started.wait(timeout=5)
        acquired = runtime._read_client_lock.acquire(blocking=False)
        if acquired:
            runtime._read_client_lock.release()
        release.set()
        assert acquired, "DBOSClient construction held _read_client_lock"
        results = [future.result(timeout=5) for future in futures]

    assert len(created) == 2
    assert results[0] is results[1]
    assert sum(client.destroyed for client in created) == 1
    assert not results[0].destroyed


def test_dbos_read_maps_dbos_exception_to_503(monkeypatch):
    error = DBOSException("database unavailable")

    monkeypatch.setattr(swarm_router.config, "enabled", lambda: True)
    monkeypatch.setattr(runtime, "is_launched", lambda: False)

    def fail_read_client():
        raise error

    monkeypatch.setattr(runtime, "read_client", fail_read_client)

    with pytest.raises(HTTPException) as raised:
        swarm_router._dbos_read()

    assert raised.value.status_code == 503
    assert raised.value.detail == "Swarm DBOS is temporarily unavailable"
    assert raised.value.__cause__ is error
