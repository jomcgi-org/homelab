# projects/monolith/agent/config.py
"""Settings for the monolith-agent-* MCP surface.

Discord defaults are baked into Helm values and surfaced via env
vars. The allow-list restricts which channel IDs the notify tool
will publish to; the default channel is always allowed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    discord_default_server_id: str
    discord_default_channel_id: str
    discord_allowed_channel_ids: frozenset[str]


def load_settings() -> AgentSettings:
    default_channel = os.environ["MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID"]
    allowed_raw = os.environ.get("MONOLITH_AGENT_DISCORD_ALLOWED_CHANNEL_IDS", "")
    allowed = {c.strip() for c in allowed_raw.split(",") if c.strip()}
    allowed.add(default_channel)  # default is always allowed
    return AgentSettings(
        discord_default_server_id=os.environ[
            "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID"
        ],
        discord_default_channel_id=default_channel,
        discord_allowed_channel_ids=frozenset(allowed),
    )
