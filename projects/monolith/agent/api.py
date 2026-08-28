"""Agent domain public API: the only surface other domains may import.

Other domains must import from ``agent.api`` (enforced by
``import_boundaries_test``), never from ``agent`` internals such as
``agent.notify``.
"""

from __future__ import annotations

from agent import config as _config
from agent import routine_jobs as _routine_jobs
from agent.config import agent_sessions_channel_id  # re-exported
from agent.notify import notify  # re-exported


def load_drainer_settings():
    return _config.load_drainer_settings()


def list_jobs(*, kind: str) -> list[dict]:
    return _routine_jobs.list_jobs(kind=kind)


__all__ = [
    "notify",
    "agent_sessions_channel_id",
    "load_drainer_settings",
    "list_jobs",
]
