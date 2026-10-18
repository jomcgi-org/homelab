"""Atomic automatic pauses and local, non-funding operator questions.

The control lock owns both the pause and the card. Callers already holding it
(including meter callers holding pool/session locks) use pause_locked directly:
this primitive takes no further locks and never performs network I/O.
"""

import json
import os

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryControl, WorkItem

KIND = "automatic_pause"
STOP = {
    "key": "stop",
    "label": "Stop this task, leave the issue open",
    "effect": "hold",
    "detail": {},
}


def is_card(document):
    return isinstance(document, dict) and document.get("kind") == KIND


def eligible(db, row):
    """Never take ownership from cancellation, human handoff or review recovery."""
    if (
        row is None
        or row.state not in controls._ACTIVE
        or not row.task_id
        or row.cancellation_requested
        or controls.review_recovery_held(row.task_id, session=db)
    ):
        return False
    item = db.get(WorkItem, row.work_item_id) if row.work_item_id else None
    return item is None or "human" not in {label.lower() for label in item.labels}


def decidable(row, document):
    return (
        is_card(document)
        and document.get("task_id") == row.task_id
        and row.state in controls._ACTIVE
        and bool(row.task_paused)
        and not row.cancellation_requested
    )


def _identity(row, document):
    return controls.decision_identity(
        {
            "id": row.id,
            "repo": row.repo,
            "generation": row.generation,
            "escalation": document,
        }
    )


def actionable(db, row, document):
    from factory.orchestration import factory_decisions as decisions

    if not eligible(db, row) or not document or document.get("resolved") is not None:
        return False
    if decisions.decision_in_flight(db, row.id):
        return False
    control = db.get(FactoryControl, "factory")
    generation = json.loads(control.policy_json or "{}").get("generation")
    if (
        type(generation) is int
        and row.generation != generation
        and os.getenv("FACTORY_ACTIVE_RECEIPT_DECISIONS_ENABLED", "false").lower()
        != "true"
    ):
        return False
    return (decidable(row, document) and document.get("options") == [STOP]) or (
        document.get("kind") == "watchdog"
        and document.get("task_id") == row.task_id
        and row.task_paused
        and bool(document.get("options"))
    )


def _write_card(db, row, actor, reason, *, body, prior):
    reason = reason[:4000]
    prior_id = _identity(row, prior)
    document = {
        "kind": KIND,
        "task_id": row.task_id,
        "actor": actor,
        "question": f"{actor}: {reason}\n\nThe task is paused. Stop this task for operator recovery?",
        "reason": reason,
        "summary": "The pause remains in force. Continuing or changing funding needs separate authorization. Stopping requests cancellation; unresolved attempts retain their reservations.",
        "recommendation": "stop",
        "options": [STOP],
        "issue_body_sha256": controls.issue_body_hash(body),
        "supersedes_decision_id": prior_id,
        "resolved": None,
        "chat": (prior or {}).get("chat") or [],
        "conversation": (prior or {}).get("conversation") or [],
    }
    row.escalation_json = controls._json(document)
    row.updated_at = controls._now()
    controls._audit(
        db,
        actor,
        "paused_card_superseded" if prior else "paused_card_created",
        task_id=row.task_id,
        receipt_id=row.id,
        generation=row.generation,
        reason=reason,
        prior_decision_id=prior_id,
        prior_card=prior,
        decision_id=_identity(row, document),
        issue_body_sha256=document["issue_body_sha256"],
    )


def pause_locked(db, row, actor, reason):
    """Caller holds control before any task/pool/session locks; no commit here."""
    from factory.orchestration import factory_decisions as decisions

    if not eligible(db, row) or decisions.decision_in_flight(db, row.id):
        return False
    prior = decisions.escalation_of(row)
    if (
        prior
        and row.task_paused
        and prior.get("task_id") == row.task_id
        and controls.terminal_resolution(prior.get("resolved"))
    ):
        return False
    was_paused = row.task_paused
    if not was_paused:
        result = controls.set_control(
            "pause_task", actor, task_id=row.task_id, session=db
        )
        if not result["ok"]:
            return False
    if actionable(db, row, prior) or (
        prior
        and prior.get("task_id") == row.task_id
        and prior.get("resolved") is None
        and (
            (is_card(prior) and prior.get("options") == [STOP])
            or (prior.get("kind") == "watchdog" and bool(prior.get("options")))
        )
    ):
        return not was_paused
    _write_card(db, row, actor, reason, body=row.body, prior=prior)
    return True


def pause(task_id, actor, reason, *, expected_version=None):
    with controls._locked_session() as (db, control):
        if expected_version is not None and control.version != expected_version:
            return False
        return pause_locked(db, controls._receipt(db, task_id), actor, reason)


def refresh(card, body, actor):
    """Compare-and-replace a still-paused card, never an operator answer.

    The version captured before GitHub I/O fences even a resume/pause ABA.
    A claim or durable request owns its brief until its outcome is known.
    """
    from factory.orchestration import factory_decisions as decisions
    from factory.orchestration.factory_models import FactoryReceipt
    from factory.orchestration.factory_progress_watchdog import decidable as watchdog

    with controls._locked_session() as (db, control):
        row = db.get(FactoryReceipt, card["receipt_id"])
        if (
            control.version != card["control_version"]
            or not eligible(db, row)
            or row.task_id != card["task_id"]
            or row.generation != card["generation"]
            or not row.task_paused
            or decisions.decision_in_flight(db, row.id)
        ):
            return False
        if not decisions._pinned_and_active(row):
            try:
                decisions._require_current_generation(db, row, control)
            except decisions.DecisionError:
                return False
        prior = decisions.escalation_of(row)
        if (
            not prior
            or prior.get("resolved") is not None
            or _identity(row, prior) != card["decision_id"]
            or watchdog(row, prior)
            or controls.issue_body_hash(body)
            == (prior.get("issue_body_sha256") or controls.issue_body_hash(row.body))
        ):
            return False
        reason = (
            "Issue body changed while this task was paused; the prior question is superseded. "
            + str(
                prior.get("reason")
                or prior.get("question")
                or "Operator recovery required"
            )
        )
        _write_card(db, row, actor, reason, body=body, prior=prior)
        row.body = body or ""
        return True
