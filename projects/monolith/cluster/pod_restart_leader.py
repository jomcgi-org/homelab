"""Private-tier watcher for Kubernetes container restarts.

Leader-elected so exactly one replica compares restart counts and enqueues
notifications. The table is a last-observation-wins latch for live containers,
not an audit log: rows disappear when their pod or container disappears.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from cluster.kubernetes import KubernetesClient
from cluster.pod_restart_models import PodRestartWatch
from core.db import get_engine
from framework import log_task_exception, register_leader_tasks
from shared.notify import notify

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 300.0
_DEFAULT_NAMESPACES = ("monolith-public",)

_Key = tuple[str, str, str]


@dataclass(frozen=True)
class _Observation:
    restart_count: int
    reason: str | None
    exit_code: int | None


def _interval_s() -> float:
    raw = os.environ.get("POD_RESTART_WATCH_INTERVAL_S", "")
    try:
        return float(raw) if raw else _DEFAULT_INTERVAL_S
    except ValueError:
        logger.warning(
            "pod restart watch: POD_RESTART_WATCH_INTERVAL_S=%r is not a number",
            raw,
        )
        return _DEFAULT_INTERVAL_S


def _namespaces() -> tuple[str, ...]:
    raw = os.environ.get("POD_RESTART_WATCH_NAMESPACES", "")
    namespaces = tuple(part.strip() for part in raw.split(",") if part.strip())
    return namespaces or _DEFAULT_NAMESPACES


def _observations(namespace: str, pods: list[dict]) -> dict[_Key, _Observation]:
    result = {}
    for pod in pods:
        metadata = pod.get("metadata") or {}
        pod_name = metadata.get("name")
        pod_namespace = metadata.get("namespace") or namespace
        if not pod_name:
            continue
        status = pod.get("status") or {}
        for container_status in status.get("containerStatuses") or []:
            container = container_status.get("name")
            if not container:
                continue
            terminated = (container_status.get("lastState") or {}).get(
                "terminated"
            ) or {}
            result[(pod_namespace, pod_name, container)] = _Observation(
                restart_count=int(container_status.get("restartCount") or 0),
                reason=terminated.get("reason"),
                exit_code=terminated.get("exitCode"),
            )
    return result


def _read_counts_sync() -> dict[_Key, int]:
    with Session(get_engine()) as session:
        rows = session.exec(select(PodRestartWatch)).all()
        return {
            (row.namespace, row.pod, row.container): row.restart_count for row in rows
        }


def _record_sync(
    observations: dict[_Key, _Observation],
    observed_namespaces: set[str],
    watched_namespaces: set[str],
) -> None:
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        rows = session.exec(select(PodRestartWatch)).all()
        existing = {(row.namespace, row.pod, row.container): row for row in rows}

        for key, row in existing.items():
            if key in observations:
                continue
            # Retire a row only once its namespace has actually been seen this
            # cycle, or has left the watch list entirely. A successfully empty
            # listing is far more often a transient read than a genuinely
            # empty namespace, and dropping the baselines makes every
            # surviving pod look new, which re-notifies restarts already
            # reported. A failed listing raises and never reaches this write.
            if row.namespace in observed_namespaces or (
                row.namespace not in watched_namespaces
            ):
                session.delete(row)

        for key, observation in observations.items():
            row = existing.get(key)
            if row is None:
                row = PodRestartWatch(
                    namespace=key[0],
                    pod=key[1],
                    container=key[2],
                    restart_count=observation.restart_count,
                    last_reason=observation.reason,
                    observed_at=now,
                )
                session.add(row)
            else:
                row.restart_count = observation.restart_count
                row.last_reason = observation.reason
                row.observed_at = now
        session.commit()


def _message(key: _Key, old_count: int, observation: _Observation) -> str:
    namespace, pod, container = key
    message = (
        f"{namespace}/{pod} container {container} restarted "
        f"({old_count} -> {observation.restart_count})"
    )
    termination = []
    if observation.reason:
        termination.append(observation.reason)
    if observation.exit_code is not None:
        termination.append(f"exit {observation.exit_code}")
    if termination:
        message += f", last termination {' '.join(termination)}"
    return message


async def _cycle() -> None:
    kubernetes = KubernetesClient()
    current: dict[_Key, _Observation] = {}
    watched = _namespaces()
    observed_namespaces: set[str] = set()
    try:
        for namespace in watched:
            pods = await kubernetes.list_resources("pods", namespace=namespace)
            if pods:
                observed_namespaces.add(namespace)
            current.update(_observations(namespace, pods))
    finally:
        await kubernetes.close()

    previous = await asyncio.to_thread(_read_counts_sync)
    for key, observation in current.items():
        old_count = previous.get(key, 0)
        if observation.restart_count > old_count:
            await notify(_message(key, old_count, observation), level="warn")

    # Record only after every notification has been queued. If enqueueing fails,
    # the next cycle retries rather than silently losing the restart signal.
    await asyncio.to_thread(_record_sync, current, observed_namespaces, set(watched))


async def _loop() -> None:
    interval = _interval_s()
    while True:
        try:  # nosemgrep: no-broad-except-swallow - a dead loop is worse, logged here
            await _cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one failed API read, database write, or notification
            # kill the watcher and leave future restarts invisible.
            logger.exception("pod restart watch cycle failed")
        await asyncio.sleep(interval)


async def leader_start(app) -> list[asyncio.Task]:
    task = asyncio.create_task(_loop(), name="pod-restart-watch")
    task.add_done_callback(log_task_exception)
    register_leader_tasks(app, [task])
    return [task]
