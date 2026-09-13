"""Factory composition, audience isolation, and partial-start recovery."""

import asyncio
import dataclasses
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from factory import module, public_module
from framework import (
    PUBLIC_PROFILE,
    build_app,
    start_leader_singletons,
    stop_leader_singletons,
)


def test_factory_is_the_only_registered_execution_domain():
    from app.modules_private import ALL_MODULES
    from app.modules_public import PUBLIC_MODULES

    names = [m.name for m in ALL_MODULES]
    assert names.count("factory") == 1
    assert "swarm" not in names
    assert "agent_sessions" not in names
    assert [m for m in PUBLIC_MODULES if m.name == "factory"] == [public_module.MODULE]


def test_private_surface_preserves_existing_routes_and_health():
    app = FastAPI()
    module.register(app)
    paths = {c.path for c in iter_route_contexts(app.routes)}
    assert {
        "/api/agents/sessions",
        "/api/agents/factory",
        "/api/swarm/factory",
        "/api/swarm/factory/control",
    } <= paths
    assert set(module.MODULE.register_health_advisory) == {
        "drainer",
        "kg",
        "provider_quota",
    }
    assert set(module.MODULE.register_liveness) == {"factory"}


def test_public_surface_has_no_private_hooks_or_mutations():
    public = public_module.MODULE
    for hook in ("register", "register_mcp", "leader_start", "leader_stop", "shutdown"):
        assert getattr(public, hook) is None
    app = build_app(dataclasses.replace(PUBLIC_PROFILE, otel_enabled=False), [public])
    routes = list(iter_route_contexts(app.routes))
    paths = {c.path for c in routes}
    assert "/api/swarm/factory/control" not in paths
    assert "/api/agents/sessions" not in paths
    api_routes = [c for c in routes if c.path.startswith("/api/agents/")]
    assert api_routes
    for context in api_routes:
        assert context.route.methods <= {"GET", "HEAD"}


@pytest.mark.asyncio
async def test_partial_factory_start_is_tracked_and_stopped(monkeypatch):
    from factory.orchestration import factory_conductor, runtime

    monkeypatch.setitem(
        sys.modules, "factory.orchestration.node_workflows", SimpleNamespace()
    )
    monkeypatch.setattr(runtime, "launch", lambda: None)
    monkeypatch.setattr(runtime, "is_launched", lambda: True)
    shutdowns = []
    monkeypatch.setattr(runtime, "shutdown", lambda: shutdowns.append(True))
    task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(factory_conductor, "start_loop", lambda: [task])

    async def fail_maintenance(app):
        raise RuntimeError("maintenance startup failed")

    monkeypatch.setattr(module, "_start_session_maintenance", fail_maintenance)
    app = SimpleNamespace(state=SimpleNamespace(singleton_tasks=[]))
    try:
        await start_leader_singletons(app, [module.MODULE])
        assert app.state.leader_singleton_failures == {"factory"}
        assert app.state.singleton_tasks == [task]
        await stop_leader_singletons(app, [module.MODULE])
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert shutdowns == [True]
        assert not app.state.leader_singletons_dbos_launched
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_maintenance_failure_keeps_previously_started_tasks_owned(monkeypatch):
    from factory.execution import kg_feed, mcp, titles

    task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(mcp, "start_pending_message_sweep", lambda: [task])
    monkeypatch.setattr(titles, "start_title_refresh_loop", lambda: [])

    def fail_feed():
        raise RuntimeError("feed failed")

    monkeypatch.setattr(kg_feed, "start_kg_feed_loop", fail_feed)
    app = SimpleNamespace(state=SimpleNamespace(singleton_tasks=[]))
    try:
        with pytest.raises(RuntimeError, match="feed failed"):
            await module._start_session_maintenance(app)
        assert app.state.singleton_tasks == [task]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_disabled_orchestration_still_starts_session_maintenance(monkeypatch):
    from factory.orchestration import factory_conductor, runtime

    monkeypatch.setitem(
        sys.modules, "factory.orchestration.node_workflows", SimpleNamespace()
    )
    monkeypatch.setattr(runtime, "launch", lambda: None)
    monkeypatch.setattr(runtime, "is_launched", lambda: False)
    monkeypatch.setattr(
        factory_conductor, "start_loop", lambda: pytest.fail("disabled")
    )
    called = []

    async def maintenance(app):
        called.append(True)
        return []

    monkeypatch.setattr(module, "_start_session_maintenance", maintenance)
    app = SimpleNamespace(state=SimpleNamespace(singleton_tasks=[]))
    assert await module._leader_start(app) == []
    assert called == [True]
    assert not app.state.leader_singletons_dbos_launched


@pytest.mark.asyncio
async def test_quota_probe_is_owned_by_factory_leader(monkeypatch):
    from factory import quota_probe
    from factory.execution import (
        kg_feed,
        mcp,
        permit_supervision,
        result_receipts,
        titles,
    )

    for owner, name in (
        (mcp, "start_pending_message_sweep"),
        (titles, "start_title_refresh_loop"),
        (kg_feed, "start_kg_feed_loop"),
        (permit_supervision, "start_permit_supervision_loop"),
        (result_receipts, "start_receipt_retention_loop"),
    ):
        monkeypatch.setattr(owner, name, lambda: [])
    task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(quota_probe, "start_quota_probe_loop", lambda: [task])
    app = SimpleNamespace(state=SimpleNamespace(singleton_tasks=[]))
    try:
        assert await module._start_session_maintenance(app) == [task]
        assert app.state.singleton_tasks == [task]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
