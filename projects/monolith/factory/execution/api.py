"""Public session API with a lightweight admission and reconciliation surface.

Storage-only callers must not load the MCP executor or its domain dependencies.
Existing execution functions resolve lazily to their unchanged implementation.
"""

from factory.execution import admission as _admission
from factory.execution.constants import DRAINER_NODE_KEY as DRAINER_NODE_KEY
from factory.execution.constants import KG_NODE_KEY as KG_NODE_KEY
from factory.execution.reconciliation import (
    adopt_completed_factory_receipt as adopt_completed_factory_receipt,
    cancel_queued_factory_attempt as cancel_queued_factory_attempt,
    confirm_reconciled_guest_cessation as confirm_reconciled_guest_cessation,
    confirm_reconciled_unbound_attempt as confirm_reconciled_unbound_attempt,
    inspect_lost_before_guest_factory_attempt as inspect_lost_before_guest_factory_attempt,
    inspect_lost_before_session_factory_attempt as inspect_lost_before_session_factory_attempt,
    lock_cessation_session as lock_cessation_session,
    read_drained_lost_factory_attempt as read_drained_lost_factory_attempt,
    read_interrupted_factory_continuation as read_interrupted_factory_continuation,
    read_factory_dispatch as read_factory_dispatch,
    read_interrupted_retry_not_invoked_factory_attempt as read_interrupted_retry_not_invoked_factory_attempt,
    read_lost_before_guest_factory_attempt as read_lost_before_guest_factory_attempt,
    read_never_dispatched_factory_attempt as read_never_dispatched_factory_attempt,
    read_not_invoked_factory_attempt as read_not_invoked_factory_attempt,
    read_uncertain_factory_attempt as read_uncertain_factory_attempt,
    settle_drained_lost_factory_attempt as settle_drained_lost_factory_attempt,
    settle_interrupted_factory_continuation as settle_interrupted_factory_continuation,
    settle_lost_before_guest_factory_attempt as settle_lost_before_guest_factory_attempt,
    settle_lost_before_session_factory_attempt as settle_lost_before_session_factory_attempt,
    settle_never_dispatched_factory_attempt as settle_never_dispatched_factory_attempt,
    settle_uncertain_factory_attempt as settle_uncertain_factory_attempt,
)

from factory.execution.factory_stop import (
    inspect_factory_attempt_stop as inspect_factory_attempt_stop,
    fence_factory_attempt_stop as fence_factory_attempt_stop,
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
    from factory.execution import store

    return store.response_lost_recovery_enabled()


def read_response_lost_hold(session_id: int):
    """The live response-loss hold on one session, or None."""
    from factory.execution import store

    return store.read_response_lost_hold_sync(session_id)


def adopt_response_lost_result(session_id: int, artifact_path: str | None = None):
    """Finish a held turn from its committed receipt, without re-executing it."""
    from factory.execution import store

    return store.adopt_response_lost_result(session_id, artifact_path)


def settle_response_lost_hold(
    session_id: int, reason: str, *, expected_hold: dict | None = None
) -> bool:
    """End an unrecoverable hold as the ordinary unknown outcome it is."""
    from factory.execution import store

    return store.settle_response_lost_hold(
        session_id, reason, expected_hold=expected_hold
    )


def __getattr__(name: str):
    if name not in _EXECUTION_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from factory.execution import execution_api

    return getattr(execution_api, name)
