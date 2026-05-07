"""Routine-facing Discord notification.

Defaults to the homelab channel from settings; channel arg, if
specified, must be in the allow-list.
"""

from __future__ import annotations

from typing import Literal

from agent.config import load_settings
from chat.bot import send_message


async def notify(
    message: str,
    level: Literal["info", "warn", "error"] = "info",
    channel: str | None = None,
) -> dict:
    settings = load_settings()
    target = channel or settings.discord_default_channel_id
    if target not in settings.discord_allowed_channel_ids:
        raise ValueError(f"Channel {target!r} not in allow-list")
    await send_message(target, message, level=level)
    return {"ok": True, "channel": target}
