"""Bounded standing authorization for landing's existing repair queue owner.

No worker receives operator identity. The existing audit journal holds both the
standing authorization and separate, exact-head machine derivations. Absence,
revocation, expiry or policy drift fails closed. This module never merges,
releases holds, resumes execution or changes admission policy.
"""

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta

import httpx
import jsonschema
from sqlmodel import select

from factory.access import is_operator
from factory.orchestration import (
    factory_controls as controls,
    graph,
    landing_authority_policy,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryControl
from factory.orchestration.models import SwarmNodeRun, SwarmTask

CONTROL = "repair_landing_delegation_configured"
GRANT = "repair_landing_authorized"
ACTOR = "factory:repair-landing"
REPO = "jomcgi-org/homelab"
# Initial, reviewable boundary: existing private observation/discovery code.
# Authority owners (funding, graph, controls, landing, MCP), public/docs, CI,
# migrations, deployment, authentication and credential paths cannot qualify.
ALLOWED_PATHS = frozenset(
    "projects/monolith/factory/orchestration/" + name + suffix
    for name in (
        "factory_problem_issues",
        "factory_intake_loop",
        "factory_progress_watchdog",
        "factory_feedback",
    )
    for suffix in (".py", "_test.py")
)
# An additional, explicitly selected boundary for routine output parsing. It
# does not include executor dispatch, build gates or control/permission owners.
PATH_PROFILES = {
    "observation": ALLOWED_PATHS,
    "diagnostics": ALLOWED_PATHS
    | {
        prefix + suffix
        for prefix in (
            "projects/monolith/factory/execution/rationale",
            "projects/monolith/factory/orchestration/unified_diff",
        )
        for suffix in (".py", "_test.py")
    },
}
MAX_FILES = 8
MAX_TASK_DERIVATIONS = 3
RATE_WINDOW = timedelta(hours=24)
SCOPE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "classification",
        "public_facing",
        "security_sensitive",
        "authentication_or_credentials",
        "destructive",
        "evidence",
        "base_sha",
        "paths",
    ],
    "properties": {
        "classification": {
            "enum": ["private_internal_reversible", "requires_operator"]
        },
        **{
            key: {"type": "boolean"}
            for key in (
                "public_facing",
                "security_sensitive",
                "authentication_or_credentials",
                "destructive",
            )
        },
        "evidence": {"type": "string", "minLength": 20, "maxLength": 4000},
        "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "paths": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
        },
    },
}


def _require(condition, reason):
    if not condition:
        raise ValueError("repair_landing_" + reason)


def _latest(db, action, task_id=None):
    query = select(FactoryAudit).where(FactoryAudit.action == action)
    if task_id is not None:
        query = query.where(FactoryAudit.task_id == task_id)
    return db.exec(query.order_by(FactoryAudit.id.desc()).limit(1)).first()


def configure(
    principal,
    operation,
    enabled,
    expected_version,
    reason,
    *,
    duration_minutes=60,
    max_repairs=3,
    mode="trial",
    path_profile="observation",
    request_key=None,
    expected_preview_sha256=None,
):
    _require(is_operator(principal), "standing_human_required")
    _require(operation in {"preview", "apply"}, "operation_invalid")
    _require(type(enabled) is bool, "enabled_invalid")
    _require(mode in {"trial", "ongoing"}, "mode_invalid")
    _require(path_profile in PATH_PROFILES, "path_profile_invalid")
    _require(
        (mode == "ongoing" and duration_minutes is None)
        or (
            mode == "trial"
            and type(duration_minutes) is int
            and 1 <= duration_minutes <= 1440
        ),
        "duration_invalid",
    )
    _require(type(max_repairs) is int and 1 <= max_repairs <= 10, "limit_invalid")
    request = dict(
        enabled=enabled,
        expected_version=expected_version,
        reason=controls._text(reason, "reason"),
        duration_minutes=duration_minutes,
        max_repairs=max_repairs,
        request_key=request_key,
        expected_preview_sha256=expected_preview_sha256,
    )
    # Preserve trial request identity for callers replaying pre-upgrade commands.
    if mode != "trial" or path_profile != "observation":
        request.update(mode=mode, path_profile=path_profile)
    with controls._locked_session() as (db, control):
        if operation == "apply":
            controls._text(request_key, "request_key")
            for raw in db.exec(
                select(FactoryAudit.detail_json).where(
                    FactoryAudit.actor == principal.subject,
                    FactoryAudit.action == CONTROL,
                    FactoryAudit.detail_json.contains(
                        '"request_key":' + json.dumps(request_key)
                    ),
                )
            ).all():
                saved = json.loads(raw)
                if saved["request"]["request_key"] == request_key:
                    _require(saved["request"] == request, "request_conflict")
                    return saved["result"]
        _require(control.version == expected_version, "version_changed")
        policy = json.loads(control.policy_json or "{}")
        _require(
            not enabled
            or (
                control.state == "enabled"
                and policy.get("repo") == REPO
                and controls.repo_auto_merge_enabled(policy, REPO)
            ),
            "policy_not_enabled",
        )
        prior = _latest(db, CONTROL)
        proposal = dict(
            enabled=enabled,
            version=control.version,
            policy_sha256=controls.digest(policy),
            landing_policy_evidence=landing_authority_policy.evidence(control),
            repo=REPO,
            previous_authorization_id=prior.id if prior else None,
            duration_minutes=duration_minutes,
            max_repairs=max_repairs,
            allowed_paths=sorted(PATH_PROFILES[path_profile]),
            reason=request["reason"],
        )
        if mode != "trial" or path_profile != "observation":
            proposal.update(mode=mode, path_profile=path_profile)
        if mode == "ongoing":
            proposal.update(
                rolling_window_hours=24,
                max_task_derivations=MAX_TASK_DERIVATIONS,
                max_source_derivations=MAX_TASK_DERIVATIONS,
            )
        proposal["preview_sha256"] = controls.digest(proposal)
        if operation == "preview":
            return {"ok": True, **proposal}
        _require(
            proposal["preview_sha256"] == expected_preview_sha256, "preview_changed"
        )
        control.version += 1
        control.actor, control.updated_at = principal.subject, controls._now()
        result = dict(
            ok=True,
            enabled=enabled,
            version=control.version,
            dispatched=False,
            expires_at=(
                controls._now() + timedelta(minutes=duration_minutes)
            ).isoformat()
            if mode == "trial"
            else None,
        )
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
    event = _latest(db, CONTROL)
    _require(event is not None, "disabled")
    saved = json.loads(event.detail_json)
    proposal, result = saved["proposal"], saved["result"]
    _require(result.get("ok") is True and result.get("enabled") is True, "disabled")
    mode = proposal.get("mode", "trial")
    _require(mode in {"trial", "ongoing"}, "mode_invalid")
    if mode == "trial":
        _require(
            controls._now() < datetime.fromisoformat(result["expires_at"]), "expired"
        )
    else:
        _require(
            result["expires_at"] is None
            and proposal.get("rolling_window_hours") == 24
            and proposal.get("max_task_derivations") == MAX_TASK_DERIVATIONS
            and proposal.get("max_source_derivations") == MAX_TASK_DERIVATIONS,
            "bounds_changed",
        )
    control = db.get(FactoryControl, "factory")
    policy = json.loads(control.policy_json or "{}")
    _require(
        control.state == "enabled" and controls.repo_auto_merge_enabled(policy, REPO),
        "policy_not_enabled",
    )
    _require(
        proposal["repo"] == REPO
        and proposal.get("path_profile", "observation") in PATH_PROFILES
        and proposal["allowed_paths"]
        == sorted(PATH_PROFILES[proposal.get("path_profile", "observation")]),
        "policy_changed",
    )
    _require(
        landing_authority_policy.compatible(db, event, proposal, "repair_landing"),
        "policy_replaced",
    )
    return event, proposal, result


def status(db):
    event = _latest(db, CONTROL)
    if event is None:
        return {"enabled": False, "reason": "repair_landing_disabled"}
    try:
        _, proposal, result = _authority(db)
        ongoing = proposal.get("mode") == "ongoing"
        used = len(_rolling_grants(db) if ongoing else _grants(db, event.id))
        return {
            "enabled": True,
            "authorization_id": event.id,
            "expires_at": result["expires_at"],
            "mode": proposal.get("mode", "trial"),
            "path_profile": proposal.get("path_profile", "observation"),
            "rolling_window_hours": 24 if ongoing else None,
            "max_task_derivations": MAX_TASK_DERIVATIONS if ongoing else None,
            "max_source_derivations": MAX_TASK_DERIVATIONS if ongoing else None,
            "max_repairs": proposal["max_repairs"],
            "derivations_used": used,
            "derivations_remaining": max(0, proposal["max_repairs"] - used),
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {
            "enabled": False,
            "authorization_id": event.id,
            "reason": str(exc)
            if isinstance(exc, ValueError)
            else "repair_landing_evidence_invalid",
            "needs_operator": landing_authority_policy.previously_enabled(db, CONTROL),
        }


def _provenance(db, row, authorization_id):
    from factory.orchestration import (
        factory_funding as funding,
        factory_problem_issues as producer,
    )

    # Lookup the exact producer marker before bounding results. An ongoing
    # authorization must not become unusable after 500 unrelated repairs.
    markers = re.findall(r"<!-- factory-problem:([0-9a-f]{64}) -->", row.body or "")
    _require(len(markers) == 1, "producer_provenance_missing_or_ambiguous")
    events = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.actor == producer.ACTOR,
            FactoryAudit.action.in_(
                ("problem_issue_created", "problem_issue_reconciled")
            ),
            FactoryAudit.id > authorization_id,
            producer._fingerprint_clause(markers[0]),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(3)
    ).all()
    _require(len(events) < 3, "producer_history_incomplete")
    matches = []
    for event in events:
        detail = json.loads(event.detail_json)
        numbers = detail.get("issue_numbers") or [detail.get("issue_number")]
        if (
            row.issue_number not in numbers
            or detail.get("source") != "recurring_blocker"
        ):
            continue
        source = controls._receipt(db, event.task_id)
        if source is None or source.repo != row.repo or source.task_id == row.task_id:
            continue
        graph._lock_task(db, source.task_id)
        wait = funding.repair_pause_evidence(db, source.task_id)
        if wait is None or wait["source_audit_id"] != detail.get("source_audit_id"):
            continue
        signal = db.get(FactoryAudit, wait["source_audit_id"])
        if signal is None or signal.id <= authorization_id:
            continue
        if f"{producer.MARKER_PREFIX}:" in source.body:
            continue
        watch = producer._last_matching(
            db, detail["fingerprint"], ("problem_issue_recovery_watch",), source.task_id
        )
        if watch is None or watch.actor != producer.ACTOR:
            continue
        watched = json.loads(watch.detail_json)
        if (
            watched.get("source_audit_id"),
            watched.get("node_key"),
            watched.get("source_run_id"),
        ) != (signal.id, wait["retry"]["node_key"], wait["retry"]["source_run_id"]):
            continue
        matches.append(
            dict(
                created_id=event.id,
                watch_id=watch.id,
                source_task_id=source.task_id,
                wait=wait,
                marker=producer._marker(detail["fingerprint"]),
            )
        )
    _require(len(matches) == 1, "producer_provenance_missing_or_ambiguous")
    return matches[0]


def _issue(row, *, source_session=None):
    from factory.orchestration import (
        factory_funding as funding,
        factory_landing as landing,
    )

    issue = landing.github_get(row.repo, f"issues/{row.issue_number}")
    from factory.orchestration.backlog_refill_repair import objective_open

    objective = issue.get("state") == "open" or (
        source_session is not None
        and objective_open(
            {"id": row.task_id, "issue_number": row.issue_number},
            issue,
            session=source_session,
        )
    )
    labels = {
        (x.get("name", "") if isinstance(x, dict) else str(x)).lower()
        for x in issue.get("labels") or []
    }
    _require(
        issue.get("number") == row.issue_number
        and objective
        and not issue.get("assignees")
        and not labels & funding.ROUTINE_HELD_LABELS,
        "issue_held",
    )
    return {
        "number": row.issue_number,
        "body": issue.get("body") or "",
        "labels": sorted(labels),
    }


def _candidate(db, row, number, head):
    from factory import review_publisher
    from factory.orchestration import (
        factory_funding as funding,
        factory_landing as landing,
    )
    from factory.orchestration import factory_landing_release as releases

    authority, proposal, result = _authority(db)
    _require(
        row is not None
        and row.repo == REPO
        and row.state == "landing"
        and row.task_class == "bug-fix",
        "receipt_ineligible",
    )
    graph._lock_task(db, row.task_id)
    _require(
        not funding.routine_hold(db, row, row.task_id)
        and not funding._recovery_held(db, row.task_id)
        and not controls.decision_in_flight(db, row.id),
        "receipt_held",
    )
    _require(
        "landing_authorization" not in json.loads(row.direction_json or "{}"),
        "operator_grant_owned",
    )
    refusal = releases.execution_refusal(db, row.task_id)
    if refusal:
        raise ValueError("repair_landing_" + refusal)
    state = landing._landing_state(db, [row.task_id])[row.task_id]
    _require(
        not state["merge_arm_refused"]
        and len(state["merge_ejected"]) < landing.MAX_EJECTIONS,
        "recovery_limit",
    )
    provenance = _provenance(db, row, authority.id)
    source = controls._receipt(db, provenance["source_task_id"])
    refusal = releases.execution_refusal(db, source.task_id)
    if refusal:
        raise ValueError("repair_landing_source_" + refusal)
    issue, source_issue = _issue(row), _issue(source, source_session=db)
    _require(
        provenance["marker"] in issue["body"] and provenance["marker"] in row.body,
        "producer_identity_changed",
    )
    review = review_publisher.collect(row.task_id, session=db)
    _require(
        isinstance(review, review_publisher.ReviewEvidence)
        and (review.repo, review.pr_number, review.head_sha)
        == (row.repo, number, head),
        "independent_review_missing",
    )
    run = db.get(SwarmNodeRun, review.review_run_id)
    valid, artifact = review_publisher._artifact(run)
    scope = artifact.get("landing_scope")
    _require(valid == "ok", "review_invalid")
    try:
        jsonschema.validate(scope, SCOPE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValueError("repair_landing_scope_missing_or_invalid") from exc
    _require(
        scope["classification"] == "private_internal_reversible"
        and not any(
            scope[key]
            for key in (
                "public_facing",
                "security_sensitive",
                "authentication_or_credentials",
                "destructive",
            )
        ),
        "scope_requires_operator",
    )
    # This is delegation eligibility, never the schema for ordinary reviews.
    _require(len(scope["paths"]) <= MAX_FILES, "file_limit")
    pull = landing.github_get(row.repo, f"pulls/{number}")
    task = db.get(SwarmTask, row.task_id)
    _require(
        pull.get("number") == number
        and pull.get("head", {}).get("sha") == head
        and pull.get("state") == "open"
        and not pull.get("merged")
        and not pull.get("draft")
        and pull.get("head", {}).get("ref") == f"factory/{row.task_id}"
        and pull.get("head", {}).get("repo", {}).get("full_name") == row.repo
        and pull.get("base", {}).get("repo", {}).get("full_name") == row.repo
        and pull.get("base", {}).get("ref") == task.base_branch,
        "pull_identity_changed",
    )
    # GitHub's base branch tip advances independently of this PR. Bind the
    # reviewed diff to its immutable merge base so unrelated main commits do
    # not strand an approved head. A changed merge base still needs new review.
    comparison = landing.github_get(row.repo, f"compare/{pull['base']['sha']}...{head}")
    _require(
        scope["base_sha"] == comparison["merge_base_commit"]["sha"],
        "review_scope_changed",
    )
    files = releases.regular_file_evidence(
        {**pull, "base": {**pull["base"], "sha": scope["base_sha"]}},
        frozenset(proposal["allowed_paths"]),
        "repair_landing_paths_require_operator",
    )
    _require(
        scope["base_sha"] == files["base_sha"]
        and sorted(scope["paths"]) == sorted(f["path"] for f in files["files"]),
        "review_scope_changed",
    )
    # This existing check requires a successful pr-checks status and every
    # non-advisory context. The protected queue remains the final CI owner.
    releases._handoff_ci(row.repo, head)
    return dict(
        authorization_id=authority.id,
        standing_actor=authority.actor,
        delegation_version=result["version"],
        policy_sha256=proposal["policy_sha256"],
        receipt_id=row.id,
        task_id=row.task_id,
        repo=row.repo,
        pr_number=number,
        head_sha=head,
        direction_sha256=controls.digest(row.direction_json),
        provenance=provenance,
        issue=issue,
        source_issue=source_issue,
        review={k: v for k, v in asdict(review).items() if k != "published_check_id"},
        review_sha256=controls.digest(artifact),
        scope=scope,
        files=files,
    )


def _grants(
    db, authorization_id=None, *, task_id=None, source_task_id=None, since=None
):
    query = select(FactoryAudit).where(
        FactoryAudit.actor == ACTOR,
        FactoryAudit.action == GRANT,
    )
    if authorization_id is not None:
        query = query.where(FactoryAudit.id > authorization_id)
    if task_id is not None:
        query = query.where(FactoryAudit.task_id == task_id)
    if source_task_id is not None:
        query = query.where(
            FactoryAudit.detail_json.contains(
                '"source_task_id":' + json.dumps(source_task_id)
            )
        )
    if since is not None:
        query = query.where(FactoryAudit.created_at > since)
    return [
        {**json.loads(row.detail_json), "task_id": row.task_id}
        for row in db.exec(query.order_by(FactoryAudit.id.desc()).limit(11)).all()
        if authorization_id is None
        or json.loads(row.detail_json).get("authorization_id") == authorization_id
    ]


def _rolling_grants(db):
    # All authorizations count. Reconfiguration cannot reset the rolling cap.
    return _grants(db, since=controls._now() - RATE_WINDOW)


def prepare(task_id, number, head):
    """Landing only: derive once under the same lock as revocation and arming."""
    try:
        with controls._locked_session() as (db, _):
            candidate = _candidate(db, controls._receipt(db, task_id), number, head)
            grants = _grants(db, task_id=task_id)
            if candidate in grants:
                return None
            _, proposal, _ = _authority(db)
            if proposal.get("mode") == "ongoing":
                _require(len(grants) < MAX_TASK_DERIVATIONS, "task_limit")
                _require(
                    len(
                        _grants(
                            db, source_task_id=candidate["provenance"]["source_task_id"]
                        )
                    )
                    < MAX_TASK_DERIVATIONS,
                    "source_limit",
                )
                _require(
                    len(_rolling_grants(db)) < proposal["max_repairs"], "rolling_limit"
                )
            else:
                _require(
                    len(_grants(db, candidate["authorization_id"]))
                    < proposal["max_repairs"],
                    "delegation_exhausted",
                )
            controls._audit(
                db,
                ACTOR,
                GRANT,
                task_id=task_id,
                **{key: value for key, value in candidate.items() if key != "task_id"},
            )
        return None
    except (httpx.HTTPError, ValueError) as exc:
        return (
            str(exc)
            if isinstance(exc, ValueError)
            else "repair_landing_observation_unavailable"
        )
    except (KeyError, TypeError, AttributeError):
        return "repair_landing_evidence_invalid"


def refusal(db, row, number, head):
    if _latest(db, CONTROL) is None:
        return "scoped_landing_authorization_missing"
    try:
        candidate = _candidate(db, row, number, head)
        _require(
            candidate in _grants(db, task_id=row.task_id),
            "authorization_missing_or_changed",
        )
        return None
    except (httpx.HTTPError, ValueError) as exc:
        return (
            str(exc)
            if isinstance(exc, ValueError)
            else "repair_landing_observation_unavailable"
        )
    except (KeyError, TypeError, AttributeError):
        return "repair_landing_evidence_invalid"
