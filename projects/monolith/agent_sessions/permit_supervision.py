"""Settle uncertain permits from exact control-plane cessation evidence.

This loop never invokes or retries a guest. It requests destroy only for an old
parked or banked guest whose Kubernetes node is known to be gone. Factory-owned
sessions have their own settlement path, and a permit whose routine job row is
still parked on this attempt is left to the operator reconciliation path that
re-arms that job. A terminal control-plane timestamp must follow the failed turn
so a historical guest state cannot release a current permit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os

from sqlalchemy import exists, or_, text
from sqlmodel import Session, select

from agent_sessions import admission
from agent_sessions.constants import SYNTHETIC_SESSION_PREFIX, UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
    ProbeObservation,
)
from core.db import get_engine
from swarm.factory_models import FactoryStart
from swarm.models import SwarmNodeRun

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 15
BATCH_SIZE = 4
GET_TIMEOUT_SECONDS = 5
MAX_PROOF_AGE_SECONDS = 30
STALE_UNBOUND_SECONDS = 3600
NODE_GONE_GRACE_SECONDS = 600
MAX_NODE_GONE_DESTROY_REQUESTS = 2
_TERMINAL_SESSION_STATUSES = frozenset({"failed", "warn", "completed", "cancelled"})


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _reason(audit, reason):
    if audit.reason != reason:
        logger.info("Permit supervision permit %s: %s", audit.permit_id, reason)
    audit.reason = reason


def _general_enabled():
    return (
        os.getenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "false").lower()
        == "true"
    )


def _factory_owned(db, agent):
    if agent.local_session_id.startswith("factory:"):
        return True
    return db.exec(
        select(
            or_(
                exists().where(FactoryStart.session_id == agent.id),
                exists().where(
                    SwarmNodeRun.session_id == agent.id,
                    SwarmNodeRun.pin_json.isnot(None),
                ),
            )
        )
    ).one()


# Every durable trace a live binding leaves behind. A no-guest proof means the
# turn never reached a guest, so any of these being set says a binding existed
# and was cleared afterwards, which is not the same claim.
_BINDING_EVIDENCE = (
    "ember_session_token",
    "ember_session_expires_at",
    "ember_lineage_id",
    "prior_ember_lineage_id",
    "cli_session_id",
    "prior_cli_session_id",
    "guest_cleanup_id",
    "guest_cleanup_guest_id",
    "guest_cleanup_workflow_id",
    "guest_cleanup_dispatch_json",
    "guest_cleanup_started_at",
)


def _has_binding_evidence(agent):
    return any(getattr(agent, name) is not None for name in _BINDING_EVIDENCE) or (
        agent.recovery_workspace_loss is True
    )


def _routine_job_held(db, permit):
    """Report whether the routine job row is parked on this exact attempt.

    hold_job_for_unknown_outcome (agent/routine_jobs.py) parks the row with
    last_status=UNKNOWN_INVOCATION, next_run_at NULL and a "session_id=<id>: "
    summary. The only owner that re-arms it is the operator path in
    agent/routine_reconciliation.py, whose delivery_error_hold predicate
    requires the reservation to still be uncertain. Settling the permit here
    would therefore strand the job with no owner able to run it again, so this
    loop leaves held-job permits to that path.
    """
    table = (
        "routine_jobs"
        if db.bind.dialect.name == "sqlite"
        else "claude_agent.routine_jobs"
    )
    row = db.execute(
        text(
            f"SELECT last_status, next_run_at, last_summary FROM {table} "
            "WHERE name = :name"
        ),
        {"name": permit.routine_job_name},
    ).first()
    return bool(
        row is not None
        and row.last_status == UNKNOWN_INVOCATION
        and row.next_run_at is None
        and isinstance(row.last_summary, str)
        and row.last_summary.startswith(f"session_id={permit.session_id}:")
    )


def _no_guest_delivery(permit, recovery):
    if permit.outcome not in {"delivery_error", "executor_cancelled"}:
        raise ValueError("unrecognised_outcome")
    if not isinstance(recovery, dict) or not recovery:
        raise ValueError("missing_recovery")
    dispatch_count = recovery.get("dispatch_count")
    if type(dispatch_count) is not int or dispatch_count < 1:
        raise ValueError("missing_dispatch_identity")
    claim_owner = recovery.get("claim_owner")
    if (
        not isinstance(claim_owner, str)
        or not claim_owner
        or claim_owner != permit.owner
    ):
        raise ValueError("missing_dispatch_identity")
    if not isinstance(recovery.get("last_dispatch_at"), str):
        raise ValueError("missing_dispatch_identity")
    try:
        _timestamp = datetime.fromisoformat(
            recovery["last_dispatch_at"].replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("missing_dispatch_identity") from None
    if any(
        "guest" in str(key).lower() or "binding" in str(key).lower() for key in recovery
    ):
        raise ValueError("guest_delivery_evidence")
    if recovery.get("partial_text") or recovery.get("partial_activities"):
        raise ValueError("guest_delivery_evidence")


def _identity(db, permit, *, allow_stale_unbound=False):
    """Called under the capacity lock, before any observation or settlement."""
    agent = db.exec(
        select(AgentSession)
        .where(AgentSession.id == permit.session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        agent is None
        or permit.state != "uncertain"
        or permit.tier not in {"probe", "kg", "project", "interactive"}
        or agent.admission_tier != permit.tier
        or agent.local_session_id != permit.local_session_id
    ):
        raise ValueError("ineligible_permit")
    if _factory_owned(db, agent):
        raise ValueError("factory_owned")
    if (
        admission.cleanup_pending(db, agent)
        or db.exec(
            select(PendingMessage.id).where(PendingMessage.session_id == agent.id)
        ).first()
        is not None
    ):
        raise ValueError("pending_executor")
    turn = db.exec(
        select(AgentTurn).where(
            AgentTurn.session_id == agent.id,
            AgentTurn.seq == permit.pending_seq,
        )
    ).first()
    # The failed turn must still be the last one. An interactive session
    # reaches here with completed history in front of it, so earlier turns are
    # evidence of a normal conversation rather than of a changed attempt.
    later = db.exec(
        select(AgentTurn.seq)
        .where(AgentTurn.session_id == agent.id, AgentTurn.seq > permit.pending_seq)
        .limit(1)
    ).first()
    stale_unbound_shape = (
        permit.tier in {"kg", "project"}
        and permit.routine_job_name is None
        and agent.status in _TERMINAL_SESSION_STATUSES
        and agent.ember_session_id is None
    )
    if (
        turn is None
        or later is not None
        or (turn.terminal_reason != "error" and not stale_unbound_shape)
    ):
        raise ValueError("changed_attempt")
    if (
        permit.tier != "interactive"
        and db.exec(
            select(AgentTurn.seq)
            .where(AgentTurn.session_id == agent.id, AgentTurn.seq < permit.pending_seq)
            .limit(1)
        ).first()
        is not None
    ):
        # Probe and drainer sessions carry exactly one turn by construction, so
        # history here is a shape this loop does not model, not a race.
        raise ValueError("unsupported_shape")
    if permit.tier == "probe":
        unknown = agent.status == "failed" and turn.stop_reason == UNKNOWN_INVOCATION
        legacy = (
            agent.status == "warn"
            and turn.stop_reason is None
            and permit.outcome == "delivery_error"
        )
        if (
            permit.pending_seq != 1
            or permit.routine_job_name is not None
            or not agent.local_session_id.startswith(SYNTHETIC_SESSION_PREFIX)
            or not (unknown or legacy)
        ):
            raise ValueError("ineligible_probe")
    elif (
        permit.tier in {"kg", "project"}
        and permit.routine_job_name is None
        and agent.status not in _TERMINAL_SESSION_STATUSES
    ):
        # A real drainer session keys on "<workflow>:<node_key>:<job_name>"
        # (swarm/drainer.py _session_key). "_drainer-worker:" is a routine job
        # NAME prefix and never leads a local_session_id, so the routine job
        # is the only selector that reaches these rows.
        raise ValueError("ineligible_drainer")
    if permit.routine_job_name is not None and _routine_job_held(db, permit):
        raise ValueError("routine_job_held")
    if agent.ember_session_id is None:
        if permit.tier == "probe" and not _general_enabled():
            raise ValueError("no_guest_supervision_disabled")
        # store.clear_ember_bindings_by_ember_id now refuses a session holding
        # an unknown outcome, but a binding cleared before that guard, or by
        # another path, still leaves these traces. Without them a cleared
        # binding would read as "no guest was ever bound".
        if _has_binding_evidence(agent):
            raise ValueError("prior_binding_evidence")
        if permit.outcome in {"delivery_error", "executor_cancelled"}:
            # The delivery proof is the fast path. A recognised outcome whose
            # recovery blob is missing or malformed is refused here for the
            # exact no-guest settlement, but the stale unbound path may ask to
            # tolerate that refusal: a terminal session with no binding
            # evidence past its grace is the same claim, reached by time
            # instead of by evidence. _record_stale_unbound applies the age.
            tolerated = allow_stale_unbound and stale_unbound_shape
            try:
                usage = json.loads(turn.usage_json or "{}")
                if not isinstance(usage, dict):
                    raise ValueError("malformed_recovery")
                _no_guest_delivery(permit, usage.get("recovery"))
            except (AttributeError, TypeError) as exc:
                if not tolerated:
                    raise ValueError("malformed_recovery") from exc
            except ValueError as exc:
                if not tolerated:
                    raise ValueError(str(exc) or "malformed_recovery") from exc
        elif not stale_unbound_shape:
            raise ValueError("unrecognised_outcome")
    owners = (
        []
        if agent.ember_session_id is None
        else db.exec(
            select(AgentSession.id)
            .where(AgentSession.ember_session_id == agent.ember_session_id)
            .limit(2)
        ).all()
    )
    reservations = db.exec(
        select(AgentCapacityReservation).where(
            or_(
                AgentCapacityReservation.session_id == agent.id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
    ).all()
    if permit.tier == "interactive":
        # admission.claim_pending reserves a start for every turn and a settled
        # reservation is never deleted, so a third-turn conversation owns three
        # rows. Earlier settled rows are ordinary history. A second live row,
        # or any row at this seq or past it, is a different attempt.
        ambiguous = any(
            row.id != permit.id
            and (row.state != "settled" or row.pending_seq >= permit.pending_seq)
            for row in reservations
        )
    else:
        # Probe and drainer sessions carry exactly one turn by construction, so
        # a second reservation of any state is a shape this loop does not model.
        ambiguous = [row.id for row in reservations] != [permit.id]
    if (agent.ember_session_id is not None and owners != [agent.id]) or ambiguous:
        raise ValueError("ambiguous_ownership")
    # Hash the complete turn to detect intervening evidence without copying
    # prompts, results, artifacts, or credentials into lifecycle audit records.
    identity = _sha(
        {
            "permit": permit.model_dump(),
            "session_id": agent.id,
            "local_session_id": agent.local_session_id,
            "guest_id": agent.ember_session_id,
            "last_turn_at": agent.last_turn_at,
            "status": agent.status,
            "turn": turn.model_dump(),
        }
    )
    return agent, turn, identity


def _candidates():
    tiers = []
    if os.getenv("AGENT_PROBE_SUPERVISION_ENABLED", "false").lower() == "true":
        tiers.append("probe")
    if _general_enabled():
        tiers.extend(("kg", "project", "interactive"))
    if not tiers:
        return []
    with Session(get_engine()) as db:
        return list(
            db.exec(
                select(AgentCapacityReservation.id)
                .join(
                    AgentSession,
                    AgentSession.id == AgentCapacityReservation.session_id,
                )
                .outerjoin(
                    ProbeObservation,
                    ProbeObservation.permit_id == AgentCapacityReservation.id,
                )
                .where(
                    AgentCapacityReservation.tier.in_(tiers),
                    AgentCapacityReservation.state == "uncertain",
                    ~AgentSession.local_session_id.startswith("factory:"),
                    ~exists().where(FactoryStart.session_id == AgentSession.id),
                    ~exists().where(
                        SwarmNodeRun.session_id == AgentSession.id,
                        SwarmNodeRun.pin_json.isnot(None),
                    ),
                    or_(
                        AgentCapacityReservation.tier.in_(("probe", "interactive")),
                        AgentCapacityReservation.routine_job_name.isnot(None),
                        (
                            AgentCapacityReservation.tier.in_(("kg", "project"))
                            & AgentSession.status.in_(_TERMINAL_SESSION_STATUSES)
                        ),
                    ),
                )
                .order_by(
                    ProbeObservation.checked_at.asc().nulls_first(),
                    AgentCapacityReservation.id,
                )
                .limit(BATCH_SIZE)
            ).all()
        )


def _prepare(permit_id):
    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        audit = db.get(ProbeObservation, permit_id)
        if audit is not None and audit.settled_at is not None:
            return None
        if audit is None:
            audit = ProbeObservation(permit_id=permit_id, checked_at=_now())
        audit.checked_at = _now()
        db.add(audit)
        permit = db.get(AgentCapacityReservation, permit_id)
        if permit is None:
            _reason(audit, "permit_missing")
            return None
        try:
            agent, _, identity = _identity(db, permit, allow_stale_unbound=True)
        except ValueError as exc:
            _reason(audit, str(exc))
            return None
        if audit.identity_sha256 not in (None, identity):
            _reason(audit, "identity_changed")
            return None
        audit.identity_sha256 = identity
        audit.guest_id = agent.ember_session_id
        return {
            "permit_id": permit_id,
            "identity": identity,
            "guest_id": audit.guest_id,
        }


def _record_no_guest(candidate):
    """Settle a re-read identity whose durable evidence shows no delivery."""
    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        audit = db.get(ProbeObservation, candidate["permit_id"])
        if audit is None or audit.settled_at is not None:
            return audit.reason if audit is not None else None
        audit.checked_at = _now()
        permit = db.get(AgentCapacityReservation, candidate["permit_id"])
        try:
            if permit is None:
                raise ValueError("permit_missing")
            agent, _turn, identity = _identity(db, permit)
            if (
                identity != candidate["identity"]
                or identity != audit.identity_sha256
                or agent.ember_session_id is not None
            ):
                raise ValueError("identity_changed")
            if permit.outcome not in {"delivery_error", "executor_cancelled"}:
                raise ValueError("unrecognised_outcome")
        except ValueError as exc:
            _reason(audit, str(exc))
            db.add(audit)
            return audit.reason
        admission.settle(
            db,
            agent,
            permit.pending_seq,
            outcome="no_guest_bound",
            cessation_confirmed=True,
        )
        audit.reason = "no_guest_bound"
        audit.settled_at = _now()
        db.add(audit)
        return audit.reason


def _record_stale_unbound(candidate):
    """Settle an old terminal reservation with no trace of a guest binding."""
    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        audit = db.get(ProbeObservation, candidate["permit_id"])
        if audit is None or audit.settled_at is not None:
            return
        audit.checked_at = _now()
        permit = db.get(AgentCapacityReservation, candidate["permit_id"])
        try:
            if permit is None:
                raise ValueError("permit_missing")
            agent, _turn, identity = _identity(db, permit, allow_stale_unbound=True)
            if (
                permit.state != "uncertain"
                or permit.tier not in {"kg", "project"}
                or permit.routine_job_name is not None
                or agent.status not in _TERMINAL_SESSION_STATUSES
                or agent.ember_session_id is not None
                or _has_binding_evidence(agent)
            ):
                raise ValueError("stale_unbound_proof_missing")
            if _aware(permit.created_at) >= _now() - timedelta(
                seconds=STALE_UNBOUND_SECONDS
            ):
                raise ValueError("stale_unbound_grace")
            if identity != candidate["identity"] or identity != audit.identity_sha256:
                raise ValueError("identity_changed")
        except ValueError as exc:
            _reason(audit, str(exc))
            db.add(audit)
            return
        admission.settle(
            db,
            agent,
            permit.pending_seq,
            outcome="stale_unbound_permit",
            cessation_confirmed=True,
        )
        audit.reason = "stale_unbound_permit"
        audit.settled_at = _now()
        db.add(audit)


def _record(candidate, observed, observed_at, node_names=None):
    """Commit proof and exact permit settlement together, or retain the hold."""
    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        audit = db.get(ProbeObservation, candidate["permit_id"])
        if audit is None or audit.settled_at is not None:
            return
        audit.checked_at = _now()
        permit = db.get(AgentCapacityReservation, candidate["permit_id"])
        try:
            if permit is None:
                raise ValueError("permit_missing")
            agent, turn, identity = _identity(db, permit)
            if identity != candidate["identity"] or identity != audit.identity_sha256:
                raise ValueError("identity_changed")
            if not 0 <= (_now() - observed_at).total_seconds() <= MAX_PROOF_AGE_SECONDS:
                raise ValueError("stale_observation")
            if not isinstance(observed, dict):
                raise ValueError("observation_unavailable")
            if observed.get("session_id") != candidate["guest_id"]:
                raise ValueError("guest_mismatch")
            generation, updated = observed.get("generation"), observed.get("updated_at")
            if (
                type(generation) is not int
                or generation < 0
                or type(updated) is not int
                or updated < 0
            ):
                raise ValueError("malformed_identity")
            if (audit.cp_updated_at is not None and updated < audit.cp_updated_at) or (
                updated > int(observed_at.timestamp() * 1000)
            ):
                raise ValueError("reordered_observation")

            try:
                prior_evidence = json.loads(audit.evidence_json or "{}")
            except (TypeError, ValueError):
                prior_evidence = {}
            if not isinstance(prior_evidence, dict):
                prior_evidence = {}
            destroy_requests = prior_evidence.get("node_gone_destroy_requests", 0)
            if type(destroy_requests) is not int or destroy_requests < 0:
                destroy_requests = 0
            evidence = {
                "session_id": candidate["guest_id"],
                "generation": generation,
                "invoke_started_at": observed.get("invoke_started_at"),
                "updated_at": updated,
                "observed_at": observed_at.isoformat(),
                "response_sha256": _sha(observed),
                "terminal_state": observed.get("state")
                if observed.get("state") in {"evicted", "destroyed"}
                else None,
                "last_invoke_at": observed.get("last_invoke_at")
                if type(observed.get("last_invoke_at")) is int
                else None,
                "node_gone_destroy_requests": destroy_requests,
            }
            node = observed.get("node") or {}
            node_id = node.get("node_id") if isinstance(node, dict) else None
            if (
                observed.get("state") in {"parked", "banked"}
                and isinstance(node_id, str)
                and node_id
                and node_names is not None
                and node_id not in node_names
                and int(observed_at.timestamp() * 1000) - updated
                > NODE_GONE_GRACE_SECONDS * 1000
            ):
                audit.generation = generation
                audit.invoke_started_at = (
                    observed.get("invoke_started_at")
                    if type(observed.get("invoke_started_at")) is int
                    else None
                )
                audit.cp_updated_at = updated
                if destroy_requests >= MAX_NODE_GONE_DESTROY_REQUESTS:
                    evidence.update(
                        reason="guest_node_gone_destroy_exhausted",
                        intervention_required=True,
                    )
                    audit.evidence_json = json.dumps(evidence, sort_keys=True)
                    _reason(audit, "guest_node_gone_destroy_exhausted")
                    db.add(audit)
                    return None
                precondition = {"generation": generation}
                evidence.update(
                    node_gone_destroy_requests=destroy_requests + 1,
                    destroy_precondition=precondition,
                    intervention_required=False,
                )
                audit.evidence_json = json.dumps(evidence, sort_keys=True)
                _reason(audit, "node_gone_destroy_requested")
                db.add(audit)
                return precondition

            # A guest that never completed an invoke carries NULL stamps: the
            # control plane initialises both nil and only the invoke path sets
            # them. A guest parked after a 409 on invoke is exactly that, and
            # every invocation-bounding check below reads an int, so such a
            # guest could never settle and held its admission slot forever
            # (#6004). Recognise it only when BOTH stamps are null AND the
            # observed state is already terminal. A live guest with null stamps
            # proves no cessation, and a SINGLE null stamp is a malformed
            # observation rather than a never-invoked one, so both stay
            # rejected. Generation, updated_at ordering and cessation_precedes_turn
            # are still enforced below: what is skipped is only the bounding of
            # an invocation that demonstrably never happened.
            never_invoked = (
                observed.get("state") in {"evicted", "destroyed"}
                and observed.get("invoke_started_at") is None
                and observed.get("last_invoke_at") is None
            )
            fields = ("generation", "updated_at")
            if not never_invoked:
                fields = ("generation", "invoke_started_at", "updated_at")
            if any(type(observed.get(k)) is not int or observed[k] < 0 for k in fields):
                raise ValueError("malformed_identity")
            started = None if never_invoked else observed["invoke_started_at"]
            # Control-plane milliseconds ordered against a monolith-side
            # timestamp. The gap asserted is an invocation's own length, which
            # is far larger than plausible skew between the two clocks.
            if not never_invoked and started > int(
                _aware(turn.created_at).timestamp() * 1000
            ):
                raise ValueError("invoke_after_failed_turn")
            # Idle banking increments generation on the same session. It does
            # not change invoke_started_at. Reject regressions and new invokes.
            if audit.generation is not None and generation < audit.generation:
                raise ValueError("invocation_changed")
            if (
                audit.generation is not None
                and not never_invoked
                and started != audit.invoke_started_at
            ):
                raise ValueError("invocation_changed")
            if not never_invoked and started > updated:
                raise ValueError("reordered_observation")
            audit.generation = generation
            audit.invoke_started_at = started
            audit.cp_updated_at = updated
            evidence["invoke_started_at"] = started
            audit.evidence_json = json.dumps(evidence, sort_keys=True)
            terminal_states = {"evicted"}
            if _general_enabled():
                # Destroyed is trustworthy cessation proof only because
                # nodeConfirmedDestroy: true (projects/embervm/deploy/values.yaml)
                # makes the control plane record "destroyed" only after the
                # owning node has itself confirmed teardown. Probe-only mode
                # stays on eviction alone so it does not depend on that gate.
                terminal_states.add("destroyed")
            if observed.get("state") not in terminal_states:
                raise ValueError("awaiting_cessation")
            if not never_invoked:
                last_invoke = observed.get("last_invoke_at")
                if (
                    type(last_invoke) is not int
                    or not started <= last_invoke <= updated
                ):
                    raise ValueError("missing_invoke_completion")
            # The same cross-clock comparison in the other direction: the gap
            # from recording the failure to the guest ceasing is an eviction or
            # teardown, again far larger than plausible skew.
            if updated <= int(_aware(turn.created_at).timestamp() * 1000):
                raise ValueError("cessation_precedes_turn")
        except ValueError as exc:
            _reason(audit, str(exc))
            db.add(audit)
            return False
        admission.confirm_guest_cessation(db, agent)
        audit.reason = "guest_cessation_confirmed"
        audit.settled_at = _now()
        db.add(audit)
        # Keep the failed turn, cost, and session history unchanged. A later
        # turn receives a fresh guest identity through normal dispatch.


def _record_destroy_failure(candidate, exception):
    """Attach a bounded intervention marker to the already consumed request."""
    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        audit = db.get(ProbeObservation, candidate["permit_id"])
        if audit is None or audit.settled_at is not None:
            return
        try:
            evidence = json.loads(audit.evidence_json or "{}")
        except (TypeError, ValueError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        evidence.update(
            reason="node_gone_destroy_failed",
            exception=type(exception).__name__,
            intervention_required=True,
        )
        audit.evidence_json = json.dumps(evidence, sort_keys=True)
        _reason(audit, "node_gone_destroy_failed")
        db.add(audit)


async def sweep_once(transport):
    for permit_id in await asyncio.to_thread(_candidates):
        try:
            candidate = await asyncio.to_thread(_prepare, permit_id)
            if candidate is None:
                continue
            if candidate["guest_id"] is None:
                reason = await asyncio.to_thread(_record_no_guest, candidate)
                if reason != "no_guest_bound":
                    await asyncio.to_thread(_record_stale_unbound, candidate)
                continue
            try:
                observed = await asyncio.wait_for(
                    transport.get_session(candidate["guest_id"]), GET_TIMEOUT_SECONDS
                )
            except Exception:  # A missing or unavailable guest is not cessation.
                observed = None
            node_names = None
            if isinstance(observed, dict) and observed.get("state") in {
                "parked",
                "banked",
            }:
                from cluster.kubernetes import cluster_node_names

                node_names = await cluster_node_names()
            destroy_precondition = await asyncio.to_thread(
                _record, candidate, observed, _now(), node_names
            )
            if destroy_precondition:
                try:
                    await asyncio.wait_for(
                        # Recorded on the observation, not sent: see
                        # factory_supervision._destroy_guest (#6091, #5502).
                        transport.destroy_session(candidate["guest_id"]),
                        GET_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    await asyncio.to_thread(_record_destroy_failure, candidate, exc)
                    logger.exception(
                        "Failed node-gone destroy for permit %s", permit_id
                    )
        except Exception:  # One failed candidate must not stop the bounded sweep.
            logger.exception("Failed permit observation for permit %s", permit_id)


async def _loop():
    from agent_sessions.transport import EmberVmShimTransport

    transport = EmberVmShimTransport()
    while True:
        try:
            await sweep_once(transport)
        except Exception:
            logger.exception("Failed permit supervision sweep")
        await asyncio.sleep(INTERVAL_SECONDS)


def start_permit_supervision_loop():
    if not any(
        os.getenv(name, "false").lower() == "true"
        for name in (
            "AGENT_PROBE_SUPERVISION_ENABLED",
            "AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED",
        )
    ):
        return []
    from framework import log_task_exception

    task = asyncio.create_task(_loop(), name="agent-permit-supervision")
    task.add_done_callback(log_task_exception)
    return [task]
