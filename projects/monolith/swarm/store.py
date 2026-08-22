from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from swarm.models import SwarmDecision


class NoOpenDecision(LookupError):
    pass


class InvalidDecision(ValueError):
    pass


def decision_response(row: SwarmDecision, idempotent: bool) -> dict:
    return {
        "workflow_id": row.workflow_id,
        "node_key": row.node_key,
        "decision": row.decision,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "actor_subject": row.actor_subject,
        "idempotent": idempotent,
    }


def get_open_decision(
    session: Session, workflow_id: str, node_key: str
) -> SwarmDecision | None:
    return session.exec(
        select(SwarmDecision)
        .where(
            SwarmDecision.workflow_id == workflow_id,
            SwarmDecision.node_key == node_key,
            SwarmDecision.decided_at.is_(None),
        )
        .order_by(SwarmDecision.requested_at, SwarmDecision.id)
    ).first()


def open_decision(
    session: Session,
    workflow_id: str,
    node_key: str,
    kind: str,
    options: list[str],
    note: str | None,
) -> SwarmDecision:
    existing = get_open_decision(session, workflow_id, node_key)
    if existing is not None:
        return existing

    row = SwarmDecision(
        workflow_id=workflow_id,
        node_key=node_key,
        kind=kind,
        options=list(options),
        note=note,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = get_open_decision(session, workflow_id, node_key)
        if existing is not None:
            return existing
        raise
    session.refresh(row)
    return row


def record_decision(
    session: Session,
    workflow_id: str,
    node_key: str,
    decision: str,
    note: str | None,
    actor_subject: str | None,
    actor_authority: str | None,
) -> SwarmDecision:
    row = get_open_decision(session, workflow_id, node_key)
    if row is None:
        latest = session.exec(
            select(SwarmDecision)
            .where(
                SwarmDecision.workflow_id == workflow_id,
                SwarmDecision.node_key == node_key,
            )
            .order_by(SwarmDecision.requested_at.desc(), SwarmDecision.id.desc())
        ).first()
        if latest is not None and latest.decision == decision:
            return latest
        raise NoOpenDecision(
            f"No open decision for workflow {workflow_id} node {node_key}"
        )
    if decision not in row.options:
        raise InvalidDecision(f"Decision {decision!r} is not one of {row.options!r}")

    row.decision = decision
    row.decision_note = note
    row.actor_subject = actor_subject
    row.actor_authority = actor_authority
    row.decided_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def expire_decision(
    session: Session, workflow_id: str, node_key: str
) -> SwarmDecision | None:
    row = get_open_decision(session, workflow_id, node_key)
    if row is None:
        return None
    row.decision = "expired"
    row.decided_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def list_open_decisions(session: Session, workflow_id: str) -> list[SwarmDecision]:
    return list(
        session.exec(
            select(SwarmDecision)
            .where(
                SwarmDecision.workflow_id == workflow_id,
                SwarmDecision.decided_at.is_(None),
            )
            .order_by(SwarmDecision.requested_at, SwarmDecision.id)
        ).all()
    )
