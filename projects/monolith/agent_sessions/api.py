"""Public session API with a lightweight admission and reconciliation surface.

Storage-only callers must not load the MCP executor or its domain dependencies.
Existing execution functions resolve lazily to their unchanged implementation.
"""

from agent_sessions import admission as _admission
from agent_sessions.constants import DRAINER_NODE_KEY as DRAINER_NODE_KEY
from agent_sessions.constants import KG_NODE_KEY as KG_NODE_KEY
from agent_sessions.reconciliation import (
    read_factory_dispatch as read_factory_dispatch,
    cancel_queued_factory_attempt as cancel_queued_factory_attempt,
    confirm_reconciled_guest_cessation as confirm_reconciled_guest_cessation,
    inspect_lost_before_guest_factory_attempt as inspect_lost_before_guest_factory_attempt,
    lock_cessation_session as lock_cessation_session,
    read_lost_before_guest_factory_attempt as read_lost_before_guest_factory_attempt,
    read_not_invoked_factory_attempt as read_not_invoked_factory_attempt,
    read_uncertain_factory_attempt as read_uncertain_factory_attempt,
    settle_lost_before_guest_factory_attempt as settle_lost_before_guest_factory_attempt,
    settle_uncertain_factory_attempt as settle_uncertain_factory_attempt,
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


def response_lost_recovery_enabled() -> bool:
    """Whether response-loss recovery is enabled for this process."""
    from agent_sessions import store

    return store.response_lost_recovery_enabled()


def read_response_lost_hold(session_id: int):
    """The live response-loss hold on one session, or None."""
    from agent_sessions import store

    return store.read_response_lost_hold_sync(session_id)


def adopt_response_lost_result(session_id: int, artifact_path: str | None = None):
    """Finish a held turn from its committed receipt, without re-executing it."""
    from agent_sessions import store

    return store.adopt_response_lost_result(session_id, artifact_path)


def settle_response_lost_hold(session_id: int, reason: str) -> bool:
    """End an unrecoverable hold as the ordinary unknown outcome it is."""
    from agent_sessions import store

    return store.settle_response_lost_hold(session_id, reason)


def __getattr__(name: str):
    if name not in _EXECUTION_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from agent_sessions import execution_api

    return getattr(execution_api, name)
