"""Outcome markers shared by durable execution owners across domains.

Owners persist this marker when they cannot establish an invocation's result.
It requires reconciliation before automatic execution can resume.
"""

UNKNOWN_INVOCATION = "invocation_outcome_unknown"


def terminal_dispatch_cessation(observed, recovery, owner, failed_at) -> bool:
    """Match terminal guest evidence to the dispatch that recorded a failure.

    Eviction or confirmed destruction can cause the client's error, so the
    terminal stamp need not follow the error stamp. The invocation must instead
    fall inside this exact dispatch's lifetime. A lost invocation response need
    not have a completion stamp. Callers still own guest identity, fresh reads,
    generation continuity, and atomic ownership revalidation.
    """
    from datetime import datetime

    if observed.get("state") not in {"evicted", "destroyed"}:
        return False
    if not isinstance(recovery, dict) or not isinstance(owner, str) or not owner:
        return False
    if (
        recovery.get("claim_owner") != owner
        or type(recovery.get("dispatch_count")) is not int
        or recovery["dispatch_count"] < 1
    ):
        return False
    started = observed.get("invoke_started_at")
    updated = observed.get("updated_at")
    last_invoke = observed.get("last_invoke_at")
    if any(type(value) is not int or value < 1 for value in (started, updated)):
        return False
    if last_invoke is not None and (
        type(last_invoke) is not int or not 0 < last_invoke <= updated
    ):
        return False
    try:
        dispatched = datetime.fromisoformat(
            recovery["last_dispatch_at"].replace("Z", "+00:00")
        )
        if dispatched.tzinfo is None or failed_at.tzinfo is None:
            return False
        return (
            int(dispatched.timestamp() * 1000)
            < started
            <= int(failed_at.timestamp() * 1000)
            and started <= updated
        )
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return False


def parked_invocation_candidate(observed) -> bool:
    """Candidate for conditional retirement, never cessation proof by itself.

    A parked session can have a queued wake-up. Only the control plane may
    atomically retire this exact snapshot after checking its local waiters.
    A recorded drain has its own continuation protocol and is excluded.
    """
    if not isinstance(observed, dict) or observed.get("state") != "parked":
        return False
    generation = observed.get("generation")
    started = observed.get("invoke_started_at")
    updated = observed.get("updated_at")
    return (
        "interrupted_turn" in observed
        and observed["interrupted_turn"] is None
        and type(generation) is int
        and generation >= 0
        and type(started) is int
        and started > 0
        and type(updated) is int
        and updated >= started
    )


def never_invoked_view(observed, guest_id) -> bool:
    """Whether the control plane shows this exact guest never ran an invoke.

    EmberVM stamps ``invoke_started_at`` and increments ``turn_seq`` in one
    durable write before any invoke runs and never clears either, so
    ``turn_seq`` 0 with no invoke stamp, interruption or stop record names a
    guest that has never run a model turn in any generation. Remote evidence
    only: a caller must pair it with local proof that no POST was ever sent.
    Mirrors factory supervision's ``_never_invoked_guest_cessation`` (#6553).
    """
    required = {
        "session_id",
        "generation",
        "turn_seq",
        "created_at",
        "invoke_started_at",
        "last_invoke_at",
        "interrupted_turn",
        "stop_intent",
        "stop_completion",
    }
    return bool(
        isinstance(observed, dict)
        and required.issubset(observed)
        and observed["session_id"] == guest_id
        and type(observed["turn_seq"]) is int
        and observed["turn_seq"] == 0
        and type(observed["generation"]) is int
        and observed["generation"] >= 0
        and type(observed["created_at"]) is int
        and observed["created_at"] >= 1
        and all(
            observed[key] is None
            for key in (
                "invoke_started_at",
                "last_invoke_at",
                "interrupted_turn",
                "stop_intent",
                "stop_completion",
            )
        )
        and observed.get("stop_precondition") is None
    )
