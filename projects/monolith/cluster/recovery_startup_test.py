"""Recovery can disable unrelated I/O without disabling leader composition."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import cluster.module as cluster_module
import home.module as home_module
import pytest

from framework import Module, start_leader_singletons, stop_leader_singletons


_HOOKS = [
    (
        cluster_module.MODULE.leader_start,
        "CD_PROBE_ENABLED",
        "cluster.cd_leader",
        "leader_start",
        [],
    ),
    (
        home_module.MODULE.startup,
        "HOME_OBSERVABILITY_PRIME_ENABLED",
        "home.observability.rollup",
        "prime_snapshots",
        None,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("hook,flag,dependency,call_name,disabled_result", _HOOKS)
@pytest.mark.parametrize("value", ["false", "0", "no", " FALSE ", " No "])
async def test_disabled_hook_does_not_import_dependency(
    monkeypatch, hook, flag, dependency, call_name, disabled_result, value
):
    monkeypatch.setenv(flag, value)
    # Importing this dependency raises even if another test loaded it earlier.
    monkeypatch.setitem(sys.modules, dependency, None)

    assert await hook(SimpleNamespace()) == disabled_result


@pytest.mark.asyncio
@pytest.mark.parametrize("hook,flag,dependency,call_name,disabled_result", _HOOKS)
@pytest.mark.parametrize("value", [None, "", "true", "1", "yes"])
async def test_enabled_hook_preserves_call_through(
    monkeypatch, hook, flag, dependency, call_name, disabled_result, value
):
    if value is None:
        monkeypatch.delenv(flag, raising=False)
    else:
        monkeypatch.setenv(flag, value)
    result = [object()] if call_name == "leader_start" else None
    callback = AsyncMock(return_value=result)
    fake = ModuleType(dependency)
    setattr(fake, call_name, callback)
    monkeypatch.setitem(sys.modules, dependency, fake)
    app = SimpleNamespace()

    assert await hook(app) is result
    if call_name == "leader_start":
        callback.assert_awaited_once_with(app)
    else:
        callback.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("hook,flag,dependency,call_name,disabled_result", _HOOKS)
async def test_hook_reads_configuration_on_each_invocation(
    monkeypatch, hook, flag, dependency, call_name, disabled_result
):
    callback = AsyncMock(return_value=disabled_result)
    fake = ModuleType(dependency)
    setattr(fake, call_name, callback)
    monkeypatch.setitem(sys.modules, dependency, fake)
    app = SimpleNamespace()

    monkeypatch.setenv(flag, "false")
    await hook(app)
    callback.assert_not_awaited()
    monkeypatch.setenv(flag, "true")
    await hook(app)
    callback.assert_awaited_once()
    monkeypatch.setenv(flag, "false")
    await hook(app)
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_cd_preserves_other_leader_start_and_stop(monkeypatch):
    monkeypatch.setenv("CD_PROBE_ENABLED", "false")
    monkeypatch.setitem(sys.modules, "cluster.cd_leader", None)
    app = SimpleNamespace(state=SimpleNamespace())
    started = asyncio.Event()
    released = asyncio.Event()
    tasks = []

    async def run_other():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    async def start_other(app):
        task = asyncio.create_task(run_other())
        tasks.append(task)
        return [task]

    stop_other = AsyncMock()
    other = Module(name="other", leader_start=start_other, leader_stop=stop_other)
    modules = [cluster_module.MODULE, other]
    try:
        await start_leader_singletons(app, modules)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert app.state.singleton_tasks == tasks
        assert len(tasks) == 1
        assert app.state.leader_singleton_failures == set()
        await stop_leader_singletons(app, modules)
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_other.assert_awaited_once_with(app)
        assert released.is_set()
        assert tasks[0].cancelled()
        assert app.state.singleton_tasks == []
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
