"""GitHub receipt deduplication and single-task admission, without network calls."""

from __future__ import annotations

import json

from sqlalchemy import or_
from sqlmodel import Session, select

from swarm.factory_controls import (
    _audit,
    _integer,
    _json,
    _locked_session,
    _now,
    _snapshot,
    _text,
    DELIVER,
    REFINE,
    intake_policy,
    normalize_repo,
    validate_policy,
)
from swarm.factory_models import FactoryReceipt
from swarm.models import SwarmTask, mint_task_id

INTAKE_ACTOR = "factory:intake"


def receive_issue(
    repo: str,
    issue_number: int,
    title: str,
    body: str,
    url: str,
    actor: str,
    *,
    generation: int = 0,
    kind: str = DELIVER,
    session: Session | None = None,
) -> dict:
    """Store one bounded issue snapshot. A duplicate can never replace its text.

    Generation is an operator-authorized recurrence identity, not an issue field.
    Receipt creation grants no execution authority; admit_next checks the policy.
    """
    repo = normalize_repo(repo)
    issue_number = _integer(issue_number, "issue_number", 1, 2**31 - 1)
    generation = _integer(generation, "generation", 0, 2**31 - 1)
    actor = _text(actor, "actor")
    if kind not in (DELIVER, REFINE):
        raise ValueError("invalid kind")
    title = _text(title, "title", 512)
    if not isinstance(body, str) or len(body) > 65536:
        raise ValueError("invalid body")
    if (
        not isinstance(url, str)
        or url.lower() != f"https://github.com/{repo}/issues/{issue_number}"
    ):
        raise ValueError("url must identify the received GitHub issue")
    with _locked_session(session) as (db, _control):
        existing = db.exec(
            select(FactoryReceipt).where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.issue_number == issue_number,
                FactoryReceipt.generation == generation,
            )
        ).first()
        if existing is not None:
            return {"ok": True, "created": False, "receipt": _snapshot(db, existing)}
        row = FactoryReceipt(
            repo=repo,
            issue_number=issue_number,
            generation=generation,
            title=title,
            body=body,
            url=url,
            actor=actor,
            kind=kind,
        )
        db.add(row)
        db.flush()
        _audit(
            db,
            actor,
            "receive_issue",
            receipt_id=row.id,
            repo=repo,
            issue_number=issue_number,
            generation=generation,
            kind=kind,
        )
        return {"ok": True, "created": True, "receipt": _snapshot(db, row)}


def concurrency_limit(policy: dict) -> int:
    """Tasks the factory may have in flight: policy max_tasks under the chart cap."""
    from swarm.config import factory_max_concurrent_tasks

    return max(1, min(int(policy["max_tasks"]), factory_max_concurrent_tasks()))


def admit_next(actor: str, *, session: Session | None = None) -> dict:
    """Atomically reserve one WIP slot and pin the operator policy to a new SwarmTask.

    max_tasks bounds tasks in flight, not tasks ever admitted: an autonomous
    intake must keep admitting as tasks settle. Total spend per generation is
    bounded by the receipts it can hold (one per repo/issue/generation, never
    returned to queued) times task_budget_usd, not by a counter a human has
    to re-arm. admitted_count is kept for status only.
    """
    actor = _text(actor, "actor")
    with _locked_session(session) as (db, control):
        if control.state != "enabled":
            return {"ok": False, "reason": control.state}
        policy = validate_policy(json.loads(control.policy_json))
        limit = concurrency_limit(policy)
        active = db.exec(
            select(FactoryReceipt)
            .where(FactoryReceipt.state.in_(("admitted", "uncertain")))
            .order_by(FactoryReceipt.id)
        ).all()
        if len(active) >= limit:
            return {
                "ok": False,
                "reason": "wip_limit",
                "task_id": active[0].task_id,
                "active_task_ids": [row.task_id for row in active],
                "active": len(active),
                "limit": limit,
            }
        eligible = FactoryReceipt.issue_number.in_(policy["issue_numbers"])
        if intake_policy(policy)["enabled"]:
            # Intake receipts are not in the operator allowlist by construction.
            # They are admissible only while intake is on, so turning intake off
            # leaves the allowlist exactly as it was.
            eligible = or_(eligible, FactoryReceipt.actor == INTAKE_ACTOR)
        row = db.exec(
            select(FactoryReceipt)
            .where(
                FactoryReceipt.state == "queued",
                FactoryReceipt.repo == policy["repo"],
                eligible,
                FactoryReceipt.generation == policy["generation"],
            )
            .order_by(FactoryReceipt.created_at, FactoryReceipt.id)
        ).first()
        if row is None:
            return {"ok": False, "reason": "no_eligible_issue"}
        task_id = mint_task_id()
        task = SwarmTask(
            id=task_id,
            task_text=f"GitHub issue {row.url}\n\n{row.title}\n\n{row.body}",
            repo=policy["repo"],
            base_branch=policy["base_branch"],
            conductor_model=policy["conductor_model"],
            budget_usd=policy["task_budget_usd"],
            workflow_id=f"factory:{task_id}",
            start_state="factory",
            start_triggered_by=actor,
        )
        db.add(task)
        db.flush()
        row.task_id, row.state, row.policy_json = task_id, "admitted", _json(policy)
        row.updated_at = _now()
        control.admitted_count += 1
        control.updated_at = _now()
        db.add(row)
        db.add(control)
        _audit(
            db,
            actor,
            "admit_next",
            task_id=task_id,
            receipt_id=row.id,
            policy_version=control.version,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "receipt_id": row.id,
            "policy": policy,
            "receipt": _snapshot(db, row, body=True),
        }
