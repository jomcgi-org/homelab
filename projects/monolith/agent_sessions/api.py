"""Compatibility adapter for the existing Discord integration."""

_EXPORTS = frozenset(
    {
        "start_session_for_thread",
        "send_to_thread_session",
        "session_id_for_thread",
    }
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from factory.execution import api

    return getattr(api, name)
