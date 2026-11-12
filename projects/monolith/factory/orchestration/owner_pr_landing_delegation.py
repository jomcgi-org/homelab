"""Standing, bounded landing authority for genuinely adopted owner PRs.

The existing landing owner derives exact-head audits from a separate standing
operator decision. This module starts no work, merges nothing, clears no hold,
and never manufactures a human per-head grant. Repair authority is independent.
"""

import json
import re
from dataclasses import asdict
from datetime import timedelta

import httpx
import jsonschema
from sqlmodel import select

from factory.access import is_operator
from factory.orchestration import factory_controls as controls
from factory.orchestration import graph
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

CONTROL = "owner_pr_landing_delegation_configured"
GRANT = "owner_pr_landing_authorized"
ACTOR = "factory:owner-pr-landing"
REPO = "jomcgi-org/homelab"
MAX_DERIVATIONS = 3
MAX_FILES = 8
RATE_WINDOW = timedelta(hours=24)
# Independently frozen. Changes to repair profiles cannot expand owner authority.
PATH_PROFILE = "private_diagnostics_v1"
ALLOWED_PATHS = frozenset(
    prefix + suffix
    for prefix in (
        "projects/monolith/factory/orchestration/factory_problem_issues",
        "projects/monolith/factory/orchestration/factory_intake_loop",
        "projects/monolith/factory/orchestration/factory_progress_watchdog",
        "projects/monolith/factory/orchestration/factory_feedback",
        "projects/monolith/factory/execution/rationale",
        "projects/monolith/factory/orchestration/unified_diff",
    )
    for suffix in (".py", "_test.py")
)


def _require(condition, reason):
    if not condition:
        raise ValueError("owner_pr_landing_" + reason)


def _binding(db, control, *, enabled):
    from factory.orchestration.factory_landing_release import latest_policy_transition

    policy = json.loads(control.policy_json or "{}")
    intake = policy.get("intake") or {}
    owner = intake.get("owner_pr_author")
    if enabled:
        _require(
            isinstance(owner, dict)
            and set(owner) == {"login", "id"}
            and isinstance(owner["login"], str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", owner["login"])
            and type(owner["id"]) is int
            and owner["id"] > 0,
            "owner_identity_missing",
        )
        _require(
            control.state == "enabled"
            and policy.get("repo") == REPO
            and isinstance(policy.get("base_branch"), str)
            and intake.get("enabled") is True
            and intake.get("refine_enabled") is True
            and not intake.get("refinement_batch")
            and controls.repo_auto_merge_enabled(policy, REPO),
            "policy_not_enabled",
        )
    # A successful control transition remains visible even if a later command
    # restores identical policy bytes. Failed commands do not renew authority.
    config = latest_policy_transition(db)
    return {
        "owner": owner,
        "repo": policy.get("repo"),
        "base_branch": policy.get("base_branch"),
        "policy_sha256": controls.digest(policy),
        "intake_policy_sha256": controls.digest(intake),
        "configuration_audit_id": config.id if config else None,
    }


def configure(
    principal,
    operation,
    enabled,
    expected_version,
    reason,
    *,
    request_key=None,
    expected_preview_sha256=None,
):
    """Preview/apply an ongoing three-per-day, three-per-PR standing decision."""
    _require(is_operator(principal), "standing_human_required")
    _require(operation in {"preview", "apply"}, "operation_invalid")
    _require(type(enabled) is bool, "enabled_invalid")
    _require(type(expected_version) is int and expected_version >= 0, "version_invalid")
    request = {
        "enabled": enabled,
        "expected_version": expected_version,
        "reason": controls._text(reason, "reason", 4000),
        "request_key": request_key,
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._locked_session() as (db, control):
        if operation == "apply":
            controls._text(request_key, "request_key", 200)
            events = db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.action == CONTROL,
                    FactoryAudit.actor == principal.subject,
                    FactoryAudit.detail_json.contains(
                        '"request_key":' + json.dumps(request_key)
                    ),
                )
            ).all()
            for event in events:
                saved = json.loads(event.detail_json)
                if saved["request"]["request_key"] == request_key:
                    _require(saved["request"] == request, "request_conflict")
                    return saved["result"]
        _require(control.version == expected_version, "version_changed")
        prior = controls.latest_audit(db, CONTROL)
        proposal = {
            "enabled": enabled,
            "version": control.version,
            "binding": _binding(db, control, enabled=enabled),
            "previous_authorization_id": prior.id if prior else None,
            "path_profile": PATH_PROFILE,
            "allowed_paths": sorted(ALLOWED_PATHS),
            "rolling_window_hours": 24,
            "max_derivations": MAX_DERIVATIONS,
            "max_pr_derivations": MAX_DERIVATIONS,
            "allow_preexisting_owner_prs": True,
            "reason": request["reason"],
        }
        proposal["preview_sha256"] = controls.digest(proposal)
        if operation == "preview":
            return {"ok": True, **proposal}
        _require(
            expected_preview_sha256 == proposal["preview_sha256"], "preview_changed"
        )
        control.version += 1
        control.actor, control.updated_at = principal.subject, controls._now()
        result = {
            "ok": True,
            "enabled": enabled,
            "version": control.version,
            "dispatched": False,
        }
        controls._audit(
            db,
            principal.subject,
            CONTROL,
            request=request,
            proposal=proposal,
            result=result,
        )
        return result


def _authority(db):
    event = controls.latest_audit(db, CONTROL)
    _require(event is not None, "disabled")
    saved = json.loads(event.detail_json)
    proposal, result = saved["proposal"], saved["result"]
    _require(result.get("ok") is True and result.get("enabled") is True, "disabled")
    _require(
        proposal.get("enabled") is True
        and proposal.get("path_profile") == PATH_PROFILE
        and proposal.get("allowed_paths") == sorted(ALLOWED_PATHS)
        and proposal.get("rolling_window_hours") == 24
        and proposal.get("max_derivations") == MAX_DERIVATIONS
        and proposal.get("max_pr_derivations") == MAX_DERIVATIONS
        and proposal.get("allow_preexisting_owner_prs") is True,
        "bounds_changed",
    )
    control = db.get(FactoryControl, "factory")
    _require(
        proposal["binding"] == _binding(db, control, enabled=True), "policy_changed"
    )
    return event, proposal, result


def _canonical(repo, number):
    return controls.digest({"repo": repo, "pr_number": number})


def _grants(db, *, canonical=None, since=None):
    query = select(FactoryAudit).where(
        FactoryAudit.action == GRANT, FactoryAudit.actor == ACTOR
    )
    if canonical is not None:
        query = query.where(
            FactoryAudit.detail_json.contains(
                '"canonical_key":' + json.dumps(canonical)
            )
        )
    if since is not None:
        query = query.where(FactoryAudit.created_at > since)
    # Four rows prove either three-cap exhaustion. Scope by immutable canonical
    # identity BEFORE bounding, so unrelated history cannot hide a grant.
    rows = db.exec(
        query.order_by(FactoryAudit.id.desc()).limit(MAX_DERIVATIONS + 1)
    ).all()
    return [{**json.loads(row.detail_json), "task_id": row.task_id} for row in rows]


def status(db):
    event = controls.latest_audit(db, CONTROL)
    if event is None:
        return {"enabled": False, "reason": "owner_pr_landing_disabled"}
    try:
        _, proposal, _ = _authority(db)
        used = len(_grants(db, since=controls._now() - RATE_WINDOW))
        return {
            "enabled": True,
            "authorization_id": event.id,
            "owner": proposal["binding"]["owner"],
            "path_profile": PATH_PROFILE,
            "rolling_window_hours": 24,
            "max_derivations": MAX_DERIVATIONS,
            "max_pr_derivations": MAX_DERIVATIONS,
            "derivations_used": used,
            "derivations_remaining": max(0, MAX_DERIVATIONS - used),
        }
    except (ValueError, KeyError, TypeError):
        return {
            "enabled": False,
            "authorization_id": event.id,
            "reason": "owner_pr_landing_authority_unavailable",
        }


def _history(db, row, number, branch):
    from factory.orchestration.factory_intake import protected_work_history
    from factory.orchestration.factory_landing_release import execution_refusal

    _require(
        controls.delivery_branch_owner(db, row.repo, branch, exclude_receipt_id=row.id)
        is None,
        "competing_branch_owner",
    )
    history = protected_work_history(db, row.repo, row.issue_number, row.work_item_id)
    # A queued receipt also owns its granted target, even before it has a task.
    for other in db.exec(
        select(FactoryReceipt).where(FactoryReceipt.repo == row.repo)
    ).all():
        if other.id == row.id:
            continue
        other_branch, other_number = controls.granted_delivery_surface(
            json.loads(other.direction_json or "{}")
        )
        if other_branch == branch or other_number == number:
            _require(
                other.state not in {"queued", "admitted", "uncertain", "landing"},
                "competing_delivery_owner",
            )
            if all(existing.id != other.id for existing in history):
                history.append(other)
    others = [other for other in history if other.id != row.id]
    _require(
        controls.natural_history_exclusion(others) is None, "canonical_history_held"
    )
    for other in others:
        _require(
            other.state
            not in {"queued", "admitted", "uncertain", "landing", "escalated"}
            and not other.task_paused
            and not other.cancellation_requested,
            "competing_canonical_owner",
        )
        escalation = json.loads(other.escalation_json or "{}")
        _require(
            not escalation or escalation.get("resolved") is not None,
            "canonical_history_escalated",
        )
        if other.task_id:
            _require(
                execution_refusal(db, other.task_id) is None,
                "canonical_execution_unsettled",
            )
        _require(
            other.state != "succeeded" or controls.is_advisory(other.task_class),
            "canonical_delivery_already_succeeded",
        )
    return [
        {"id": r.id, "task_id": r.task_id, "state": r.state, "generation": r.generation}
        for r in others
    ]


def _intake_event(db, row, number):
    events = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.actor == controls.INTAKE_ACTOR,
            FactoryAudit.action == "receive_owner_pr",
            FactoryAudit.detail_json.contains('"receipt_id":' + str(row.id) + ","),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(2)
    ).all()
    # Receipt ids are globally unique; two real source events are ambiguous.
    _require(len(events) == 1, "intake_provenance_missing_or_ambiguous")
    event = events[0]
    value = json.loads(event.detail_json)
    _require(
        all(
            value.get(k) == v
            for k, v in {
                "receipt_id": row.id,
                "repo": row.repo,
                "pr_number": number,
                "issue_number": row.issue_number,
            }.items()
        ),
        "intake_identity_changed",
    )
    return event, value


def _direct_refinement(db, row, intake_event, direction):
    from factory.orchestration import factory_refine

    rows = db.exec(
        select(FactoryReceipt).where(
            FactoryReceipt.repo == row.repo,
            FactoryReceipt.issue_number == row.issue_number,
            FactoryReceipt.work_item_id == row.work_item_id,
            FactoryReceipt.task_class == "refine",
            FactoryReceipt.state == "succeeded",
        )
    ).all()
    eligible = {}
    for refined in rows:
        prior = json.loads(refined.direction_json or "{}")
        if (
            prior.get("owner_pr", {}).get("direct") is True
            and prior.get("owner_pr", {}).get("author_id")
            == direction["owner_pr"]["author_id"]
            and controls.granted_delivery_surface(prior)
            == controls.granted_delivery_surface(direction)
        ):
            _intake_event(db, refined, row.issue_number)
            eligible[refined.task_id] = refined
    _require(bool(eligible), "direct_refinement_missing")
    settled = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.actor == factory_refine.ACTOR,
            FactoryAudit.action == "refine_settled",
            FactoryAudit.task_id.in_(eligible),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    _require(
        settled is not None and settled.id < intake_event.id,
        "direct_refinement_missing",
    )
    value = json.loads(settled.detail_json)
    _require(value.get("outcome") == "agent-ready", "direct_refinement_not_ready")
    return {
        "receipt_id": eligible[settled.task_id].id,
        "task_id": settled.task_id,
        "settlement_audit_id": settled.id,
        "comment_url": value.get("comment_url"),
    }


def _provenance(db, row, number, proposal):
    from factory.orchestration import owner_prs, work_items

    direction = json.loads(row.direction_json or "{}")
    marker = direction.get("owner_pr")
    _require(
        isinstance(marker, dict) and type(marker.get("direct")) is bool,
        "owner_provenance_missing",
    )
    _require(
        direction.get("delivery_adoption") is True
        and direction.get("delivery_target_checked") is True,
        "adoption_missing",
    )
    branch, granted_number = controls.granted_delivery_surface(direction)
    _require(
        granted_number == number
        and isinstance(branch, str)
        and not branch.startswith("factory/"),
        "adopted_identity_changed",
    )
    _require(
        marker.get("author_id") == proposal["binding"]["owner"]["id"],
        "owner_identity_changed",
    )
    event, received = _intake_event(db, row, number)
    _require(
        marker.get("observed_head_sha") == received.get("head_sha")
        and isinstance(received.get("head_sha"), str)
        and re.fullmatch(r"[0-9a-f]{40}", received["head_sha"]),
        "intake_identity_changed",
    )
    item = db.get(WorkItem, row.work_item_id) if row.work_item_id else None
    _require(
        item is not None
        and (item.github_repo, item.github_issue_number) == (row.repo, row.issue_number)
        and item.authority == "github"
        and work_items.is_admissible(db, item.id)
        and not owner_prs._hold({"labels": item.labels, "body": item.body}, set()),
        "canonical_work_item_held",
    )
    history = _history(db, row, number, branch)
    _require(marker["direct"] == (row.issue_number == number), "scope_identity_changed")
    refined = (
        _direct_refinement(db, row, event, direction) if marker["direct"] else None
    )
    return {
        "intake_audit_id": event.id,
        "work_item_id": item.id,
        "issue_number": row.issue_number,
        "branch": branch,
        "direct_refinement": refined,
        "history": history,
    }


def _fresh_context(db, row, number, pull, provenance):
    from factory.orchestration import factory_landing as landing
    from factory.orchestration import owner_prs
    from factory.orchestration.dependency_prs import _pages
    from factory.orchestration.factory_intake import protected_work_history
    from factory.orchestration.factory_intake_loop import _label_names

    policy = json.loads(db.get(FactoryControl, "factory").policy_json)
    issue = landing.github_get(row.repo, f"issues/{row.issue_number}")
    _require(
        issue.get("number") == row.issue_number and issue.get("state") == "open",
        "objective_not_open",
    )
    issues = owner_prs._resolve_references(pull, [issue], row.repo, landing.github_get)
    scoped, reason = owner_prs._scope(pull, issues, row.repo, policy)
    direct = provenance["direct_refinement"] is not None
    _require(
        (direct and reason == "scope_authority_missing")
        or (
            not direct
            and reason is None
            and scoped is not None
            and scoped["number"] == row.issue_number
        ),
        "objective_scope_changed",
    )
    excluded = {
        s.lower() for s in controls.repos_map(policy)[row.repo]["exclude_labels"]
    }
    _require(not owner_prs._hold(pull, excluded), "pull_held")
    references = {i["number"] for i in issues}
    _require(
        owner_prs._referenced_history(db, row.repo, references - {row.issue_number})
        is None,
        "referenced_objective_held",
    )
    for related in issues:
        _require(not owner_prs._hold(related, excluded), "objective_held")
        if related["number"] != row.issue_number:
            from factory.orchestration.factory_landing_release import execution_refusal

            history = protected_work_history(db, row.repo, related["number"])
            _require(
                controls.natural_history_exclusion(history) is None,
                "referenced_history_held",
            )
            for other in history:
                _require(
                    other.state
                    not in {"queued", "admitted", "uncertain", "landing", "escalated"}
                    and not other.task_paused
                    and not other.cancellation_requested,
                    "referenced_history_held",
                )
                if other.task_id:
                    _require(
                        execution_refusal(db, other.task_id) is None,
                        "referenced_execution_unsettled",
                    )
    comments = {}
    for related_number in sorted(references | {number}):
        observed = _pages(
            row.repo, f"issues/{related_number}/comments", landing.github_list
        )
        _require(
            not any(owner_prs.HOLD.search(c.get("body") or "") for c in observed),
            "explicit_discussion_hold",
        )
        comments[related_number] = observed
    if direct:
        url = provenance["direct_refinement"]["comment_url"]
        _require(
            isinstance(url, str)
            and any(
                c.get("html_url") == url and "## Agent brief" in (c.get("body") or "")
                for c in comments[row.issue_number]
            ),
            "direct_brief_unavailable",
        )
    # Non-authoritative comments do not spend a new derivation. Scope-bearing
    # objective bodies/labels do, and every discussion is freshly checked.
    return [
        {
            "number": i["number"],
            "body": i.get("body") or "",
            "state": i.get("state"),
            "labels": sorted(_label_names(i)),
        }
        for i in sorted(issues, key=lambda i: i["number"])
    ]


def _candidate(db, row, number, head):
    from factory import review_publisher
    from factory.orchestration import (
        factory_funding as funding,
    )
    from factory.orchestration import (
        factory_landing as landing,
    )
    from factory.orchestration import (
        factory_landing_release as releases,
    )
    from factory.orchestration import (
        owner_prs,
    )
    from factory.orchestration.repair_landing_delegation import SCOPE_SCHEMA

    _require(
        type(number) is int
        and number > 0
        and isinstance(head, str)
        and re.fullmatch(r"[0-9a-f]{40}", head),
        "delivery_identity_invalid",
    )
    authority, proposal, result = _authority(db)
    _require(
        row is not None
        and row.repo == REPO
        and row.state == "landing"
        and row.task_class in {"bug-fix", "mechanical-refactor", "docs"},
        "receipt_ineligible",
    )
    graph._lock_task(db, row.task_id)
    direction = json.loads(row.direction_json or "{}")
    _require("landing_authorization" not in direction, "operator_grant_owned")
    _require(
        not row.task_paused
        and not row.cancellation_requested
        and not controls.landing_held(row)
        and not funding.routine_hold(db, row, row.task_id)
        and not funding._recovery_held(db, row.task_id)
        and not controls.decision_in_flight(db, row.id),
        "receipt_held",
    )
    _require(releases.execution_refusal(db, row.task_id) is None, "execution_unsettled")
    state = landing._landing_state(db, [row.task_id])[row.task_id]
    _require(
        not state["merge_arm_refused"]
        and len(state["merge_ejected"]) < landing.MAX_EJECTIONS,
        "recovery_limit",
    )
    provenance = _provenance(db, row, number, proposal)
    task = db.get(SwarmTask, row.task_id)
    pull = landing.github_get(row.repo, f"pulls/{number}")
    owner = proposal["binding"]["owner"]
    user = pull.get("user") or {}
    _require(
        pull.get("number") == number
        and pull.get("state") == "open"
        and not pull.get("merged")
        and not pull.get("draft")
        and type(user.get("id")) is int
        and user.get("id") == owner["id"]
        and user.get("type") == "User"
        and user.get("login", "").lower() == owner["login"]
        and pull.get("head", {}).get("sha") == head
        and pull.get("head", {}).get("ref") == provenance["branch"]
        and pull.get("head", {}).get("repo", {}).get("full_name") == row.repo
        and pull.get("base", {}).get("repo", {}).get("full_name") == row.repo
        and task is not None
        and task.base_branch == proposal["binding"]["base_branch"]
        and pull.get("base", {}).get("ref") == task.base_branch,
        "pull_identity_changed",
    )
    owner_prs.assert_unheld(
        {
            "owner_pr": direction["owner_pr"],
            "repo": row.repo,
            "delivery_pr_number": number,
        },
        pull,
        read_get=landing.github_get,
        read_list=landing.github_list,
    )
    objectives = _fresh_context(db, row, number, pull, provenance)
    review = review_publisher.collect(row.task_id, session=db)
    _require(
        isinstance(review, review_publisher.ReviewEvidence)
        and (review.repo, review.pr_number, review.head_sha, review.branch)
        == (row.repo, number, head, provenance["branch"]),
        "independent_review_missing",
    )
    run = db.get(SwarmNodeRun, review.review_run_id)
    valid, artifact = review_publisher._artifact(run)
    scope = artifact.get("landing_scope")
    _require(valid == "ok", "review_invalid")
    try:
        jsonschema.validate(scope, SCOPE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValueError("owner_pr_landing_scope_missing_or_invalid") from exc
    _require(
        scope["classification"] == "private_internal_reversible"
        and not any(
            scope[k]
            for k in (
                "public_facing",
                "security_sensitive",
                "authentication_or_credentials",
                "destructive",
            )
        ),
        "scope_requires_operator",
    )
    _require(len(scope["paths"]) <= MAX_FILES, "file_limit")
    comparison = landing.github_get(row.repo, f"compare/{pull['base']['sha']}...{head}")
    _require(
        scope["base_sha"] == comparison["merge_base_commit"]["sha"],
        "review_scope_changed",
    )
    files = releases.regular_file_evidence(
        {**pull, "base": {**pull["base"], "sha": scope["base_sha"]}},
        ALLOWED_PATHS,
        "owner_pr_landing_paths_require_operator",
    )
    _require(
        sorted(scope["paths"]) == sorted(f["path"] for f in files["files"]),
        "review_scope_changed",
    )
    releases._handoff_ci(row.repo, head)
    return {
        "authorization_id": authority.id,
        "standing_actor": authority.actor,
        "delegation_version": result["version"],
        "binding": proposal["binding"],
        "receipt_id": row.id,
        "task_id": row.task_id,
        "repo": row.repo,
        "pr_number": number,
        "canonical_key": _canonical(row.repo, number),
        "head_sha": head,
        "direction_sha256": controls.digest(row.direction_json),
        "provenance": provenance,
        "objectives": objectives,
        "review": {
            k: v for k, v in asdict(review).items() if k != "published_check_id"
        },
        "review_sha256": controls.digest(artifact),
        "scope": scope,
        "files": files,
    }


def _observed_refusal(exc):
    if isinstance(exc, httpx.HTTPError):
        return "owner_pr_landing_observation_unavailable"
    if isinstance(exc, ValueError):
        reason = str(exc)
        return (
            reason
            if reason.startswith("owner_pr_landing_")
            else "owner_pr_landing_" + reason
        )
    return "owner_pr_landing_evidence_invalid"


def prepare(task_id, number, head):
    """Derive under the existing locks. Replay is free, reauthorization is not."""
    try:
        with controls._locked_session() as (db, _):
            candidate = _candidate(db, controls._receipt(db, task_id), number, head)
            prior = _grants(db, canonical=candidate["canonical_key"])
            if candidate in prior:
                return None
            _require(len(prior) < MAX_DERIVATIONS, "pr_limit")
            _require(
                len(_grants(db, since=controls._now() - RATE_WINDOW)) < MAX_DERIVATIONS,
                "rolling_limit",
            )
            controls._audit(
                db,
                ACTOR,
                GRANT,
                task_id=task_id,
                **{k: v for k, v in candidate.items() if k != "task_id"},
            )
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _observed_refusal(exc)


def refusal(db, row, number, head):
    try:
        candidate = _candidate(db, row, number, head)
        _require(
            candidate in _grants(db, canonical=candidate["canonical_key"]),
            "authorization_missing_or_changed",
        )
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _observed_refusal(exc)
