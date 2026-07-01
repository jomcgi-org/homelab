"""Chat domain public API: the only surface other domains may import.

Other domains must import from ``chat.api`` (enforced by
``import_boundaries_test``), never from ``chat`` internals such as ``chat.bot``.
"""

from __future__ import annotations

from chat.bot import send_message  # re-exported
from chat.changelog import run_changelog_for_config  # re-exported
from chat.goosecracker import drain_agent_queue  # re-exported
from chat.goosecracker_progress import (  # re-exported
    mark_done as mark_goosecracker_progress_done,
)
from chat.outbox import enqueue_message  # re-exported
from chat.summarizer import run_summary_generation  # re-exported

__all__ = [
    "drain_agent_queue",
    "enqueue_message",
    "mark_goosecracker_progress_done",
    "run_changelog_for_config",
    "run_summary_generation",
    "send_message",
]
