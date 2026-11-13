"""Compatibility adapter for the existing Discord integration."""

from core.lazy import lazy_exports

_EXPORTS = frozenset(
    {
        "start_session_for_thread",
        "send_to_thread_session",
        "session_id_for_thread",
    }
)


__getattr__ = lazy_exports(__name__, dict.fromkeys(_EXPORTS, "factory.execution.api"))
