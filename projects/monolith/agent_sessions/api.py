"""Public session API with a lightweight admission and reconciliation surface.

Storage-only callers must not load the MCP executor or its domain dependencies.
Existing execution functions resolve lazily to their unchanged implementation.
"""

from agent_sessions import admission as _admission
from agent_sessions.constants import KG_NODE_KEY as KG_NODE_KEY
from agent_sessions.reconciliation import (
    confirm_reconciled_guest_cessation as confirm_reconciled_guest_cessation,
    lock_cessation_session as lock_cessation_session,
)

_EXECUTION_EXPORTS = frozenset(
    {
        "run_synthetic_session",
        "start_session_for_swarm",
        "send_to_swarm_session",
        "reap_sessions_for_workflow",
        "start_session_for_thread",
        "send_to_thread_session",
        "session_id_for_thread",
    }
)


def lock_capacity_pool(session) -> None:
    """Lock shared capacity without committing the caller's transaction."""
    _admission.lock_pool(session)


def __getattr__(name: str):
    if name not in _EXECUTION_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from agent_sessions import execution_api

    return getattr(execution_api, name)
