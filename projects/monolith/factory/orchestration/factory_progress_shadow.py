"""Objective accounting retained from the retired progress shadow.

The runtime meter consumes attribution and outstanding exposure. Candidate
classifications and unused price reports no longer produce audit rows.
"""

import math
from collections import defaultdict

from sqlalchemy import or_
from sqlmodel import select

from factory.execution.models import AgentSession, AgentTurn
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_funding_limits import objective_task_ids
from factory.orchestration.factory_models import FactoryAudit, FactoryStart
from factory.orchestration.models import SwarmNodeRun, SwarmTask


def _amount(value):
    return (
        float(value)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def exposure(starts, turns):
    """Unknown outcomes and unpriced work retain their original reservations."""
    prices = defaultdict(list)
    for turn in turns:
        prices[turn.session_id].append(_amount(turn.list_cost_usd))
    return round(
        sum(
            max(start.max_cost_usd, start.cost_usd or 0)
            for start in starts
            if start.accounting_basis
            not in ("no_model_post", "capacity_denied", "no_session_created")
            and (
                start.session_id is None
                or start.status in ("reserved", "uncertain")
                or not prices[start.session_id]
                or None in prices[start.session_id]
            )
        ),
        6,
    )


def snapshot(db, receipt, *, session_ids=()):
    """Attribute native turns once, including lost start/run associations."""
    task_ids = objective_task_ids(db, receipt)
    starts = db.exec(
        select(FactoryStart).where(FactoryStart.task_id.in_(task_ids))
    ).all()
    sessions = set(session_ids) | {start.session_id for start in starts}
    sessions.update(
        db.exec(
            select(SwarmNodeRun.session_id).where(SwarmNodeRun.task_id.in_(task_ids))
        ).all()
    )
    sessions.update(
        db.exec(select(SwarmTask.session_id).where(SwarmTask.id.in_(task_ids))).all()
    )
    if task_ids:
        sessions.update(
            db.exec(
                select(AgentSession.id).where(
                    or_(
                        *(
                            AgentSession.local_session_id.startswith(
                                f"factory:{task_id}:", autoescape=True
                            )
                            for task_id in task_ids
                        )
                    )
                )
            ).all()
        )
    sessions.discard(None)
    # Scalar reads keep transcripts and artifacts out of serial reconciliation.
    turns = db.exec(
        select(AgentTurn.session_id, AgentTurn.seq, AgentTurn.list_cost_usd).where(
            AgentTurn.session_id.in_(sessions)
        )
    ).all()
    supervision = db.exec(
        select(FactoryAudit).where(
            FactoryAudit.task_id.in_(task_ids),
            FactoryAudit.action == "watchdog_assessed",
        )
    ).all()
    return {
        "objective_task_ids": task_ids,
        "turns": turns,
        "supervision_list_usd": sum(
            _amount(controls.audit_detail(row).get("cost_usd")) or 0
            for row in supervision
        ),
        "unknown_or_reserved_exposure_usd": exposure(starts, turns),
    }
