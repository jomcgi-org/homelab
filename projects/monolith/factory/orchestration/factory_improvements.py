"""One reviewed scouting batch into the existing problem-issue audit ledger.

No scouting/model loop: an operator supplies evidence after reviewing a fresh
context. The existing producer is the sole GitHub writer.
"""

from __future__ import annotations

import json
from datetime import timedelta

from core.clock import aware
from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_improvement_preview import (
    PreviewPolicy,
    digest,
    preview,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
)

SOURCE = "quota_improvement"
ATTEMPT = "improvement_attempt"
CANDIDATE = "improvement_candidate"
REPO = "jomcgi-org/homelab"
POLICY = PreviewPolicy(max_drafts=1)


def generation_policy(policy):
    limits = controls.problem_issues_policy(policy)["improvement_limits"]
    return PreviewPolicy(max_drafts=1, **limits)


def _goals(db):
    from observability.factory_goals import list_active_goals

    return list_active_goals(db)


def _quota():
    from factory.execution.provider_quota import fetch_provider_quota_sync

    return fetch_provider_quota_sync(force=True)


def _inventory():
    from factory.orchestration.factory_intake_loop import _pages

    issues, issue_cap = _pages(REPO, "issues")
    prs, pr_cap = _pages(REPO, "pulls")
    if issue_cap or pr_cap:
        raise ValueError("open_work_scan_incomplete")
    rows = []
    for kind, items in (("issue", issues), ("pr", prs)):
        for item in items:
            if not isinstance(item, dict) or type(item.get("number")) is not int:
                raise ValueError("open_work_scan_invalid")
            rows.append(
                {
                    "kind": kind,
                    **{
                        key: item.get(key)
                        for key in (
                            "number",
                            "title",
                            "body",
                            "labels",
                            "assignees",
                            "updated_at",
                            "created_at",
                        )
                    },
                }
            )
    return rows


def _scout_receipt(db, receipt_id):
    if type(receipt_id) is not int or receipt_id <= 0:
        raise ValueError("invalid_scout_receipt")
    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or row.repo != REPO
        or row.actor != "factory:improvement-scout"
        or row.task_class != "advisory-diagnosis"
    ):
        raise ValueError("invalid_scout_receipt")
    claims = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action == "scout_claim",
            FactoryAudit.actor == "factory:improvement-scout",
            (
                FactoryAudit.detail_json.contains(f'"receipt_id":{receipt_id},')
                | FactoryAudit.detail_json.contains(f'"receipt_id":{receipt_id}}}')
            ),
        )
        .limit(2)
    ).all()
    if not any(
        json.loads(claim.detail_json).get("receipt_id") == receipt_id
        for claim in claims
    ):
        raise ValueError("scout_claim_missing")
    return row


def _database_context(db, control, *, scout_receipt_id=None):
    policy = json.loads(control.policy_json or "{}")
    goals = _goals(db)
    active = db.exec(
        select(FactoryReceipt)
        .where(
            FactoryReceipt.repo == REPO,
            FactoryReceipt.state.in_(
                ("queued", "admitted", "uncertain", "landing", "escalated")
            ),
        )
        .order_by(FactoryReceipt.id)
        .limit(1001)
    ).all()
    if scout_receipt_id is not None:
        _scout_receipt(db, scout_receipt_id)
        active = [row for row in active if row.id != scout_receipt_id]
    owners = [
        {
            "receipt_id": row.id,
            "issue_number": row.issue_number,
            "task_id": row.task_id,
            "state": row.state,
        }
        for row in active
    ]
    sweep = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == "intake_evaluated")
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    complete = False
    if sweep is not None:
        detail = json.loads(sweep.detail_json)
        counts = detail.get("candidates") or {}
        age = (controls._now() - aware(sweep.created_at)).total_seconds()
        complete = (
            0 <= age <= POLICY.max_age_seconds
            and detail.get("github") == "completed"
            and detail.get("truncated") is False
            and detail.get("generation") == policy.get("generation")
            and counts.get("delivery") == 0
            and counts.get("advisory") == 0
            and not owners
        )
    return {
        "control_version": control.version,
        "control_state": control.state,
        "repo_enabled": REPO in controls.enabled_repos(policy),
        "policy": policy,
        "goals": goals,
        "owners": owners,
        "intake_complete": complete,
    }


def _review_digest(db_context, inventory):
    # Quota ages change every read; ownership, rules, goals and GitHub content
    # are the review identity. Fresh quotas are independently rechecked.
    return digest({**db_context, "inventory": inventory})


def context(*, scout_receipt_id=None) -> dict:
    """Fresh read-only context for a bounded operator review, no reservations."""
    inventory = _inventory()
    quota = _quota()
    with controls._read_session() as db:
        control = db.get(FactoryControl, "factory")
        current = _database_context(db, control, scout_receipt_id=scout_receipt_id)
        history = _history(db, current["policy"])
    return {
        **current,
        "inventory": inventory,
        "improvement_history": history,
        "quota": quota,
        "review_digest": _review_digest(current, inventory),
        "captured_at": controls._now().isoformat(),
    }


def _history(db, policy):
    review = controls.problem_issues_policy(policy)["improvement_review"]
    reviewed_id = review["through_audit_id"]
    if reviewed_id:
        row = db.get(FactoryAudit, reviewed_id)
        if row is None or row.action != ATTEMPT:
            raise ValueError("improvement_review_requires_exact_attempt_audit")
    rows = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == ATTEMPT)
        .order_by(FactoryAudit.id.desc())
        .limit(20)
    ).all()
    result = []
    for row in rows:
        detail = json.loads(row.detail_json)
        item = {
            "attempted_at": aware(row.created_at).isoformat(),
            **detail,
            "audit_id": row.id,
            "operator_reviewed": row.id <= reviewed_id,
        }
        # Only delivery settlement is useful completion. Issue creation,
        # advisory refinement and ticket counts never reset the trial budget.
        fingerprint = detail.get("fingerprint")
        if fingerprint:
            created = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.action.in_(
                        ("problem_issue_created", "problem_issue_reconciled")
                    ),
                    FactoryAudit.detail_json.contains(f'"fingerprint":"{fingerprint}"'),
                )
                .order_by(FactoryAudit.id.desc())
                .limit(1)
            ).first()
            if created:
                delivered = json.loads(created.detail_json)
                numbers = delivered.get("issue_numbers") or [
                    delivered.get("issue_number")
                ]
                receipts = db.exec(
                    select(FactoryReceipt)
                    .where(
                        FactoryReceipt.repo == REPO,
                        FactoryReceipt.issue_number.in_(
                            [n for n in numbers if type(n) is int]
                        ),
                        FactoryReceipt.state == "succeeded",
                    )
                    .limit(10)
                ).all()
                for receipt in receipts:
                    if controls.is_advisory(controls.receipt_task_class(receipt)):
                        continue
                    merged = db.exec(
                        select(FactoryAudit)
                        .where(
                            FactoryAudit.task_id == receipt.task_id,
                            FactoryAudit.action == "owned_pr_merged",
                        )
                        .order_by(FactoryAudit.id.desc())
                        .limit(1)
                    ).first()
                    if merged is None:
                        continue
                    evidence = json.loads(merged.detail_json)
                    number = evidence.get("pr_number")
                    if type(number) is int and number > 0 and evidence.get("head_sha"):
                        item.update(
                            outcome="useful_completion",
                            merged_pr=number,
                            acceptance_evidence=f"succeeded receipt {receipt.id}; merged audit {merged.id}",
                        )
                        break
        result.append(item)
    return result


def _ready_work(inventory, policy):
    """Conservative fresh screen using intake labels and ordering.

    The earlier sweep supplies dependency/delivery exclusions. A newly ready
    row must not be hidden by that older sweep, even if a later full intake
    evaluation would exclude it. False positives only defer discovery.
    """
    from factory.orchestration.factory_intake_loop import (
        RANK_LABELS,
        _created_rank,
        _label_names,
    )

    entry = controls.repo_entry(policy, REPO)
    include = {label.lower() for label in entry["labels"]}
    exclude = {label.lower() for label in entry["exclude_labels"]} | {"human"}
    candidates = []
    for row in inventory:
        labels = _label_names(row)
        if (
            row.get("kind") != "issue"
            or row.get("assignees")
            or labels & exclude
            or not labels & include
        ):
            continue
        rank = next(
            (i for i, label in enumerate(RANK_LABELS) if label in labels),
            len(RANK_LABELS),
        )
        candidates.append((rank, _created_rank(row), row["number"]))
    return [number for _, _, number in sorted(candidates)[:20]]


def _snapshot(reviewed, current, history):
    return {
        "repo": REPO,
        "captured_at": controls._now().isoformat(),
        "intake_complete": current["intake_complete"],
        "ready_issue_numbers": _ready_work(current["inventory"], current["policy"]),
        "dedup_complete": True,
        "rules_reviewed": True,
        "history_complete": True,
        "active_goal_ids": [g["id"] for g in current["goals"]],
        "quota": current["quota"],
        "subscription_caps": reviewed.get("subscription_caps"),
        "history": history,
        "inventory": [],
        "findings": reviewed.get("findings", []),
    }


def _duplicates(draft, inventory):
    marker = f"<!-- factory-problem:{draft['fingerprint']} -->"
    title = draft["title"].strip().casefold()
    return any(
        marker in (row.get("body") or "")
        or title == (row.get("title") or "").strip().casefold()
        for row in inventory
    )


def _verify_scout_review(db, receipt_id, reviewed):
    from factory.orchestration.models import SwarmNodeRun

    receipt = _scout_receipt(db, receipt_id)
    if not receipt.task_id:
        raise ValueError("scout_task_missing")
    audit = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action == "scout_review",
            FactoryAudit.actor == "factory:improvement-scout",
            FactoryAudit.task_id == receipt.task_id,
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    detail = json.loads(audit.detail_json) if audit else {}
    run = (
        db.get(SwarmNodeRun, detail.get("run_id"))
        if type(detail.get("run_id")) is int
        else None
    )
    collection = db.exec(
        select(SwarmNodeRun).where(
            SwarmNodeRun.task_id == receipt.task_id,
            SwarmNodeRun.node_key == "scout_collect_1",
            SwarmNodeRun.status == "succeeded",
        )
    ).first()
    if (
        detail.get("receipt_id") != receipt_id
        or detail.get("approved") is not True
        or detail.get("review_digest") != reviewed.get("review_digest")
        or detail.get("findings_digest") != digest(reviewed.get("findings"))
        or run is None
        or run.task_id != receipt.task_id
        or run.node_key != "scout_review_1"
        or run.status != "succeeded"
        or run.session_id is None
        or collection is None
        or collection.session_id is None
        or run.session_id == collection.session_id
    ):
        raise ValueError("scout_review_not_verified")


def submit(reviewed: dict, *, actor: str, scout_receipt_id=None) -> dict:
    """Persist at most one candidate; the existing tick writes the issue later.

    Exact reviewed context prevents stale policy/owner/goal approval. History is
    loaded under the factory lock, so concurrent submissions cannot evade caps.
    """
    if not isinstance(reviewed, dict) or len(json.dumps(reviewed)) > 100_000:
        raise ValueError("invalid_improvement_batch")
    if (
        reviewed.get("rules_reviewed") is not True
        or reviewed.get("dedup_reviewed") is not True
    ):
        raise ValueError("rules_and_semantic_dedup_review_required")
    current = context(scout_receipt_id=scout_receipt_id)
    from factory.orchestration.factory_problem_issues import _observe_policy

    # Establish the source watermark before adding a new source event.
    _observe_policy(controls.problem_issues_policy(current["policy"]))
    with controls._locked_session() as (db, control):
        live = _database_context(db, control, scout_receipt_id=scout_receipt_id)
        block = controls.problem_issues_policy(live["policy"])
        scout_task_id = None
        if scout_receipt_id is not None:
            _verify_scout_review(db, scout_receipt_id, reviewed)
            scout_task_id = _scout_receipt(db, scout_receipt_id).task_id
            prior = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.action == ATTEMPT,
                    FactoryAudit.task_id == scout_task_id,
                )
                .order_by(FactoryAudit.id.desc())
                .limit(1)
            ).first()
            if prior:
                return {
                    "reason": json.loads(prior.detail_json)["reason"],
                    "drafts": [],
                    "replayed": True,
                }
        if (
            control.state != "enabled"
            or not live["repo_enabled"]
            or not block["enabled"]
            or not block["sources"][SOURCE]
        ):
            raise ValueError("improvement_source_disabled")
        if reviewed.get("review_digest") != _review_digest(live, current["inventory"]):
            raise ValueError("improvement_context_changed")
        current.update(live)
        result = preview(
            _snapshot(reviewed, current, _history(db, live["policy"])),
            now=controls._now(),
            policy=generation_policy(live["policy"]),
        )
        # Throttle denied retries without growing the ledger on each request.
        if result["reason"] not in ("candidates", "no_actionable_findings"):
            return result
        drafts = result["drafts"]
        # Trial/cooldown evaluation needs only recent attempts. Deduplication
        # still searches the entire durable ledger without loading its history.
        seen = None
        if drafts:
            fingerprint = drafts[0]["fingerprint"]
            seen = db.exec(
                select(FactoryAudit.id)
                .where(
                    FactoryAudit.action == ATTEMPT,
                    FactoryAudit.detail_json.contains(f'"fingerprint":"{fingerprint}"'),
                )
                .limit(1)
            ).first()
        if drafts and (
            seen is not None or _duplicates(drafts[0], current["inventory"])
        ):
            drafts.clear()
            result["reason"] = "duplicate_or_owned"
        draft = drafts[0] if drafts else None
        controls._audit(
            db,
            actor,
            ATTEMPT,
            task_id=scout_task_id,
            reason=result["reason"],
            fingerprint=draft["fingerprint"] if draft else None,
            review_digest=reviewed["review_digest"],
            quota_decisions=result.get("quota_decisions", []),
        )
        if draft:
            controls._audit(
                db,
                actor,
                CANDIDATE,
                repo=REPO,
                scout_receipt_id=scout_receipt_id,
                draft=draft,
                control_version=control.version,
                database_digest=digest(live),
                fingerprint=draft["fingerprint"],
                review_digest=reviewed["review_digest"],
                expires_at=(
                    controls._now() + timedelta(seconds=POLICY.max_age_seconds)
                ).isoformat(),
                reviewed={
                    key: reviewed[key] for key in ("subscription_caps", "findings")
                },
            )
        return {**result, "mode": "audit_candidate"}


def candidate_matches(detail: dict, current: dict) -> bool:
    """Pure deadline/quota check, repeatable under the writer's claim lock."""
    from factory.orchestration.factory_improvement_preview import _stamp

    expires = _stamp(detail.get("expires_at"))
    if expires is None or expires <= controls._now():
        return False
    result = preview(
        _snapshot(detail["reviewed"], current, []),
        now=controls._now(),
        policy=generation_policy(current["policy"]),
    )
    return bool(
        result["drafts"]
        and result["drafts"][0] == detail.get("draft")
        and not _duplicates(result["drafts"][0], current["inventory"])
    )


def validate_candidate(detail: dict) -> dict | None:
    """Recheck current conditions immediately before the shared writer claims."""
    current = context(scout_receipt_id=detail.get("scout_receipt_id"))
    block = controls.problem_issues_policy(current["policy"])
    if (
        current["control_state"] != "enabled"
        or not current["repo_enabled"]
        or not block["enabled"]
        or not block["sources"][SOURCE]
        or current["review_digest"] != detail.get("review_digest")
    ):
        return None
    return current if candidate_matches(detail, current) else None
