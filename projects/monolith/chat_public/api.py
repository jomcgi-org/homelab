"""Public chat domain API: the only surface other domains may import.

Other domains must import from ``chat_public.api`` (enforced by
``import_boundaries_test``), never from ``chat_public`` internals such as
``chat_public.sse``.
"""

from __future__ import annotations

from chat_public.sse import format_sse  # re-exported

__all__ = ["format_sse"]
