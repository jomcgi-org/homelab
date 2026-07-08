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
