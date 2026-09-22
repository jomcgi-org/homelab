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
