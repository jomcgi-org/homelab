"""Agent domain public API: the only surface other domains may import.

Other domains must import from ``agent.api`` (enforced by
``import_boundaries_test``), never from ``agent`` internals such as
``agent.notify``.
"""

from __future__ import annotations

from agent.dispatch import submit  # re-exported
from agent.notify import notify  # re-exported

__all__ = ["notify", "submit"]
