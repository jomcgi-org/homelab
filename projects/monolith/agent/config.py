# projects/monolith/agent/config.py
"""Settings for the monolith-agent-* MCP surface.

Discord defaults are baked into Helm values and surfaced via env vars. The
default channel is where notify() posts when a caller names none. There is no
application-level channel allow-list: the bot can only post to channels in the
server(s) it has been added to, and that membership is the operative boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.github import GITHUB_REPO


@dataclass(frozen=True)
class AgentSettings:
    discord_default_server_id: str
    discord_default_channel_id: str


@dataclass(frozen=True)
class DrainerSettings:
    enabled: bool
    max_jobs_per_cycle: int
    turn_timeout_seconds: int
    stall_threshold_seconds: int
    job_kind: str
    repo: str
    branch: str
    reasoning: bool


def load_settings() -> AgentSettings:
    return AgentSettings(
        discord_default_server_id=os.environ[
            "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID"
        ],
        discord_default_channel_id=os.environ[
            "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID"
        ],
    )


def drainer_enabled() -> bool:
    return os.environ.get("DRAINER_ENABLED", "false").lower() == "true"


def load_drainer_settings() -> DrainerSettings:
    return DrainerSettings(
        enabled=drainer_enabled(),
        max_jobs_per_cycle=int(os.environ.get("DRAINER_MAX_JOBS_PER_CYCLE", "3")),
        turn_timeout_seconds=int(
            os.environ.get("DRAINER_TURN_TIMEOUT_SECONDS", "1800")
        ),
        stall_threshold_seconds=int(
            os.environ.get("DRAINER_STALL_THRESHOLD_SECONDS", "2700")
        ),
        job_kind=os.environ.get("DRAINER_JOB_KIND", "qwen-drain"),
        repo=os.environ.get("DRAINER_REPO", GITHUB_REPO),
        branch=os.environ.get("DRAINER_BRANCH", "main"),
        # Drain jobs are usually multi-step repo audits, so Luna uses high
        # reasoning by default while each job can still opt out in its payload.
        reasoning=os.environ.get("DRAINER_REASONING", "true").lower() == "true",
    )


def agent_sessions_channel_id() -> str | None:
    """Channel for agent-session turn notifications, or None for the default.

    Read on its own rather than through AgentSettings: notifying a turn must not
    depend on the unrelated required Discord settings, and a session that cannot
    resolve an optional channel should fall back, never fail. Sessions notify on
    EVERY terminal turn by design (they are voice-driven), so routing them apart
    keeps validation noise off the channel real alerts use.
    """
    return os.environ.get("MONOLITH_AGENT_DISCORD_AGENT_SESSIONS_CHANNEL_ID") or None
