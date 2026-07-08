"""Chat domain public API: the only surface other domains may import.

Other domains must import from ``chat.api`` (enforced by
``import_boundaries_test``), never from ``chat`` internals such as ``chat.bot``.
"""

from __future__ import annotations

from chat.bot import send_message  # re-exported
from chat.changelog import run_changelog_for_config  # re-exported
from chat.directive_admin import directive_history  # re-exported
from chat.directive_admin import list_directives  # re-exported
from chat.directive_admin import pin_directive  # re-exported
from chat.directive_admin import revert_directive  # re-exported
from chat.directive_admin import set_directive  # re-exported
from chat.goosecracker import ack_inflight  # re-exported
from chat.goosecracker import artifact_id_for_thread  # re-exported
from chat.goosecracker import build_injected_context  # re-exported
from chat.goosecracker import drain_agent_queue  # re-exported
from chat.goosecracker import ensure_steering_token  # re-exported
from chat.goosecracker import force_idle_thread  # re-exported
from chat.goosecracker import mark_inflight_running  # re-exported
from chat.goosecracker import parent_channel_for_thread  # re-exported
from chat.goosecracker import reclaim_orphaned_agent_sessions  # re-exported
from chat.goosecracker import set_progress_message  # re-exported
from chat.goosecracker import take_progress_message  # re-exported
from chat.goosecracker_progress import (  # re-exported
    clear as reset_goosecracker_progress,
    mark_done as mark_goosecracker_progress_done,
    set_notice as set_goosecracker_progress_notice,
)
from chat.orchestrator import replan  # re-exported
from chat.orchestrator_plan import Plan  # re-exported
from chat.orchestrator_plan import PlanStep  # re-exported
from chat.outbox import enqueue_edit  # re-exported
from chat.outbox import enqueue_message  # re-exported
from chat.summarizer import build_openrouter_caller  # re-exported
from chat.summarizer import conversational_agent_reply  # re-exported
from chat.summarizer import run_summary_generation  # re-exported
from chat.whatsapp_session import checklist_final as whatsapp_checklist_final
from chat.whatsapp_session import enqueue_message_sync as enqueue_whatsapp_message
from chat.whatsapp_session import (
    group_jid_for_session as whatsapp_group_jid_for_session,
)

__all__ = [
    "Plan",
    "PlanStep",
    "replan",
    "ack_inflight",
    "artifact_id_for_thread",
    "build_injected_context",
    "build_openrouter_caller",
    "conversational_agent_reply",
    "drain_agent_queue",
    "ensure_steering_token",
    "force_idle_thread",
    "mark_inflight_running",
    "parent_channel_for_thread",
    "reclaim_orphaned_agent_sessions",
    "set_progress_message",
    "take_progress_message",
    "enqueue_edit",
    "enqueue_message",
    "enqueue_whatsapp_message",
    "mark_goosecracker_progress_done",
    "reset_goosecracker_progress",
    "set_goosecracker_progress_notice",
    "run_changelog_for_config",
    "run_summary_generation",
    "send_message",
    "list_directives",
    "directive_history",
    "set_directive",
    "pin_directive",
    "revert_directive",
    "whatsapp_checklist_final",
    "whatsapp_group_jid_for_session",
]
