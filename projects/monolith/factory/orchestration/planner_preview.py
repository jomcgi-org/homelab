"""Default-off, unregistered planner read adapter, with no operator authority.

No route or MCP tool exposes this contract today. A future serving path must
resolve request identity through the normal verified-principal context and
implement the binding verifier before enabling it. Never accept caller IDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypedDict

from auth.api import Authority, Principal, PrincipalKind, current_principal
from core.db import get_engine
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from factory.orchestration import factory_conductor as conductor
from factory.orchestration.factory_models import FactoryPlannerPreview, FactoryReceipt
from factory.orchestration.models import SwarmNodeRun, SwarmTask
from factory.orchestration.turn_artifact import schema_errors


@dataclass(frozen=True)
class PlannerBinding:
    """An exact run of one task, resolved only by a server verifier."""

    task_id: str
    planner_run_id: int


class PreviewRefusal(TypedDict):
    advisory: bool
    ok: bool
    refusal: dict[str, str]


def verified_planner_binding(principal: Principal) -> PlannerBinding | None:
    """Delegated task authority is neither minted nor verified in production.

    Scope strings, groups, standing operators and workload names cannot stand
    in for a verified binding. Tests inject a verifier, never a caller grant.
    """
    return None


def _refusal(code: str) -> PreviewRefusal:
    return {"advisory": True, "ok": False, "refusal": {"code": code, "detail": code}}


def _active_binding(db: Session, binding: PlannerBinding) -> bool:
    if (
        not isinstance(binding, PlannerBinding)
        or type(binding.planner_run_id) is not int
        or binding.planner_run_id <= 0
        or not isinstance(binding.task_id, str)
    ):
        return False
    run = db.get(SwarmNodeRun, binding.planner_run_id)
    task = db.get(SwarmTask, binding.task_id)
    receipts = db.exec(
        select(FactoryReceipt).where(FactoryReceipt.task_id == binding.task_id)
    ).all()
    return bool(
        run is not None
        and run.task_id == binding.task_id
        and re.fullmatch(r"conductor_[0-9]+", run.node_key)
        and run.status in ("admitted", "dispatched")
        and run.finished_at is None
        and task is not None
        and task.start_state == "factory"
        and task.settled_at is None
        and len(receipts) == 1
        and receipts[0].state == "admitted"
        and not receipts[0].task_paused
        and not receipts[0].cancellation_requested
    )


def preview_planner_decision(
    decision: dict, *, expected_revision: int
) -> conductor.DecisionPreview | PreviewRefusal:
    """Read the caller's own proposal, without granting submission authority.

    Every enabled, bound, schema-valid call reaching the revision check spends
    a slot, including stale revisions and projection refusals. Retries spend a
    new slot. A unique insert, rather than a read/count/write, bounds races.
    Submission does not consume or trust this ledger and revalidates live state.
    """
    if not conductor.planner_preview_enabled():
        return _refusal("preview_disabled")
    principal = current_principal()
    if (
        principal.authority != Authority.DELEGATED
        or principal.kind != PrincipalKind.WORKLOAD
        or not principal.subject
        or not principal.issuer
    ):
        return _refusal("planner_binding_unavailable")
    binding = verified_planner_binding(principal)
    if binding is None:
        return _refusal("planner_binding_unavailable")
    if type(expected_revision) is not int:
        return _refusal("validation_failed")
    if schema_errors(decision, conductor.DECISION_SCHEMA) or decision.get(
        "action"
    ) not in ("plan", "add_node"):
        return _refusal("validation_failed")
    with Session(get_engine()) as db:
        if not _active_binding(db, binding):
            return _refusal("planner_binding_inactive")
        for ordinal in (1, 2):
            try:
                with db.begin_nested():
                    db.execute(
                        insert(FactoryPlannerPreview).values(
                            planner_run_id=binding.planner_run_id, ordinal=ordinal
                        )
                    )
            except IntegrityError:
                # Only the exact slot collision is a used slot. Do not swallow
                # foreign-key failures or turn post-commit errors into refusal.
                occupied = db.exec(
                    select(FactoryPlannerPreview).where(
                        FactoryPlannerPreview.planner_run_id == binding.planner_run_id,
                        FactoryPlannerPreview.ordinal == ordinal,
                    )
                ).first()
                if occupied is None:
                    raise
                continue
            db.commit()
            break
        else:
            return _refusal("preview_limit_reached")
    return conductor.preview_decision(
        binding.task_id, decision, expected_revision=expected_revision
    )
