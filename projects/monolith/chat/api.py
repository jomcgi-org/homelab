"""Chat domain public API: the only surface other domains may import.

Other domains must import from ``chat.api`` (enforced by
``import_boundaries_test``), never from ``chat`` internals such as ``chat.bot``.
"""

from __future__ import annotations

from chat.bot import send_message  # re-exported
from chat.changelog import run_changelog_for_config  # re-exported
from chat.goosecracker import ack_inflight  # re-exported
from chat.goosecracker import artifact_id_for_thread  # re-exported
from chat.goosecracker import drain_agent_queue  # re-exported
from chat.goosecracker import force_idle_thread  # re-exported
from chat.goosecracker import mark_inflight_running  # re-exported
from chat.goosecracker import parent_channel_for_thread  # re-exported
from chat.goosecracker import reclaim_orphaned_agent_sessions  # re-exported
from chat.goosecracker_progress import (  # re-exported
    clear as reset_goosecracker_progress,
    mark_done as mark_goosecracker_progress_done,
    set_notice as set_goosecracker_progress_notice,
)
from chat.outbox import enqueue_message  # re-exported
from chat.summarizer import conversational_agent_reply  # re-exported
from chat.summarizer import run_summary_generation  # re-exported

__all__ = [
    "ack_inflight",
    "artifact_id_for_thread",
    "conversational_agent_reply",
    "drain_agent_queue",
    "force_idle_thread",
    "mark_inflight_running",
    "parent_channel_for_thread",
    "reclaim_orphaned_agent_sessions",
    "enqueue_message",
    "mark_goosecracker_progress_done",
    "reset_goosecracker_progress",
    "set_goosecracker_progress_notice",
    "run_changelog_for_config",
    "run_summary_generation",
    "send_message",
]
