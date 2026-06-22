"""Chat domain public API: the only surface other domains may import.

Other domains must import from ``chat.api`` (enforced by
``import_boundaries_test``), never from ``chat`` internals such as ``chat.bot``.
"""

from __future__ import annotations

from chat.bot import send_message  # re-exported
from chat.summarizer import run_summary_generation  # re-exported

__all__ = ["run_summary_generation", "send_message"]
