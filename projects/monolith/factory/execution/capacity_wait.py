"""Durable physical preflight for an existing factory session.

Factory still owns pending prompts, logical permits and task policy. This module
holds no worker lease across polls and never creates an execution receipt.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from faas.embervm_client import EmberVMTransportError
from opentelemetry import metrics
from sqlmodel import Session, select

from core.clock import aware
from factory.execution.models import AgentSession, PendingMessage

_meter = metrics.get_meter("factory.capacity_wait")
_waits = _meter.create_counter("factory.capacity_wait.transitions")
_starts = _meter.create_counter("factory.capacity_wait.invocation_starts")


def get_engine():
    # Use the pending-lane store's DB owner, including its test seam.
    from factory.execution import store

    return store.get_engine()


def enabled() -> bool:
    return os.getenv("FACTORY_ASYNC_RESIDENCY_ENABLED", "").lower() in {"1", "true"}


def _read(session_id: int, *, include_claimed: bool = False) -> dict | None:
    with Session(get_engine()) as db:
        agent = db.get(AgentSession, session_id)
        pending = db.exec(
            select(PendingMessage)
            .where(PendingMessage.session_id == session_id)
            .order_by(PendingMessage.seq)
        ).first()
        if (
            agent is None
            or pending is None
            or (pending.claimed_by_replica and not include_claimed)
        ):
            return None
        if (
            not agent.local_session_id.startswith("factory:")
            or not agent.ember_session_id
        ):
            return None
        context = (
            json.loads(pending.capacity_intent_json)
            if pending.capacity_intent_json
            else None
        )
        from factory.execution import store

        retry_allowed = store._retry_permission(
            store.get_turn(db, session_id, pending.seq), pending
        )
        continuation = bool(
            context
            and context.get("claimed_dispatch_count") == pending.dispatch_count
            and retry_allowed
        )
        legacy_started_at = (
            pending.last_dispatch_at if context is None and retry_allowed else None
        )
        return {
            "continuation": continuation,
            "started_at": pending.capacity_started_at,
            "legacy_started_at": legacy_started_at,
            "legacy_recovery": context is None
            and store._attempted(pending)
            and not retry_allowed,
            "session_id": session_id,
            "seq": pending.seq,
            "local_session_id": agent.local_session_id,
            "guest_id": agent.ember_session_id,
            "token": agent.ember_session_token,
            "lineage_id": agent.ember_lineage_id or agent.ember_session_id,
            "context": context,
            "next_poll": aware(pending.capacity_next_poll_at)
            if pending.capacity_next_poll_at
            else None,
        }


def _save(
    snapshot: dict,
    context: dict,
    *,
    expected_key: str | None,
    started_at: datetime | None = None,
) -> bool:
    # The session lock is the existing pending-lane serialization boundary.
    from factory.execution.store import _lock_session

    with Session(get_engine()) as db:
        agent = _lock_session(db, snapshot["session_id"])
        pending = db.exec(
            select(PendingMessage).where(
                PendingMessage.session_id == snapshot["session_id"],
                PendingMessage.seq == snapshot["seq"],
            )
        ).first()
        if (
            agent is None
            or pending is None
            or pending.claimed_by_replica
            or agent.ember_session_id != snapshot["guest_id"]
        ):
            return False
        current = (
            json.loads(pending.capacity_intent_json)
            if pending.capacity_intent_json
            else {}
        )
        if current.get("key") != expected_key:
            return False
        pending.capacity_intent_json = json.dumps(context)
        if pending.capacity_started_at is None and started_at is not None:
            pending.capacity_started_at = started_at
        pending.capacity_state = context["state"]
        pending.capacity_next_poll_at = datetime.now(timezone.utc) + timedelta(
            seconds=5
        )
        db.add(pending)
        db.commit()
        _waits.add(1, {"state": context["state"]})
        return True


def _task_deadline(local_session_id: str) -> int | None:
    from factory.orchestration.factory_controls import task_snapshot

    fields = local_session_id.split(":")
    if len(fields) != 4:
        return None
    snapshot = task_snapshot(fields[1])
    value = snapshot.get("deadline_at")
    return int(datetime.fromisoformat(value).timestamp() * 1000) if value else None


async def poll(session_id: int, transport, *, allowed: bool) -> bool:
    """One short poll; False leaves the pending turn unclaimed and unchanged."""
    snapshot = await asyncio.to_thread(_read, session_id)
    if snapshot is None:
        return allowed
    if snapshot["legacy_recovery"]:
        # No asynchronous operation exists. Leave an unconfirmed prior attempt
        # to the existing claim/receipt recovery instead of hiding it as waiting.
        return allowed
    context = snapshot["context"]
    if context is None and not enabled():
        return allowed
    if context and context["state"] == "legacy_unsupported":
        return allowed
    if context and not allowed:
        try:
            if not context.get("operation_id") and context.get("enqueue_attempted"):
                # Reattach by key without creating demand after a pause. An
                # original POST still in transit is found on a subsequent poll.
                result = await transport.residency_request(
                    snapshot["guest_id"], snapshot["token"], "GET", key=context["key"]
                )
                if (
                    result["session_id"] != snapshot["guest_id"]
                    or result["key"] != context["key"]
                ):
                    return False
                context["operation_id"] = result["id"]
                if not await asyncio.to_thread(
                    _save, snapshot, context, expected_key=context["key"]
                ):
                    return False
            if context.get("operation_id"):
                await transport.residency_request(
                    snapshot["guest_id"],
                    snapshot["token"],
                    "DELETE",
                    operation_id=context["operation_id"],
                )
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return False
    if not allowed:
        return False
    if context and context["state"] == "ready" and not snapshot["continuation"]:
        return True
    if snapshot["next_poll"] and datetime.now(timezone.utc) < snapshot["next_poll"]:
        return False
    try:
        if snapshot["continuation"] and snapshot["started_at"] is None:
            # A fast interrupted response can release the claim before the
            # ordinary observer sees consumption. Preserve that first execution
            # clock before replacing the only durable operation reference.
            result = await transport.residency_request(
                snapshot["guest_id"],
                snapshot["token"],
                "GET",
                operation_id=context["operation_id"],
            )
            if (
                result.get("state") != "consumed"
                or not context.get("claimed_dispatch_id")
                or result.get("dispatch_id") != context["claimed_dispatch_id"]
                or result.get("session_id") != snapshot["guest_id"]
                or result.get("id") != context["operation_id"]
                or type(result.get("updated_at")) is not int
            ):
                return False
            started = datetime.fromtimestamp(result["updated_at"] / 1000, timezone.utc)
            if not await asyncio.to_thread(
                _save,
                snapshot,
                context,
                expected_key=context["key"],
                started_at=started,
            ):
                return False
        if (
            context is None
            or snapshot["continuation"]
            or context.get("reason")
            in {
                "stale_generation",
                "cancelled",
            }
        ):
            view = await transport.get_session(snapshot["guest_id"], snapshot["token"])
            if (
                view.get("session_id") != snapshot["guest_id"]
                or view.get("lineage_id") != snapshot["lineage_id"]
            ):
                return False
            task_deadline = (
                context["task_deadline"]
                if context
                else await asyncio.to_thread(
                    _task_deadline, snapshot["local_session_id"]
                )
            )
            if (
                task_deadline is None
                or type(view.get("generation")) is not int
                or type(view.get("expires_at")) is not int
            ):
                return False
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            deadline = (
                context["deadline"]
                if context
                else min(task_deadline, view["expires_at"])
            )
            if deadline <= now or deadline > view["expires_at"]:
                return False
            previous_key = context["key"] if context else None
            context = {
                "key": uuid4().hex,
                "operation_id": None,
                "state": "waiting_capacity",
                "generation": view["generation"],
                "deadline": deadline,
                "task_deadline": task_deadline,
                "first_wait_at": context["first_wait_at"] if context else now,
                "reason": "enqueue_pending",
                "enqueue_attempted": False,
            }
            if not await asyncio.to_thread(
                _save,
                snapshot,
                context,
                expected_key=previous_key,
                started_at=snapshot["legacy_started_at"],
            ):
                return False
        if not context.get("operation_id"):
            # Persist uncertainty before POST. A disconnect cannot reopen legacy
            # fallback or mint a new key on the next replica.
            was_attempted = context["enqueue_attempted"]
            context["enqueue_attempted"] = True
            if not await asyncio.to_thread(
                _save, snapshot, context, expected_key=context["key"]
            ):
                return False
            try:
                result = await transport.residency_request(
                    snapshot["guest_id"],
                    snapshot["token"],
                    "POST",
                    key=context["key"],
                    payload={
                        name: context[name]
                        for name in (
                            "generation",
                            "deadline",
                            "task_deadline",
                            "first_wait_at",
                        )
                    },
                )
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code == 409
                    and exc.response.json().get("error") == "stale_generation"
                ):
                    context["reason"] = "stale_generation"
                    await asyncio.to_thread(
                        _save, snapshot, context, expected_key=context["key"]
                    )
                    return False
                # Only the first, authoritative unsupported response permits
                # fallback. A previous ambiguous enqueue remains fenced.
                if (
                    not was_attempted
                    and exc.response.status_code == 501
                    and exc.response.json().get("error")
                    == "async_residency_unsupported"
                ):
                    context["state"] = "legacy_unsupported"
                    return await asyncio.to_thread(
                        _save, snapshot, context, expected_key=context["key"]
                    )
                raise
            context["operation_id"] = result["id"]
        else:
            result = await transport.residency_request(
                snapshot["guest_id"],
                snapshot["token"],
                "GET",
                operation_id=context["operation_id"],
            )
        if (
            result["session_id"] != snapshot["guest_id"]
            or result["generation"] != context["generation"]
            or result["id"] != context["operation_id"]
        ):
            return False
        context.update(
            state="ready" if result["state"] == "ready" else "waiting_capacity",
            reason=result["reason"],
            residency_epoch=result.get("residency_epoch"),
            operation_state=result["state"],
        )
        saved = await asyncio.to_thread(
            _save, snapshot, context, expected_key=context["key"]
        )
        return saved and context["state"] == "ready"
    except (
        httpx.HTTPError,
        EmberVMTransportError,
        ValueError,
        KeyError,
        OverflowError,
    ):
        return False


def handoff_context(pending: PendingMessage) -> dict | None:
    if (
        not pending.capacity_intent_json
        or pending.capacity_state == "legacy_unsupported"
    ):
        return None
    context = json.loads(pending.capacity_intent_json)
    if context["state"] != "ready":
        raise ValueError("Residency is not ready")
    return context


def resolve_not_dispatched(
    session_id: int,
    seq: int,
    owner: str,
    count: int,
    proof: dict,
    receipt_id: str | None,
) -> bool:
    """Release only the exact provisional POST with authoritative negative proof."""
    from factory.execution import admission, store
    from factory.execution.constants import exact_dispatch_id
    from factory.execution.models import AgentResultReceipt

    with Session(get_engine()) as db:
        agent = store._lock_session(db, session_id)
        pending = store.get_pending_message(db, session_id, seq)
        if (
            agent is None
            or pending is None
            or pending.claimed_by_replica != owner
            or pending.dispatch_count != count
        ):
            return False
        context = handoff_context(pending)
        expected_dispatch = exact_dispatch_id(
            session_id, agent.ember_session_id, seq, owner, count
        )
        if (
            context is None
            or proof.get("outcome") != "not_dispatched"
            or proof.get("operation_id") != context["operation_id"]
            or proof.get("dispatch_id") != expected_dispatch
            or proof.get("session_id") != agent.ember_session_id
        ):
            return False
        if receipt_id:
            receipt = db.get(AgentResultReceipt, receipt_id)
            if (
                receipt is None
                or receipt.session_id != session_id
                or receipt.seq != seq
                or receipt.claim_owner != owner
                or receipt.dispatch_count != count
                or receipt.received_at is not None
            ):
                return False
            receipt.superseded_at = datetime.now(timezone.utc)
            receipt.response_observer_released_at = datetime.now(timezone.utc)
            db.add(receipt)
        prior = context["claim_snapshot"]
        previous = store.get_turn(db, session_id, seq)
        if previous is not None and prior["retry_usage"] is not None:
            previous.usage_json = prior["retry_usage"]
            db.add(previous)
        permit = admission.reservation(db, agent.local_session_id, seq)
        if permit is not None:
            if permit.owner != owner or permit.state not in {"reserved", "running"}:
                return False
            permit.owner = None
            permit.state = "reserved"
            db.add(permit)
        pending.dispatch_count = prior["dispatch_count"]
        pending.last_dispatch_at = (
            datetime.fromisoformat(prior["last_dispatch_at"])
            if prior["last_dispatch_at"]
            else None
        )
        pending.capacity_started_at = (
            datetime.fromisoformat(prior["capacity_started_at"])
            if prior.get("capacity_started_at")
            else None
        )
        pending.claimed_at = None
        pending.claimed_by_replica = None
        context.update(state="waiting_capacity", reason=proof["reason"])
        context.pop("claim_snapshot")
        pending.capacity_intent_json = json.dumps(context)
        pending.capacity_state = "waiting_capacity"
        pending.capacity_next_poll_at = datetime.now(timezone.utc) + timedelta(
            seconds=5
        )
        db.add(pending)
        db.commit()
        return True


async def return_before_post(
    session_id: int,
    seq: int,
    owner: str,
    count: int,
    guest_id: str,
    token: str,
    context: dict,
    transport,
    invocation_record: dict,
) -> bool:
    """Return a locally proven unattempted claim without charging an execution.

    The transport publishes attempted before yielding to its first POST. This
    path is only for that caller's pre-POST admission/cancellation boundary.
    """
    from factory.execution.constants import exact_dispatch_id

    if invocation_record.get("attempted"):
        return False
    try:
        await transport.residency_request(
            guest_id, token, "DELETE", operation_id=context["operation_id"]
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return False
    except (httpx.HTTPError, ValueError):
        pass
    proof = {
        "outcome": "not_dispatched",
        "operation_id": context["operation_id"],
        "session_id": guest_id,
        "dispatch_id": exact_dispatch_id(session_id, guest_id, seq, owner, count),
        "reason": "local_not_attempted",
    }
    return await asyncio.to_thread(
        resolve_not_dispatched,
        session_id,
        seq,
        owner,
        count,
        proof,
        invocation_record.get("receipt_id"),
    )


def observe_handoff(session_id: int, *, engine=None) -> None:
    """Resolve the dispatch clock from exact durable CP evidence, never a lease."""
    from factory.execution import store, transport
    from factory.execution.constants import exact_dispatch_id

    with Session(engine if engine is not None else get_engine()) as db:
        agent = db.get(AgentSession, session_id)
        pending = db.exec(
            select(PendingMessage)
            .where(PendingMessage.session_id == session_id)
            .order_by(PendingMessage.seq)
        ).first()
        if (
            agent is None
            or pending is None
            or not pending.claimed_by_replica
            or pending.capacity_started_at is not None
            or not pending.capacity_intent_json
            or pending.capacity_state == "legacy_unsupported"
        ):
            return
        context = json.loads(pending.capacity_intent_json)
        expected = exact_dispatch_id(
            session_id,
            agent.ember_session_id,
            pending.seq,
            pending.claimed_by_replica,
            pending.dispatch_count,
        )
        guest_id, token = agent.ember_session_id, agent.ember_session_token
        seq, owner, count = (
            pending.seq,
            pending.claimed_by_replica,
            pending.dispatch_count,
        )
    try:
        response = httpx.get(
            f"{transport.EMBERVM_URL}/v1/sessions/{guest_id}/residency/{context['operation_id']}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        if (
            result.get("state") != "consumed"
            or result.get("dispatch_id") != expected
            or result.get("session_id") != guest_id
            or result.get("id") != context["operation_id"]
            or type(result.get("updated_at")) is not int
        ):
            return
        started = datetime.fromtimestamp(result["updated_at"] / 1000, timezone.utc)
    except (httpx.HTTPError, ValueError, KeyError, OverflowError):
        return
    with Session(engine if engine is not None else get_engine()) as db:
        agent = store._lock_session(db, session_id)
        pending = store.get_pending_message(db, session_id, seq)
        if (
            agent is None
            or pending is None
            or agent.ember_session_id != guest_id
            or pending.claimed_by_replica != owner
            or pending.dispatch_count != count
            or json.loads(pending.capacity_intent_json or "{}").get("operation_id")
            != context["operation_id"]
        ):
            return
        if pending.capacity_started_at is None:
            pending.capacity_started_at = started
            db.add(pending)
            db.commit()
            _starts.add(1)
