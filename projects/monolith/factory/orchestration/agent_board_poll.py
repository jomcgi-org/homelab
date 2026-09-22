"""Staged agent-board poll at the factory's between-job claim boundary."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

POLL_ENABLED_ENV = "FACTORY_AGENT_BOARD_POLL_ENABLED"


def poll_enabled() -> bool:
    return os.getenv(POLL_ENABLED_ENV, "false").lower() == "true"


def _read_active(lanes: tuple[str, ...]) -> frozenset[str]:
    # Lazy to keep the factory import graph independent while the consumer is
    # disabled. This internal reader still requires the server-trusted binding
    # used by board tools. Shared bearer identity alone cannot authorize it.
    from knowledge.board import active_blocker_topics_for_poll

    return active_blocker_topics_for_poll(lanes)


def eligible_lanes(
    lanes: Iterable[str],
    *,
    reader: Callable[[tuple[str, ...]], frozenset[str]] | None = None,
) -> tuple[str, ...]:
    """Remove lanes with active blockers before the next receipt is claimed.

    Board failure is fail-open for queue availability: the exclusive receipt
    and agent lock gates remain authoritative. Only ``blocker:lane:*`` topics
    can defer here, so a soft ``claim:*`` never replaces or bypasses those
    exclusive gates.
    """

    original = tuple(lanes)
    if not poll_enabled() or not original:
        return original
    try:
        blocked = (reader or _read_active)(original)
    except Exception:  # noqa: BLE001 - an optional read cannot stop intake
        logger.warning("factory agent-board poll unavailable", exc_info=True)
        return original
    return tuple(lane for lane in original if f"blocker:lane:{lane}" not in blocked)


__all__ = ["eligible_lanes", "poll_enabled"]
