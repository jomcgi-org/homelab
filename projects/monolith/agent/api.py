"""Agent domain public API: the only surface other domains may import.

Other domains must import from ``agent.api`` (enforced by
``import_boundaries_test``), never from ``agent`` internals such as
``agent.notify``.
"""

from __future__ import annotations

from agent import config as _config
from agent import routine_jobs as _routine_jobs
from agent.config import agent_sessions_channel_id, agent_sessions_channel_notify
from agent.notify import notify  # re-exported


def load_drainer_settings():
    return _config.load_drainer_settings()


def list_jobs(
    *,
    kind: str | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict]:
    options = {}
    if limit is not None:
        options["limit"] = limit
    if newest_first:
        options["newest_first"] = True
    if kinds is not None:
        return _routine_jobs.list_jobs(kinds=kinds, **options)
    return _routine_jobs.list_jobs(kind=kind, **options)


__all__ = [
    "notify",
    "agent_sessions_channel_id",
    "agent_sessions_channel_notify",
    "load_drainer_settings",
    "list_jobs",
]
