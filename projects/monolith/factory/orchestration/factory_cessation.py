"""Produce cessation evidence for a stranded uncertain factory attempt.

Stop supervision (factory_supervision.py) releases an uncertain attempt only
on positive cessation evidence, and every one of its proofs waits for that
evidence to appear: a stop completion, a terminal control-plane transition, a
drained loss, sustained absence. When none of them ever matches, the attempt
holds its lane slot and admission permit forever. Elapsed time is still not
evidence (FACTORY.md, "Elapsed time is not cessation evidence"), so the
answer to an attempt that waited too long is not to release it on the clock
but to go and make the evidence.

This does that for one exact attempt:

1. Record, durably and before any external effect, the fingerprinted identity
   of the attempt and the exact guest it is about to terminate
   (``cessation_intent``).
2. Destroy that guest through the ordinary EmberVM destroy route, consuming a
   bounded, audited request budget before each call (``cessation_request``).
3. Observe that recorded guest, never the live binding, until the control
   plane has reported it gone across a sampled window with the same rules as
   the #6156 absence proof (``cessation_absence``, broken by
   ``cessation_presence``).
4. Settle in one transaction through factory_supervision._settle_failed_attempt:
   run failed, FactoryStart failed, permit settled with cessation confirmed,
   cost unknown so the reserved ceiling stays charged.

An attempt that never bound a guest has nothing to destroy. It settles on the
local ledger's proof that no binding was ever committed, at the reserved
ceiling unless the exact lost-before-guest proof also holds, which is the only
evidence this codebase accepts for a measured zero.

Deadline expiry chooses which attempts are worth this; it never settles one.
The automatic path is off behind FACTORY_ACTIVE_CESSATION_ENABLED. An operator
can drive the same steps for one exact attempt with ``request_cessation``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

from opentelemetry import trace
from sqlmodel import Session, select

from factory.execution.api import (
    inspect_lost_before_guest_factory_attempt,
    read_stranded_factory_attempt,
    settle_lost_before_guest_factory_attempt,
    settle_stranded_factory_attempt,
)
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_supervision as supervisor
from factory.orchestration import graph
from factory.orchestration.factory_models import FactoryAudit, FactoryStart
from factory.orchestration.models import SwarmNodeRun
from factory.orchestration.tracing import set_attributes, tracer

ACTOR = "factory:active-cessation"
# Matches FACTORY_DEADLINE_BACKSTOP_GRACE_SECONDS and the reconciler pause TTL,
# so every evidence-waiting proof has had the same two hours of ticks to
# settle the attempt before anything goes and destroys its guest.
GRACE_SECONDS = 7200
# Two DELETEs, spaced by one observation interval. A destroy the control
# plane accepted normally reads back terminal on the next observation; a
# guest still answering after two requests needs a person, not a third.
MAX_DESTROY_REQUESTS = 2
DESTROY_REQUEST_INTERVAL_SECONDS = supervisor.ABSENCE_OBSERVATION_INTERVAL_SECONDS
HTTP_SECONDS = supervisor.HTTP_SECONDS
TERMINAL_WORKFLOW_STATUSES = frozenset(
    {"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}
)
# Control-plane states that name a guest which is no longer running. The
# control plane keeps a terminal record after teardown, so a guest this path
# destroyed normally reads back "destroyed" rather than 404. Both are counted
# as the same "gone" sample; any other state is presence and breaks the run.
GONE_STATES = frozenset({"destroyed", "evicted", "expired", "failed"})
_ACTIONS = (
    "cessation_intent",
    "cessation_request",
    "cessation_absence",
    "cessation_presence",
    "stop_settled",
)
# Audits whose identity names the guest an earlier supervisor recorded for
# this workflow. Read only when the live binding has already been cleared, to
# recover the guest a destroy cleared it for (#6288).
_GUEST_EVIDENCE_ACTIONS = (
    "stop_intent",
    "bound_zero_turn_fence",
    "attempt_stop_requested",
)


# One span per stranded attempt considered, on every path: the conductor tick
# (supervise_task) and the operator repair (request_cessation). Before it, an
# attempt skipped because the flag was off, the deadline grace had not passed,
# its workflow was still live or its session id was unresolved left no trace.
SPAN = "factory.cessation.advance"
# Refusal codes are fixed identifiers; anything else (a stray ValueError
# message) collapses to one value so the attribute stays low-cardinality.
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _reason_code(code: str) -> str:
    return code if _REASON_CODE.match(code) else "other"


def _span_attributes(span, pin, workflow_status, trigger):
    set_attributes(
        span,
        {
            "factory.task_id": pin.get("task_id"),
            "factory.node_key": pin.get("node_key"),
            "factory.attempt": pin.get("attempt"),
            "factory.workflow_status": workflow_status,
            "factory.cessation.trigger": trigger,
        },
    )


def _span_outcome(span, outcome, reason=None):
    set_attributes(
        span,
        {"factory.cessation.outcome": outcome, "factory.cessation.reason": reason},
    )
    return outcome


def enabled() -> bool:
    return os.environ.get("FACTORY_ACTIVE_CESSATION_ENABLED", "false").lower() == "true"


def _now():
    return datetime.now(timezone.utc)


def _records(db, pin):
    rows = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == pin["task_id"],
            FactoryAudit.action.in_(_ACTIONS),
        )
        .order_by(FactoryAudit.id)
    ).all()
    return [
        (row.action, detail)
        for row in rows
        if (detail := json.loads(row.detail_json)).get("workflow_id")
        == pin["workflow_id"]
    ]


def _audit(db, pin, action, **detail):
    controls._audit(
        db,
        ACTOR,
        action,
        task_id=pin["task_id"],
        workflow_id=pin["workflow_id"],
        **detail,
    )


def _intent(records):
    intents = [detail for action, detail in records if action == "cessation_intent"]
    if len(intents) > 1:
        raise ValueError("conflicting_cessation_intents")
    return intents[0] if intents else None


def _locked_stranded(db, control, pin, session_id):
    """Lock the exact uncertain run, start and session identity.

    The run and start checks mirror factory_supervision._locked_attempt, but
    both ledgers must already read "uncertain": a reserved or dispatched
    attempt is still owned by its node workflow, and this path never races
    live work.
    """
    if control.state == "disabled":
        raise ValueError("factory_disabled")
    run = db.exec(
        select(SwarmNodeRun)
        .where(
            SwarmNodeRun.task_id == pin["task_id"],
            SwarmNodeRun.node_key == pin["node_key"],
            SwarmNodeRun.attempt == pin["attempt"],
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        run is None
        or json.loads(run.pin_json or "null") != pin
        or run.dispatch_key != pin["workflow_id"]
        or run.session_id not in (None, session_id)
        or run.status != "uncertain"
        or db.exec(
            select(SwarmNodeRun.id).where(
                SwarmNodeRun.task_id == pin["task_id"],
                SwarmNodeRun.node_key == pin["node_key"],
                SwarmNodeRun.attempt > pin["attempt"],
            )
        ).first()
        is not None
    ):
        raise ValueError("factory_run_changed")
    start = db.exec(
        select(FactoryStart)
        .where(
            FactoryStart.task_id == pin["task_id"],
            FactoryStart.start_key == pin["workflow_id"],
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        start is None
        or start.status != "uncertain"
        or start.session_id not in (None, session_id)
        or start.model != pin["model"]
        or start.max_cost_usd != pin["max_cost_usd"]
    ):
        raise ValueError("factory_start_changed")
    return read_stranded_factory_attempt(db, pin, session_id), run


def _recorded_guests(db, pin, session_id):
    """Guest ids earlier supervisors recorded for this exact workflow."""
    guests = set()
    for row in db.exec(
        select(FactoryAudit).where(
            FactoryAudit.task_id == pin["task_id"],
            FactoryAudit.action.in_(_GUEST_EVIDENCE_ACTIONS),
        )
    ).all():
        detail = json.loads(row.detail_json)
        identity = detail.get("identity")
        if (
            detail.get("workflow_id") == pin["workflow_id"]
            and isinstance(identity, dict)
            and identity.get("session_id") == session_id
            and isinstance(identity.get("guest_id"), str)
            and identity["guest_id"]
        ):
            guests.add(identity["guest_id"])
    return guests


def _target(db, pin, identity):
    """The exact guest this intent will terminate, and the attempt's shape.

    The live binding when there is one. When a destroy has already cleared it
    but a binding existed, the guest is recovered only from the monolith's own
    durable records: the receipts prepared for it and the identities earlier
    supervisors audited for this workflow. Exactly one candidate is required;
    zero or several refuse, because destroying a guessed guest is worse than
    holding the slot.
    """
    if identity["guest_id"] is not None:
        return identity["guest_id"], "bound"
    if not identity["bound"]:
        # No guest was ever bound, so there is nothing to destroy. The session
        # has to be terminal as well, or an executor could still bind one.
        if identity["status"] not in {"failed", "warn"}:
            raise ValueError("never_bound_session_not_terminal")
        return None, "never_bound"
    candidates = set(identity["receipt_guest_ids"]) | _recorded_guests(
        db, pin, identity["session_id"]
    )
    if len(candidates) != 1:
        raise ValueError("guest_identity_unrecoverable")
    return candidates.pop(), "bound"


def _verify(saved, identity):
    """Refuse any change between the recorded intent and this tick.

    The fingerprint covers everything that would show the attempt executing
    again or taken over. The binding is compared separately: it may still name
    the recorded guest or have been cleared by the destroy, never anything
    else.
    """
    if saved["identity"]["identity_sha256"] != identity["identity_sha256"]:
        raise ValueError("cessation_identity_changed")
    if identity["guest_id"] not in (None, saved["guest_id"]):
        raise ValueError("cessation_guest_changed")
    if saved["shape"] == "never_bound" and identity["bound"]:
        raise ValueError("cessation_guest_changed")


def _begin(pin, session_id, *, trigger, actor, expected_identity_sha256=None):
    """Return the committed intent, recording it first when allowed.

    ``trigger`` None means only an existing intent may be advanced. Returns
    None when there is nothing to do, and names why on the current span.
    """
    with controls._locked_session() as (db, control):
        records = _records(db, pin)
        if any(action == "stop_settled" for action, _ in records):
            set_attributes(
                trace.get_current_span(),
                {"factory.cessation.reason": "already_settled"},
            )
            return None
        identity, _run = _locked_stranded(db, control, pin, session_id)
        saved = _intent(records)
        if saved is not None:
            _verify(saved, identity)
            return saved
        if trigger is None:
            set_attributes(
                trace.get_current_span(), {"factory.cessation.reason": "no_intent"}
            )
            return None
        if (
            expected_identity_sha256 is not None
            and expected_identity_sha256 != identity["identity_sha256"]
        ):
            raise ValueError("factory_attempt_changed")
        guest_id, shape = _target(db, pin, identity)
        saved = {
            "identity": identity,
            "guest_id": guest_id,
            "shape": shape,
            "trigger": trigger,
            "requested_by": actor,
            "observed_at": _now().isoformat(),
        }
        _audit(db, pin, "cessation_intent", **saved)
        return saved


def _get(guest_id):
    from factory.execution.transport import EmberVmShimTransport

    async def request():
        return await asyncio.wait_for(
            EmberVmShimTransport().get_session(guest_id), timeout=HTTP_SECONDS
        )

    return asyncio.run(request())


def _destroy(guest_id):
    from factory.execution.transport import EmberVmShimTransport

    async def request():
        # Unconditional, like the departed-node destroy in factory_supervision:
        # the control plane accepts no stop precondition on a parked or
        # banked guest (#6091), and the exact target was committed before
        # this call. The DELETE is only the action; the sampled observation
        # afterwards is the evidence.
        return await asyncio.wait_for(
            EmberVmShimTransport().destroy_session(guest_id), timeout=HTTP_SECONDS
        )

    return asyncio.run(request())


def _gone_run(records, saved):
    """The unbroken tail of gone observations for this exact intent.

    Same rules as factory_supervision._absence_run: newest first, stopped at
    the first record that is not this intent's gone sample or at a gap wider
    than ABSENCE_MAX_GAP_SECONDS, so readings from separate episodes never add
    up to a release.
    """
    sha = saved["identity"]["identity_sha256"]
    run = []
    previous = None
    for action, detail in reversed(records):
        if action == "cessation_request":
            # A request is our own action, not an observation of the guest,
            # and the gap rule still bounds a run across it.
            continue
        if (
            action != "cessation_absence"
            or detail.get("identity_sha256") != sha
            or detail.get("guest_id") != saved["guest_id"]
        ):
            break
        stamp = supervisor._timestamp(detail["observed_at"])
        if (
            previous is not None
            and (previous - stamp).total_seconds() > supervisor.ABSENCE_MAX_GAP_SECONDS
        ):
            break
        run.append(stamp)
        previous = stamp
    run.reverse()
    return run


def _settle(db, pin, session_id, run, saved, original_result, *, proof):
    """The single settlement transaction, under the caller's control lock."""
    # A phase carried over from an earlier outcome must never refund this one:
    # graph._outcome_phase reads invocation_phase before any typed proof.
    unknown = {
        **{k: v for k, v in original_result.items() if k != "invocation_phase"},
        "cost_usd": None,
    }
    if saved["shape"] == "never_bound":
        lost, _refusal = inspect_lost_before_guest_factory_attempt(db, pin, session_id)
        if lost is not None:
            # The one proof this codebase accepts for a measured zero. It
            # re-reads under the same locks the fingerprint was just checked
            # under, so both describe the same rows.
            return supervisor._settle_failed_attempt(
                db,
                pin,
                session_id,
                {**saved["identity"], "cost_usd": 0.0},
                run,
                unknown,
                reason=(
                    "lost_before_guest: supervised cessation found an invoked "
                    "attempt that never bound a guest"
                ),
                evidence_key="lost_before_guest",
                evidence=lost,
                settle_attempt=lambda db, pin, _identity: (
                    settle_lost_before_guest_factory_attempt(db, pin, lost)
                ),
                accounting={"cost_basis": "unknown", "accounting": "unknown_cost"},
            )

    def settle(db, pin, identity):
        settle_stranded_factory_attempt(
            db,
            pin,
            identity,
            guest_id=saved["guest_id"],
            outcome=(
                "supervised_cessation"
                if saved["shape"] == "bound"
                else "never_bound_released"
            ),
        )

    # Unknown, never the measured figure a turn may carry: a bound guest that
    # outlived its recorded turn may have spent past it, so the reservation
    # ceiling stays charged. A never-bound attempt without the exact
    # lost-before-guest proof is charged the same way rather than guessed at.
    return supervisor._settle_failed_attempt(
        db,
        pin,
        session_id,
        {**saved["identity"], "cost_usd": None},
        run,
        unknown,
        reason=(
            "supervised_cessation: the recorded guest was destroyed and the "
            "control plane then reported it gone across the sampled window"
            if saved["shape"] == "bound"
            else "supervised_cessation: no guest was ever bound to this attempt"
        ),
        evidence_key="supervised_cessation",
        evidence=proof,
        settle_attempt=settle,
    )


def _settle_if_proven(pin, session_id, saved, original_result, *, proof_for):
    """Re-verify everything under the lock and settle when proof holds."""
    with controls._locked_session() as (db, control):
        records = _records(db, pin)
        if any(action == "stop_settled" for action, _ in records):
            return "settled"
        identity, run = _locked_stranded(db, control, pin, session_id)
        _verify(saved, identity)
        proof = proof_for(db, records)
        if proof is None:
            return "waiting"
        _settle(db, pin, session_id, run, saved, original_result, proof=proof)
        _audit(
            db,
            pin,
            "stop_settled",
            reason="supervised_cessation",
            identity=saved["identity"],
            guest_id=saved["guest_id"],
            shape=saved["shape"],
            supervised_cessation=proof,
            cessation_confirmed=True,
            intervention_required=False,
        )
        return "settled"


def _record_gone(pin, session_id, saved, original_result, observation):
    """Sample one gone reading, and settle once the run has held."""
    now = _now()

    def proof_for(db, records):
        seen = _gone_run(records, saved)
        newest = seen[-1] if seen else None
        if (
            newest is None
            or (now - newest).total_seconds()
            >= supervisor.ABSENCE_OBSERVATION_INTERVAL_SECONDS
        ):
            if len(seen) >= supervisor.MAX_ABSENCE_OBSERVATIONS and not any(
                action == "stop_observation"
                and detail.get("reason") == "cessation_absence_run_unsettled"
                for action, detail in supervisor._records(db, pin)
            ):
                # Inline rather than supervisor._note, which would take the
                # control lock this transaction already holds.
                supervisor._audit(
                    db,
                    pin,
                    "stop_observation",
                    reason="cessation_absence_run_unsettled",
                    intervention_required=True,
                    cessation_confirmed=False,
                )
            _audit(
                db,
                pin,
                "cessation_absence",
                identity_sha256=saved["identity"]["identity_sha256"],
                guest_id=saved["guest_id"],
                observation=len(seen) + 1,
                observed=observation,
                observed_at=now.isoformat(),
                cessation_confirmed=False,
                intervention_required=False,
            )
            return None
        if len(seen) < supervisor.MIN_ABSENCE_OBSERVATIONS:
            return None
        if (now - seen[-1]).total_seconds() > supervisor.ABSENCE_MAX_GAP_SECONDS:
            return None
        held = (newest - seen[0]).total_seconds()
        if held < supervisor.ABSENCE_CONFIRM_SECONDS:
            return None
        return {
            "kind": "supervised_termination",
            "guest_id": saved["guest_id"],
            "destroy_requests": sum(
                action == "cessation_request" for action, _ in records
            ),
            "observations": len(seen),
            "first_observed_at": seen[0].isoformat(),
            "last_observed_at": newest.isoformat(),
            "confirmed_at": now.isoformat(),
            "held_seconds": int(held),
        }

    return _settle_if_proven(
        pin, session_id, saved, original_result, proof_for=proof_for
    )


def _record_presence(pin, saved):
    """Break an open gone run when the recorded guest answers live."""
    with controls._locked_session() as (db, _control):
        records = _records(db, pin)
        if not records or records[-1][0] != "cessation_absence":
            return
        _audit(
            db,
            pin,
            "cessation_presence",
            identity_sha256=saved["identity"]["identity_sha256"],
            guest_id=saved["guest_id"],
            observed_at=_now().isoformat(),
            cessation_confirmed=False,
            intervention_required=False,
        )


def _reserve_destroy(pin, session_id, saved):
    """Consume one durable request slot before contacting the control plane."""
    with controls._locked_session() as (db, control):
        records = _records(db, pin)
        identity, _run = _locked_stranded(db, control, pin, session_id)
        _verify(saved, identity)
        requests = [
            detail for action, detail in records if action == "cessation_request"
        ]
        if len(requests) >= MAX_DESTROY_REQUESTS:
            return "exhausted"
        if (
            requests
            and (
                _now() - supervisor._timestamp(requests[-1]["observed_at"])
            ).total_seconds()
            < DESTROY_REQUEST_INTERVAL_SECONDS
        ):
            return "waiting"
        _audit(
            db,
            pin,
            "cessation_request",
            identity_sha256=saved["identity"]["identity_sha256"],
            guest_id=saved["guest_id"],
            request_number=len(requests) + 1,
            observed_at=_now().isoformat(),
            cessation_confirmed=False,
            intervention_required=False,
        )
        return "due"


def _advance_bound(pin, session_id, saved, original_result):
    from factory.execution.transport import EmberSessionGone

    guest_id = saved["guest_id"]
    try:
        view = _get(guest_id)
    except EmberSessionGone:
        return _record_gone(
            pin, session_id, saved, original_result, {"status": "absent"}
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable CP is never absence
        supervisor._note(
            pin, "cessation_observation_unavailable", error=type(exc).__name__
        )
        return "waiting"
    if not isinstance(view, dict) or view.get("session_id") != guest_id:
        supervisor._note(pin, "cessation_wrong_observation")
        return "waiting"
    if view.get("state") in GONE_STATES:
        return _record_gone(
            pin,
            session_id,
            saved,
            original_result,
            {
                "status": "terminal",
                "state": view.get("state"),
                "terminal_reason": view.get("terminal_reason"),
                "updated_at": view.get("updated_at"),
            },
        )
    _record_presence(pin, saved)
    reserved = _reserve_destroy(pin, session_id, saved)
    if reserved == "exhausted":
        supervisor._note(pin, "cessation_destroy_exhausted")
        return "waiting"
    if reserved != "due":
        return "waiting"
    try:
        _destroy(guest_id)
    except EmberSessionGone:
        return _record_gone(
            pin, session_id, saved, original_result, {"status": "absent"}
        )
    except Exception as exc:  # noqa: BLE001 - the next observation decides
        supervisor._note(pin, "cessation_destroy_unconfirmed", error=type(exc).__name__)
    return "waiting"


def advance(
    pin,
    session_id,
    original_result,
    workflow_status,
    *,
    trigger=None,
    actor=ACTOR,
    expected_identity_sha256=None,
):
    """One bounded step of supervised cessation for one exact attempt.

    Returns "settled", "waiting", "refused" or "not_applicable". A live node
    workflow still owns its attempt, so nothing starts before DBOS reports it
    terminal. Without ``trigger`` only an already recorded intent advances.
    Every call leaves one ``factory.cessation.advance`` span carrying the
    outcome and, for not_applicable and refused, the reason.
    """
    with tracer.start_as_current_span(SPAN) as span:
        _span_attributes(span, pin, workflow_status, trigger)
        outcome = _advance(
            span,
            pin,
            session_id,
            original_result,
            workflow_status,
            trigger=trigger,
            actor=actor,
            expected_identity_sha256=expected_identity_sha256,
        )
        return _span_outcome(span, outcome)


def _advance(
    span,
    pin,
    session_id,
    original_result,
    workflow_status,
    *,
    trigger,
    actor,
    expected_identity_sha256,
):
    if workflow_status not in TERMINAL_WORKFLOW_STATUSES:
        span.set_attribute("factory.cessation.reason", "workflow_not_terminal")
        return "not_applicable"
    if type(session_id) is not int:
        span.set_attribute(
            "factory.cessation.reason",
            "session_id_missing" if session_id is None else "session_id_not_int",
        )
        return "not_applicable"
    try:
        saved = _begin(
            pin,
            session_id,
            trigger=trigger,
            actor=actor,
            expected_identity_sha256=expected_identity_sha256,
        )
    except ValueError as exc:
        # Before an intent exists a refusal is only noted for a caller that
        # asked for one; after, every refusal is, because the attempt this
        # path committed to has changed underneath it.
        if trigger is not None or str(exc).startswith("cessation_"):
            supervisor._note(pin, _refusal(exc), error=str(exc))
        span.set_attribute("factory.cessation.reason", _reason_code(str(exc)))
        return "refused"
    if saved is None:
        # _begin named the reason (already_settled or no_intent).
        return "not_applicable"
    span.set_attribute("factory.cessation.shape", saved["shape"])
    try:
        if saved["shape"] == "never_bound":
            return _settle_if_proven(
                pin,
                session_id,
                saved,
                original_result,
                proof_for=lambda _db, _records: {
                    "kind": "never_bound",
                    "workflow_status": workflow_status,
                    "session_status": saved["identity"]["status"],
                    "confirmed_at": _now().isoformat(),
                },
            )
        return _advance_bound(pin, session_id, saved, original_result)
    except ValueError as exc:
        supervisor._note(pin, _refusal(exc), error=str(exc))
        span.set_attribute("factory.cessation.reason", _reason_code(str(exc)))
        return "refused"


def _refusal(exc):
    """One stop_observation reason per refusal code, noted once each."""
    code = str(exc)
    return code if code.startswith("cessation_") else f"cessation_refused_{code}"


def _deadline_due(task: dict) -> bool:
    stamp = task.get("deadline_at")
    if not isinstance(stamp, str):
        return False
    try:
        deadline = supervisor._timestamp(stamp)
    except ValueError:
        return False
    return _now() - deadline >= timedelta(seconds=GRACE_SECONDS)


def _has_intent(pin) -> bool:
    with controls._locked_session() as (db, _control):
        return _intent(_records(db, pin)) is not None


def supervise_task(task: dict, dbos) -> int:
    """Advance every stranded attempt on one task; return how many settled.

    An operator intent advances whatever the flag says: a person asked for
    it. A new intent is recorded here only while the flag is on and the task
    is more than GRACE_SECONDS past its deadline, paused or not. A pause holds
    admission, and this admits nothing: it produces the evidence that lets the
    attempt the pause is holding settle.
    """
    from factory.orchestration.node_workflows import resolve_node_session_id

    task_id = task["task_id"]
    automatic = enabled() and _deadline_due(task)
    settled = 0
    for run in graph.node_runs(task_id):
        if run["status"] != "uncertain":
            continue
        pin = run["pin"]
        if not automatic and not _has_intent(pin):
            # The common silent case: the flag is off or the deadline grace
            # has not passed, and no operator intent exists. Recorded so an
            # attempt held uncertain for hours shows why nothing acted on it.
            with tracer.start_as_current_span(SPAN) as span:
                _span_attributes(span, pin, None, None)
                set_attributes(
                    span,
                    {
                        "factory.cessation.enabled": enabled(),
                        "factory.cessation.deadline_due": _deadline_due(task),
                    },
                )
                _span_outcome(
                    span,
                    "not_applicable",
                    "flag_disabled" if not enabled() else "deadline_grace_pending",
                )
            continue
        state = dbos.get_workflow_status(pin["workflow_id"])
        workflow_status = None if state is None else state.status
        session_id = run.get("session_id")
        if session_id is None:
            with Session(controls.get_engine()) as db:
                session_id = resolve_node_session_id(pin, session=db)
        result = json.loads(run.get("outcome_json") or "{}")
        if not isinstance(result, dict):
            result = {}
        if (
            advance(
                pin,
                session_id,
                {**result, "status": "uncertain", "session_id": session_id},
                workflow_status,
                trigger="deadline" if automatic else None,
            )
            == "settled"
        ):
            settled += 1
    return settled


def request_cessation(
    task_id: str,
    node_key: str,
    attempt: int,
    actor: str,
    *,
    expected_identity_sha256: str,
    workflow_status: str,
) -> dict:
    """Operator repair: produce cessation evidence for one exact attempt.

    The #6025 item 2 action, called in process from a backend pod the way
    factory_controls.settle_lost_attempt is. It records the intent under the
    identity the operator reviewed (``read_cessation`` returns it), then takes
    the first step at once. Later steps run on every conductor tick whatever
    FACTORY_ACTIVE_CESSATION_ENABLED says, because a person asked for this
    attempt, until the recorded guest has been observed gone for the sampled
    window and the attempt settles. The receipt is never cancelled: only this
    attempt is settled, and normal reconciliation then decides retry,
    re-plan or escalation under max_attempts.

    Confirm the node's DBOS workflow is terminal and pass its status: this
    path, like settle_lost_attempt, has no DBOS handle and refuses a live
    workflow. There is no HTTP route.
    """
    pin, session_id, result = _attempt(task_id, node_key, attempt)
    outcome = advance(
        pin,
        session_id,
        result,
        workflow_status,
        trigger="operator",
        actor=controls._text(actor, "actor"),
        expected_identity_sha256=expected_identity_sha256,
    )
    return {"ok": outcome in {"settled", "waiting"}, "state": outcome}


def read_cessation(task_id: str, node_key: str, attempt: int) -> dict:
    """The fingerprinted identity an operator reviews before request_cessation."""
    pin, session_id, _result = _attempt(task_id, node_key, attempt)
    with controls._locked_session() as (db, control):
        identity, _run = _locked_stranded(db, control, pin, session_id)
        guest_id, shape = _target(db, pin, identity)
        return {**identity, "target_guest_id": guest_id, "shape": shape}


def _attempt(task_id, node_key, attempt):
    from factory.orchestration.node_workflows import resolve_node_session_id

    run = next(
        (
            row
            for row in graph.node_runs(task_id, node_key)
            if row["attempt"] == attempt
        ),
        None,
    )
    if run is None:
        raise ValueError("unknown_attempt")
    pin = run["pin"]
    session_id = run.get("session_id")
    if session_id is None:
        with Session(controls.get_engine()) as db:
            session_id = resolve_node_session_id(pin, session=db)
    if session_id is None:
        raise ValueError("missing_factory_session")
    result = json.loads(run.get("outcome_json") or "{}")
    if not isinstance(result, dict):
        result = {}
    return pin, session_id, {**result, "status": "uncertain", "session_id": session_id}
