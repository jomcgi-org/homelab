"""Read-only candidate progress/backstop policy, never execution authority.

The production collector can prove control constraints and wait states today.
Semantic evidence needs independent result validators before live activation;
worker summaries, command names and commit counts do not provide that proof.
The pure classifier's fixtures specify that future boundary explicitly.
"""

import hashlib
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import or_
from sqlmodel import select

from factory.execution.models import AgentSession, AgentTurn
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_funding_limits import (
    OBJECTIVE_CEILING_USD,
    objective_task_ids,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryStart
from factory.orchestration.models import SwarmNodeRun, SwarmTask

ACTION = "progress_shadow_assessed"
# Proposed list-price-equivalent maximums, not allocations or enabled limits.
SESSION_BACKSTOP_USD = 500.0
OBJECTIVE_BACKSTOP_USD = 1000.0
SEMANTIC_KINDS = frozenset(
    {
        "reproduction",
        "hypothesis_eliminated",
        "test_result",
        "review_resolved",
        "constraint",
        "next_action",
    }
)


@dataclass(frozen=True)
class VerifiedEvidence:
    """Construct only after verifying the result at its source, never from text.

    subject identifies a test, hypothesis, finding or constraint, not a node.
    result includes negative evidence. next_action must name the concrete
    decision it changes. source is a durable receipt, not a worker's claim.
    """

    kind: str
    subject: str
    result: str
    next_action: str
    source: str

    def signature(self):
        return (self.kind, self.subject, self.result, self.next_action)


def classify(evidence, *, authority_blocked=False, waiting=False):
    """Five-way deterministic shadow judgment, independent of spend/commits."""
    if authority_blocked:
        return {
            "classification": "authority_blocked",
            "reason": "operator or policy authority is required",
        }
    if waiting:
        return {
            "classification": "waiting",
            "reason": "an admitted attempt or uncertain outcome still needs reconciliation",
        }
    verified = [
        item
        for item in evidence
        if isinstance(item, VerifiedEvidence)
        and item.kind in SEMANTIC_KINDS
        and all((item.subject, item.result, item.next_action, item.source))
    ]
    if not verified:
        return {
            "classification": "insufficient_evidence",
            "reason": "no independently verified decision-changing evidence",
        }
    last = verified[-1]
    # A changed node label, a different source ID, or a new commit cannot turn
    # the same experiment and unchanged next action into progress.
    repeated = last.signature() in {item.signature() for item in verified[:-1]}
    return {
        "classification": "stalled" if repeated else "progressing",
        "reason": "same evidence and next action repeated"
        if repeated
        else "verified evidence changes the next decision",
        "evidence_source": last.source,
        "evidence_kind": last.kind,
    }


def _amount(value):
    return (
        float(value)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def price_projection(starts, turns, *, session_ids=(), supervision_list_usd=0.0):
    """Deduplicate native turns; keep reservations and unpriced work visible.

    AgentTurn.list_cost_usd already includes drain continuations. Provider
    reports can themselves be list estimates, and never imply marginal cash.
    Unknown exposure is not priced spend and is never silently refunded.
    """
    unique = {(turn.session_id, turn.seq): turn for turn in turns}
    by_session = defaultdict(list)
    for turn in unique.values():
        by_session[turn.session_id].append(turn)
    owners = defaultdict(list)
    for start in starts:
        owners[start.session_id].append(start)
    sessions = []
    unknown = 0.0
    for session_id in sorted(
        set(session_ids) | set(by_session) | {key for key in owners if key is not None}
    ):
        session_turns = by_session[session_id]
        listed = [_amount(turn.list_cost_usd) for turn in session_turns]
        reported = [_amount(turn.cost_usd) for turn in session_turns]
        known = sum(value for value in listed if value is not None)
        held = sum(
            max(start.max_cost_usd, start.cost_usd or 0)
            for start in owners[session_id]
            if start.accounting_basis
            not in ("no_model_post", "capacity_denied", "no_session_created")
            and (
                start.status in ("reserved", "uncertain")
                or not listed
                or None in listed
            )
        )
        unknown += held
        sessions.append(
            {
                "session_id": session_id,
                "known_list_usd": round(known, 6),
                "provider_reported_usd": round(
                    sum(value for value in reported if value is not None), 6
                )
                if any(value is not None for value in reported)
                else None,
                "unpriced_turns": sum(value is None for value in listed),
                "missing_turns": not session_turns,
                "unbounded_unknown_usage": (not listed or None in listed) and held == 0,
                "unknown_or_reserved_exposure_usd": round(held, 6),
                "proposed_backstop_usd": SESSION_BACKSTOP_USD,
                "observed_backstop_exceeded": known >= SESSION_BACKSTOP_USD,
            }
        )
    unknown += sum(
        controls._committed_cost(start)
        for start in owners[None]
        if start.accounting_basis
        not in ("no_model_post", "capacity_denied", "no_session_created")
    )
    known = sum(item["known_list_usd"] for item in sessions) + supervision_list_usd
    return {
        "sessions": sessions,
        "known_list_usd": round(known, 6),
        "supervision_list_usd": round(supervision_list_usd, 6),
        "unknown_or_reserved_exposure_usd": round(unknown, 6),
        "proposed_objective_backstop_usd": OBJECTIVE_BACKSTOP_USD,
        "observed_objective_backstop_exceeded": known >= OBJECTIVE_BACKSTOP_USD,
        "known_cash_cost_usd": None,
        "subscription_quota": None,
        "coverage": "attributed factory sessions and recorded watchdog assessments only; external sessions, cash billing and quota are not verified",
        "hard_stop_enforced": False,
        "within_backstop_verified": False,
    }


def _detail(row):
    try:
        value = json.loads(row.detail_json)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def snapshot(db, receipt, policy):
    """Use the existing objective owner across generations and readmissions."""
    task_ids = objective_task_ids(db, receipt)
    starts = db.exec(
        select(FactoryStart).where(FactoryStart.task_id.in_(task_ids))
    ).all()
    runs = db.exec(select(SwarmNodeRun).where(SwarmNodeRun.task_id.in_(task_ids))).all()
    tasks = db.exec(select(SwarmTask).where(SwarmTask.id.in_(task_ids))).all()
    session_ids = {
        row.session_id for row in [*starts, *runs, *tasks] if row.session_id is not None
    }
    # The same local-session links as #6716, including sessions whose response
    # was lost before the start/run association could be written.
    if task_ids:
        session_ids.update(
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
    turns = db.exec(
        select(AgentTurn).where(AgentTurn.session_id.in_(session_ids))
    ).all()
    audits = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id.in_(task_ids),
            FactoryAudit.action.in_(
                ("conductor_rejected", "dispatch_refused", "watchdog_assessed")
            ),
        )
        .order_by(FactoryAudit.id)
    ).all()
    supervision = sum(
        _amount(_detail(row).get("cost_usd")) or 0
        for row in audits
        if row.action == "watchdog_assessed"
    )
    facts = []
    for row in audits:
        detail = _detail(row)
        code = detail.get("refusal_code")
        if (
            row.task_id == receipt.task_id
            and row.action != "watchdog_assessed"
            and isinstance(code, str)
            and code
        ):
            facts.append(
                VerifiedEvidence(
                    "constraint",
                    code,
                    "server refused the requested action",
                    "repair the refused precondition before retrying",
                    f"factory_audit:{row.id}",
                )
            )
    active = any(row.status in ("admitted", "dispatched", "uncertain") for row in runs)
    active |= any(row.status in ("reserved", "uncertain") for row in starts)
    return {
        "mode": "shadow",
        "objective_task_ids": task_ids,
        "progress": classify(
            facts, authority_blocked=receipt.task_paused, waiting=active
        ),
        "prices": price_projection(
            starts, turns, session_ids=session_ids, supervision_list_usd=supervision
        ),
        "binding_limits": {
            "task_budget_usd": policy["task_budget_usd"],
            "objective_budget_usd": OBJECTIVE_CEILING_USD,
            "max_task_turns_hard": controls.task_turn_ceiling(policy),
            "turn_budget_usd": policy["turn_budget_usd"],
        },
        "activation_blockers": [
            "incremental authoritative list-price metering and bounded stop latency are missing",
            "semantic evidence validators for reproduction, tests, hypotheses and review findings are not connected",
            "complete objective attribution and cash/quota coverage are not established",
            "existing task, objective, turn and per-node limits require explicit supported amendments",
        ],
        "watermarks": {
            "run": max((row.id for row in runs), default=0),
            "audit": max((row.id for row in audits), default=0),
        },
    }


def observe(db, receipt, policy):
    """Opt-in audit-only observation; never call a mutation or model endpoint."""
    if os.getenv("FACTORY_PROGRESS_SHADOW_ENABLED", "false").lower() != "true":
        return None
    result = snapshot(db, receipt, policy)
    fingerprint = hashlib.sha256(
        json.dumps(result, sort_keys=True).encode()
    ).hexdigest()
    previous = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == receipt.task_id,
            FactoryAudit.action == ACTION,
        )
        .order_by(FactoryAudit.id.desc())
    ).first()
    if previous is None or _detail(previous).get("fingerprint") != fingerprint:
        db.add(
            FactoryAudit(
                actor="factory:progress-shadow",
                action=ACTION,
                task_id=receipt.task_id,
                detail_json=json.dumps(
                    {"fingerprint": fingerprint, **result}, sort_keys=True
                ),
            )
        )
        db.commit()
    return result
