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
from chat.orchestrator_plan import Plan  # re-exported
from chat.safeguards import pardon_user  # re-exported
from chat.safeguards import trust_status  # re-exported
from chat.orchestrator_plan import PlanStep  # re-exported
from chat.outbox import enqueue_edit  # re-exported
from chat.outbox import enqueue_message  # re-exported
from chat.summarizer import build_openrouter_caller  # re-exported
from chat.summarizer import conversational_agent_reply  # re-exported
from chat.summarizer import run_summary_generation  # re-exported
from chat.whatsapp_session import enqueue_message_sync as enqueue_whatsapp_message
from chat.whatsapp_session import (
    household_group_jids as whatsapp_household_group_jids,
)

__all__ = [
    "Plan",
    "PlanStep",
    "build_openrouter_caller",
    "conversational_agent_reply",
    "enqueue_edit",
    "enqueue_message",
    "enqueue_whatsapp_message",
    "run_changelog_for_config",
    "run_summary_generation",
    "send_message",
    "list_directives",
    "directive_history",
    "set_directive",
    "pin_directive",
    "revert_directive",
    "trust_status",
    "pardon_user",
    "whatsapp_household_group_jids",
]
