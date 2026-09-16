"""Tests for the leader-elected pod restart watcher."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from cluster import pod_restart_leader
from cluster.pod_restart_models import PodRestartWatch


def _pod(
    restart_count: int,
    *,
    pod: str = "web-abc",
    container: str = "web",
    reason: str | None = None,
    exit_code: int | None = None,
) -> dict:
    terminated = {}
    if reason is not None:
        terminated["reason"] = reason
    if exit_code is not None:
        terminated["exitCode"] = exit_code
    last_state = {"terminated": terminated} if terminated else {}
    return {
        "metadata": {"name": pod, "namespace": "monolith-public"},
        "status": {
            "containerStatuses": [
                {
                    "name": container,
                    "restartCount": restart_count,
                    "lastState": last_state,
                }
            ]
        },
    }


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'pod-restarts.db'}")
    SQLModel.metadata.create_all(engine, tables=[PodRestartWatch.__table__])
    kubernetes = SimpleNamespace(
        list_resources=AsyncMock(),
        close=AsyncMock(),
    )
    notification = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(pod_restart_leader, "get_engine", lambda: engine)
    monkeypatch.setattr(pod_restart_leader, "KubernetesClient", lambda: kubernetes)
    monkeypatch.setattr(pod_restart_leader, "notify", notification)
    monkeypatch.delenv("POD_RESTART_WATCH_NAMESPACES", raising=False)
    return engine, kubernetes, notification


@pytest.mark.asyncio
async def test_unseen_zero_records_baseline_without_notification(watcher):
    engine, kubernetes, notification = watcher
    kubernetes.list_resources.return_value = [_pod(0)]

    await pod_restart_leader._cycle()

    notification.assert_not_awaited()
    kubernetes.list_resources.assert_awaited_once_with(
        "pods", namespace="monolith-public"
    )
    kubernetes.close.assert_awaited_once()
    with Session(engine) as session:
        row = session.get(PodRestartWatch, ("monolith-public", "web-abc", "web"))
        assert row is not None
        assert row.restart_count == 0


@pytest.mark.asyncio
async def test_unseen_nonzero_notifies_with_termination_details(watcher):
    _, kubernetes, notification = watcher
    kubernetes.list_resources.return_value = [
        _pod(
            1,
            pod="monolith-public-web-5877458788-xgdct",
            reason="OOMKilled",
            exit_code=137,
        )
    ]

    await pod_restart_leader._cycle()

    notification.assert_awaited_once_with(
        "monolith-public/monolith-public-web-5877458788-xgdct container web "
        "restarted (0 -> 1), last termination OOMKilled exit 137",
        level="warn",
    )


@pytest.mark.asyncio
async def test_known_increase_notifies_only_once(watcher):
    engine, kubernetes, notification = watcher
    with Session(engine) as session:
        session.add(
            PodRestartWatch(
                namespace="monolith-public",
                pod="web-abc",
                container="web",
                restart_count=1,
                observed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    kubernetes.list_resources.return_value = [_pod(2)]

    await pod_restart_leader._cycle()
    await pod_restart_leader._cycle()

    notification.assert_awaited_once_with(
        "monolith-public/web-abc container web restarted (1 -> 2)", level="warn"
    )
    with Session(engine) as session:
        row = session.get(PodRestartWatch, ("monolith-public", "web-abc", "web"))
        assert row is not None
        assert row.restart_count == 2


@pytest.mark.asyncio
async def test_absent_pods_are_deleted(watcher):
    """A rollout retires the previous pod's row and baselines the new one."""
    engine, kubernetes, notification = watcher
    with Session(engine) as session:
        session.add(
            PodRestartWatch(
                namespace="monolith-public",
                pod="old-rollout-pod",
                container="web",
                restart_count=0,
                observed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    kubernetes.list_resources.return_value = [_pod(0, pod="new-rollout-pod")]

    await pod_restart_leader._cycle()

    notification.assert_not_awaited()
    with Session(engine) as session:
        keys = {
            (row.namespace, row.pod, row.container)
            for row in session.exec(select(PodRestartWatch)).all()
        }
    assert keys == {("monolith-public", "new-rollout-pod", "web")}


@pytest.mark.asyncio
async def test_empty_listing_keeps_baselines(watcher):
    """A successfully empty listing must not retire the namespace's baselines.

    Dropping them makes every surviving pod look unseen on the next cycle,
    which re-notifies restarts that were already reported.
    """
    engine, kubernetes, notification = watcher
    with Session(engine) as session:
        session.add(
            PodRestartWatch(
                namespace="monolith-public",
                pod="web-abc",
                container="web",
                restart_count=1,
                observed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    kubernetes.list_resources.return_value = []

    await pod_restart_leader._cycle()

    # The baseline survives, so the pod coming back at the same count is quiet.
    kubernetes.list_resources.return_value = [_pod(1)]
    await pod_restart_leader._cycle()

    notification.assert_not_awaited()
    with Session(engine) as session:
        row = session.get(PodRestartWatch, ("monolith-public", "web-abc", "web"))
    assert row is not None
    assert row.restart_count == 1


@pytest.mark.asyncio
async def test_unwatched_namespace_rows_are_retired(watcher, monkeypatch):
    """Narrowing the watch list clears rows the loop no longer covers."""
    engine, kubernetes, notification = watcher
    monkeypatch.setenv("POD_RESTART_WATCH_NAMESPACES", "monolith-public")
    with Session(engine) as session:
        session.add(
            PodRestartWatch(
                namespace="retired-namespace",
                pod="stale",
                container="web",
                restart_count=3,
                observed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    kubernetes.list_resources.return_value = [_pod(0)]

    await pod_restart_leader._cycle()

    notification.assert_not_awaited()
    with Session(engine) as session:
        namespaces = {
            row.namespace for row in session.exec(select(PodRestartWatch)).all()
        }
    assert namespaces == {"monolith-public"}


def test_malformed_interval_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("POD_RESTART_WATCH_INTERVAL_S", "not-a-number")

    with caplog.at_level(logging.WARNING):
        assert pod_restart_leader._interval_s() == 300.0

    assert "POD_RESTART_WATCH_INTERVAL_S='not-a-number' is not a number" in caplog.text


def test_message_without_termination_details_is_well_formed():
    observation = pod_restart_leader._Observation(
        restart_count=1, reason=None, exit_code=None
    )

    assert (
        pod_restart_leader._message(
            ("monolith-public", "web-abc", "web"), 0, observation
        )
        == "monolith-public/web-abc container web restarted (0 -> 1)"
    )
