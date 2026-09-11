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


def test_init_dbos_pins_the_application_version_to_the_node_workflow(monkeypatch):
    configs = []

    monkeypatch.setattr(dbos, "DBOS", lambda config: configs.append(config) or object())
    monkeypatch.setattr(runtime, "node_workflow_version", lambda: "pinned-version")

    assert runtime.init_dbos() is not None
    assert configs[0]["application_version"] == "pinned-version"


@pytest.fixture
def dbos_registry():
    """The global DBOS registry, restored after the test that mutates it.

    The node workflow modules are imported before the snapshot is taken on
    purpose. They register their workflow and steps at import, and a snapshot
    taken first would be restored over those registrations at teardown,
    leaving a later test looking at an empty registry.
    """
    from dbos._dbos import _get_or_create_dbos_registry

    from swarm import node_workflows, steps  # noqa: F401 - register first

    registry = _get_or_create_dbos_registry()
    workflows = dict(registry.workflow_info_map)
    types = dict(registry.function_type_map)
    try:
        yield registry
    finally:
        registry.workflow_info_map.clear()
        registry.workflow_info_map.update(workflows)
        registry.function_type_map.clear()
        registry.function_type_map.update(types)


def test_the_node_workflow_version_ignores_an_unrelated_workflow(dbos_registry):
    """A workflow this lane does not run must not move the version.

    This is the whole point of pinning it. DBOS hashes every registered
    workflow, so before this the drainer or the legacy swarm engine changing
    would strand in-flight factory nodes. Both shapes of unrelated change are
    covered: a workflow added, and a workflow already registered whose body is
    edited, which is the case that actually caused the incident.
    """
    ours = runtime.node_workflow_version()
    theirs = dbos_registry.compute_app_version()

    @dbos.DBOS.workflow()
    def unrelated_fixture_workflow() -> int:
        return 1

    assert dbos_registry.compute_app_version() != theirs
    assert runtime.node_workflow_version() == ours

    # Now edit that registered workflow's source the way a deploy would. DBOS
    # reads it with inspect.getsource, so a different function under the same
    # registration is exactly what it sees after an edit.
    theirs = dbos_registry.compute_app_version()
    name = next(
        key
        for key, value in dbos_registry.workflow_info_map.items()
        if value is unrelated_fixture_workflow
    )

    @dbos.DBOS.workflow()
    def unrelated_fixture_workflow() -> int:  # noqa: F811 - the edited body
        return 2

    dbos_registry.workflow_info_map[name] = unrelated_fixture_workflow
    assert dbos_registry.compute_app_version() != theirs
    assert runtime.node_workflow_version() == ours


def test_every_node_workflow_step_is_in_the_hashed_member_list():
    """The member list is hand-maintained, so this is its guard.

    A step added to the node workflow without being added here would not move
    the application version, and DBOS would then recover in-flight nodes onto a
    workflow whose recorded step sequence no longer matches.
    """
    from dbos._dbos import _get_or_create_dbos_registry

    from swarm import node_workflows, steps

    # DBOS registers a bare step under "<temp>.<name>", so the map is read by
    # the suffix rather than by the attribute alone.
    steps_registered = {
        name.rsplit(".", 1)[-1]
        for name, kind in _get_or_create_dbos_registry().function_type_map.items()
        if kind == "step"
    }
    members = set(runtime._node_workflow_members())
    declared = {
        value
        for value in vars(node_workflows).values()
        if callable(value)
        and getattr(value, "__module__", None) == node_workflows.__name__
        and getattr(value, "dbos_function_name", None) in steps_registered
    }
    assert declared, "found no registered steps, so this guard proves nothing"
    missing = {member.__name__ for member in declared - members}
    assert not missing, f"steps missing from _node_workflow_members: {missing}"

    imported = {
        name
        for name, value in vars(node_workflows).items()
        if getattr(value, "__module__", None) == steps.__name__
    }
    named = {member.__name__ for member in members}
    assert imported <= named, f"swarm.steps names not hashed: {imported - named}"


def test_the_node_workflow_version_changes_when_a_step_body_changes(monkeypatch):
    from swarm import steps

    before = runtime.node_workflow_version()

    def poll_turn(session_id: int, after_seq: int) -> dict | None:
        """A different body is a different durable shape."""
        return None

    monkeypatch.setattr(steps, "poll_turn", poll_turn)
    assert runtime.node_workflow_version() != before


def test_an_unreadable_source_fails_launch_rather_than_falling_back(monkeypatch):
    """Falling back to the DBOS-computed version is not the neutral choice.

    That version differs from the pinned one every running node started under,
    so the reconciler would read them all as stranded and cancel the lot, once
    on the deploy that could not read its source and again on the one that
    fixes it.
    """
    # len is a builtin, so inspect.getsource raises for it.
    monkeypatch.setattr(runtime, "_node_workflow_members", lambda: (len,))
    with pytest.raises(RuntimeError, match="cannot be pinned"):
        runtime.node_workflow_version()
