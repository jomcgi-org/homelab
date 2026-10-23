"""Small physical-transport fence for quota scouts.

This module belongs to the session executor's pruned controls closure. It must
not import the conductor, improvement submission service or GitHub inventory
reader. Full GitHub/context review happens at graph admission and publication;
transport rechecks durable ownership, fresh intake/goals and broker quotas.
"""

from __future__ import annotations

import hashlib
import json

from core.clock import aware
from observability.factory_goals import list_active_goals
from sqlalchemy import or_
from sqlmodel import select

from factory.execution.provider_quota import fetch_provider_quota_sync
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_improvement_preview import (
    PreviewPolicy,
    quota_decisions,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
)
from factory.orchestration.models import SwarmNodeRun

ACTOR = "factory:improvement-scout"
TASK_CLASS = "advisory-diagnosis"
REPO = "jomcgi-org/homelab"
SOURCE = "quota_improvement"
MODEL = "sol"
NODE_KEYS = {"scout_collect_1", "scout_review_1"}
MAX_AGE_SECONDS = 900


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def grant_permission(quota: dict, policy: dict, *, now) -> dict:
    """Require every potentially routable Codex account, never a headline."""
    block = controls.problem_issues_policy(policy)
    caps = block["improvement_scout"].get("subscription_caps", {})
    grants = quota.get("grants", {}) if isinstance(quota, dict) else {}
    if (
        not isinstance(grants, dict)
        or not grants
        or any(
            not isinstance(view, dict)
            or view.get("provider") not in {"codex", "claude"}
            for view in grants.values()
        )
    ):
        return {"ok": False, "reason": "scout_grant_inventory_unknown"}
    codex = {name for name, view in grants.items() if view.get("provider") == "codex"}
    decisions = quota_decisions(
        quota,
        now=now,
        policy=PreviewPolicy(max_drafts=1, **block["improvement_limits"]),
    )
    permitting = {
        row["grant"]
        for row in decisions
        if row.get("provider") == "codex" and row.get("eligible")
    }
    if not codex or not codex <= permitting:
        return {
            "ok": False,
            "reason": "scout_all_grants_required",
            "quota_decisions": decisions,
        }
    if any(
        not isinstance(caps.get(name), dict)
        or caps[name].get("subscription_only") is not True
        or caps[name].get("overage_disabled") is not True
        or not caps[name].get("evidence")
        for name in codex
    ):
        return {"ok": False, "reason": "scout_cap_evidence_required"}
    return {
        "ok": True,
        "reason": "scout_all_grants_verified",
        "quota_decisions": decisions,
    }


def _verified_policy(db, receipt, task_id, node_key, attempt):
    if (
        receipt.task_class != TASK_CLASS
        or receipt.repo != REPO
        or receipt.state != "admitted"
        or receipt.task_paused
        or receipt.cancellation_requested
    ):
        return None
    if node_key not in NODE_KEYS or type(attempt) is not int or attempt != 1:
        return None
    control = db.get(FactoryControl, "factory")
    if control is None or control.state != "enabled":
        return None
    policy = json.loads(control.policy_json or "{}")
    block = controls.problem_issues_policy(policy)
    scout = block["improvement_scout"]
    if (
        not block["enabled"]
        or not block["sources"].get(SOURCE)
        or not scout["enabled"]
        or REPO not in controls.enabled_repos(policy)
        or MODEL not in policy.get("allowed_models", [])
        or receipt.issue_number != scout["issue_number"]
        or receipt.generation != policy.get("generation")
    ):
        return None
    marker = f'"receipt_id":{receipt.id}'
    claim = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action == "scout_claim",
            FactoryAudit.actor == ACTOR,
            or_(
                FactoryAudit.detail_json.contains(marker + ","),
                FactoryAudit.detail_json.contains(marker + "}"),
            ),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    detail = json.loads(claim.detail_json) if claim else {}
    if detail.get("receipt_id") != receipt.id or detail.get("policy_digest") != _digest(
        policy
    ):
        return None
    settled = db.exec(
        select(FactoryAudit.id)
        .where(
            FactoryAudit.task_id == task_id,
            FactoryAudit.action.in_(("scout_settled", "improvement_attempt")),
        )
        .limit(1)
    ).first()
    if settled is not None:
        return None
    run = db.exec(
        select(SwarmNodeRun).where(
            SwarmNodeRun.task_id == task_id,
            SwarmNodeRun.node_key == node_key,
            SwarmNodeRun.attempt == attempt,
        )
    ).first()
    if run is None or run.status not in {"admitted", "dispatched", "uncertain"}:
        return None
    pin = json.loads(run.pin_json or "{}")
    if not isinstance(pin, dict) or any(
        (
            run.model != MODEL,
            pin.get("model") != MODEL,
            pin.get("task_id") != task_id,
            pin.get("node_key") != node_key,
            type(pin.get("attempt")) is not int,
            pin.get("attempt") != attempt,
        )
    ):
        return None
    owner = db.exec(
        select(FactoryReceipt.id)
        .where(
            FactoryReceipt.repo == REPO,
            FactoryReceipt.id != receipt.id,
            FactoryReceipt.state.in_(
                ("queued", "admitted", "uncertain", "landing", "escalated")
            ),
        )
        .limit(1)
    ).first()
    if owner is not None or not list_active_goals(db):
        return None
    sweep = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == "intake_evaluated")
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    if (
        sweep is None
        or not 0
        <= (controls._now() - aware(sweep.created_at)).total_seconds()
        <= MAX_AGE_SECONDS
    ):
        return None
    intake = json.loads(sweep.detail_json)
    counts = intake.get("candidates", {})
    if (
        intake.get("github") != "completed"
        or intake.get("truncated") is not False
        or intake.get("generation") != policy.get("generation")
        or not isinstance(counts, dict)
        or type(counts.get("delivery")) is not int
        or type(counts.get("advisory")) is not int
        or counts.get("delivery") != 0
        or counts.get("advisory") != 0
    ):
        return None
    return policy


def transport_allowed(task_id: str, node_key: str, attempt: int) -> bool:
    """Read-only gate called on every wake/retry before physical transport.

    Exceptions propagate to the existing API's fail-closed wrapper. Broker I/O
    runs after the DB read session closes, with no control lock held. A scout's
    own receipt is the sole ownership exclusion; unrelated owners block it.
    """
    with controls._read_session() as db:
        receipt = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).first()
        if receipt is None or receipt.actor != ACTOR:
            return True
        policy = _verified_policy(db, receipt, task_id, node_key, attempt)
    if policy is None:
        return False
    quota = fetch_provider_quota_sync(force=True)
    # A slow broker read cannot carry an earlier enable/ownership decision
    # across a pause, new backlog, goal removal or policy change.
    with controls._read_session() as db:
        receipt = db.exec(
            select(FactoryReceipt).where(
                FactoryReceipt.task_id == task_id,
            )
        ).first()
        if receipt is None or receipt.actor != ACTOR:
            return False
        refreshed = _verified_policy(db, receipt, task_id, node_key, attempt)
    if refreshed is None or _digest(refreshed) != _digest(policy):
        return False
    return grant_permission(quota, refreshed, now=controls._now())["ok"]
