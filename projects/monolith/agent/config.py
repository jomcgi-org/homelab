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


@dataclass(frozen=True)
class AgentSettings:
    discord_default_server_id: str
    discord_default_channel_id: str


def load_settings() -> AgentSettings:
    return AgentSettings(
        discord_default_server_id=os.environ[
            "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID"
        ],
        discord_default_channel_id=os.environ[
            "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID"
        ],
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
