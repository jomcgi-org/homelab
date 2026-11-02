"""Cluster leader and home startup hooks call through to their dependencies."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import cluster.module as cluster_module
import home.module as home_module
import pytest


def _fake(monkeypatch, dependency, call_name, result):
    callback = AsyncMock(return_value=result)
    fake = ModuleType(dependency)
    setattr(fake, call_name, callback)
    monkeypatch.setitem(sys.modules, dependency, fake)
    return callback


@pytest.mark.asyncio
async def test_cluster_leader_start_starts_cd_probe_and_pod_restart_watch(
    monkeypatch,
):
    cd_task, watch_task = object(), object()
    start_cd = _fake(monkeypatch, "cluster.cd_leader", "leader_start", [cd_task])
    start_watch = _fake(
        monkeypatch, "cluster.pod_restart_leader", "leader_start", [watch_task]
    )
    app = SimpleNamespace()

    assert await cluster_module.MODULE.leader_start(app) == [cd_task, watch_task]
    start_cd.assert_awaited_once_with(app)
    start_watch.assert_awaited_once_with(app)


@pytest.mark.asyncio
async def test_home_startup_primes_snapshots(monkeypatch):
    prime = _fake(monkeypatch, "home.observability.rollup", "prime_snapshots", None)

    assert await home_module.MODULE.startup(SimpleNamespace()) is None
    prime.assert_awaited_once_with()
