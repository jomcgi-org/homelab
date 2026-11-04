"""Turn selected exact factory audit events into ordinary GitHub issues.

The audit ledger is both the bounded source queue and the idempotency record.
Every GitHub write is preceded by a durable ``write_started`` row. If the
outcome is ambiguous, later ticks only reconcile the exact marker; they never
blindly repeat the create request.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
import re

import httpx
from sqlalchemy import and_, or_
from sqlmodel import Session, select

from core.clock import aware
from core.github import GITHUB_API, github_headers
from factory.orchestration.factory_controls import (
    PROBLEM_ISSUE_SOURCES,
    _audit,
    _locked_session,
    _now,
    _read_session,
    problem_issues_policy,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
)

logger = logging.getLogger(__name__)

ACTOR = "factory:problem-issues"
WRITE_TIMEOUT_SECONDS = 15
RESPONSE_LIMIT_BYTES = 1_000_000
MARKER_PREFIX = "factory-problem"
RECOVERY_SOURCES = frozenset({"recurring_blocker", "workflow_stranded"})
RECOVERY_ACTIVATION = "problem_issue_recovery_loop_activated"
RECOVERY_REPO = "jomcgi-org/homelab"
_TERMINAL_ACTIONS = (
    "problem_issue_created",
    "problem_issue_reconciled",
    "problem_issue_write_refused",
    "problem_issue_unresolved",
    "problem_issue_source_refused",
)
_EVENT_ACTIONS = {
    "quota_improvement": "improvement_candidate",
    "node_stalled": "node_stalled",
    "workflow_stranded": "workflow_stranded",
    "landing_recovery_exhausted": "merge_arm_refused",
    "recurring_blocker": "recurring_blocker",
}


def _delivery_enabled(policy: dict) -> bool:
    from factory.orchestration import factory_controls as controls

    if policy.get("repo") != RECOVERY_REPO:
        return False
    intake = controls.intake_policy(policy)
    if not intake["enabled"] or intake["refinement_batch"]:
        return False
    entry = controls.repo_entry(policy, RECOVERY_REPO)
    return bool(
        entry["enabled"]
        and not entry["paused"]
        and "bug" in entry["labels"]
        and "bug" not in entry["exclude_labels"]
    )


def _activate_recovery_loop(policy: dict) -> dict:
    """One reviewed rollout transition, not a recurring override of controls.

    Only the already-authorized homelab bug lane qualifies. A dedicated
    operator decision (including a disable), or a previous rollout transition,
    wins forever. No receipt, generation, intake or landing policy is changed.
    """
    from core.jsonio import dumps

    if not _delivery_enabled(policy):
        return policy
    with _locked_session() as (db, control):
        live = json.loads(control.policy_json or "{}")
        if live != policy:
            return live
        if control.state != "enabled":
            return policy
        if (
            db.exec(
                select(FactoryAudit.id)
                .where(
                    FactoryAudit.action.in_(
                        (RECOVERY_ACTIVATION, "problem_issues_control_applied")
                    )
                )
                .limit(1)
            ).first()
            is not None
        ):
            return policy
        before = live.get("problem_issues", {})
        live["problem_issues"] = {
            **before,
            "enabled": True,
            "sources": {
                **before.get("sources", {}),
                **dict.fromkeys(RECOVERY_SOURCES, True),
            },
        }
        problem_issues_policy(live)  # Validate before committing the upgrade.
        control.policy_json = dumps(live)
        control.version += 1
        control.actor, control.updated_at = ACTOR, _now()
        _audit(
            db,
            ACTOR,
            RECOVERY_ACTIVATION,
            before=before,
            after=live["problem_issues"],
            generation=live.get("generation"),
            version=control.version,
        )
        return live


def _spare_quota() -> bool:
    """Reuse improvement's fresh per-grant surplus evidence, not a headline.

    This authorizes issue discovery only. Normal admission and provider routing
    still own every paid start; no scout subscription or execution fence changes.
    """
    from factory.execution.provider_quota import fetch_provider_quota_sync
    from factory.orchestration.factory_improvement_preview import (
        PreviewPolicy,
        quota_decisions,
    )

    return any(
        row["eligible"]
        for row in quota_decisions(
            fetch_provider_quota_sync(force=True), now=_now(), policy=PreviewPolicy()
        )
    )


def _recovery_owner(db, row):
    """Bind local ownership without interpreting a card as resume authority."""
    from factory.orchestration.factory_models import FactoryControl, WorkItem

    receipt = _receipt(db, row.task_id)
    if receipt is None:
        return None
    item = db.get(WorkItem, receipt.work_item_id) if receipt.work_item_id else None
    labels = set(item.labels) if item else set()
    if (
        receipt.state in {"succeeded", "cancelled"}
        or "human" in labels
        or f"{MARKER_PREFIX}:" in (receipt.body or "")
    ):
        return None
    control = db.get(FactoryControl, "factory")
    if control is None or control.state != "enabled":
        return None
    return {
        **receipt.model_dump(mode="json"),
        "control_version": control.version,
        "label_mirror": item.model_dump(mode="json") if item else None,
    }


def _recovery_context(policy, row):
    from factory.orchestration.factory_conductor import github_get

    with _read_session() as db:
        owner = _recovery_owner(db, row)
    if owner is None or owner["repo"] != RECOVERY_REPO:
        return None
    issue = github_get(owner["repo"], f"issues/{owner['issue_number']}")
    labels = {x["name"].lower() for x in issue.get("labels", [])}
    if (
        issue.get("number") != owner["issue_number"]
        or issue.get("state") != "open"
        or issue.get("assignees")
        or labels & {"human", "security-finding", "security"}
        or f"{MARKER_PREFIX}:" in (issue.get("body") or "")
    ):
        return None
    return owner


def _watch_recovery(db, row, source, fingerprint):
    """Retain an exact private target; absent bindings cannot prove recovery."""
    from factory.orchestration.models import SwarmNodeRun

    detail = json.loads(row.detail_json)
    node_key = detail.get("node_key")
    run = (
        db.exec(
            select(SwarmNodeRun)
            .where(
                SwarmNodeRun.task_id == row.task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.created_at <= row.created_at,
            )
            .order_by(SwarmNodeRun.id.desc())
            .limit(1)
        ).first()
        if node_key
        else None
    )
    if detail.get("source_run_id") is not None and (
        run is None or run.id != detail["source_run_id"]
    ):
        run = None
    _audit(
        db,
        ACTOR,
        "problem_issue_recovery_watch",
        task_id=row.task_id,
        **_detail(
            row,
            source,
            fingerprint,
            node_key=node_key,
            source_run_id=run.id if run else None,
        ),
    )


def _observe_recoveries():
    """Observe, never resume: merged/deployed repair is not runtime recovery.

    A bounded pending set reuses the producer ledger. Exact successful node
    execution must follow the repair's existing rollout proof. Historical
    cards, holds, missing bindings and unknown starts stay unverified.
    """
    from factory.orchestration import factory_controls as controls
    from factory.orchestration.factory_funding import routine_hold
    from factory.orchestration.models import SwarmNodeRun

    with _locked_session() as (db, _control):
        watches = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.action == "problem_issue_recovery_watch",
            )
            .order_by(FactoryAudit.id.desc())
            .limit(50)
        ).all()
        if not watches:
            return
        unknown = controls.unresolved_start_task_ids(db)
        for watch in watches:
            detail = json.loads(watch.detail_json)
            fingerprint = detail["fingerprint"]
            if _last_matching(
                db, fingerprint, ("problem_issue_recovery_observed",), watch.task_id
            ):
                continue
            source = _receipt(db, watch.task_id)
            if (
                source is None
                or source.task_paused
                or source.cancellation_requested
                or source.state in {"escalated", "cancelled", "uncertain"}
                or source.task_id in unknown
                or not detail.get("source_run_id")
                or routine_hold(db, source, source.task_id)
            ):
                continue
            created = _last_matching(
                db,
                fingerprint,
                ("problem_issue_created", "problem_issue_reconciled"),
                watch.task_id,
            )
            if created is None:
                continue
            result = json.loads(created.detail_json)
            numbers = result.get("issue_numbers") or [result.get("issue_number")]
            repairs = db.exec(
                select(FactoryReceipt).where(
                    FactoryReceipt.repo == source.repo,
                    FactoryReceipt.issue_number.in_(
                        [n for n in numbers if type(n) is int]
                    ),
                    FactoryReceipt.task_class == "bug-fix",
                    FactoryReceipt.state == "succeeded",
                )
            ).all()
            for repair in repairs:
                rollout = db.exec(
                    select(FactoryAudit)
                    .where(
                        FactoryAudit.task_id == repair.task_id,
                        FactoryAudit.action == "rollout_verified",
                    )
                    .order_by(FactoryAudit.id.desc())
                    .limit(1)
                ).first()
                if rollout is None:
                    continue
                recovered = db.exec(
                    select(SwarmNodeRun)
                    .where(
                        SwarmNodeRun.task_id == watch.task_id,
                        SwarmNodeRun.node_key == detail["node_key"],
                        SwarmNodeRun.id > detail["source_run_id"],
                        SwarmNodeRun.status == "succeeded",
                        SwarmNodeRun.created_at > rollout.created_at,
                        SwarmNodeRun.finished_at.is_not(None),
                    )
                    .order_by(SwarmNodeRun.id)
                    .limit(1)
                ).first()
                if recovered is not None:
                    _audit(
                        db,
                        ACTOR,
                        "problem_issue_recovery_observed",
                        task_id=watch.task_id,
                        fingerprint=fingerprint,
                        repair_task_id=repair.task_id,
                        rollout_audit_id=rollout.id,
                        node_key=detail["node_key"],
                        source_run_id=detail["source_run_id"],
                        recovered_run_id=recovered.id,
                    )
                    break


def configure(
    operation,
    enabled,
    recurring_blocker,
    workflow_stranded,
    expected_version,
    reason,
    *,
    actor,
    request_key=None,
    expected_preview_sha256=None,
):
    """Change only the producer switch and its two recovery sources.

    The ordinary producer owns watermarking, deduplication and publication;
    intake still owns receipts. No generation rollover or task mutation.
    """
    from core.jsonio import dumps
    from factory.orchestration import factory_controls as controls

    if operation not in {"preview", "apply"}:
        raise ValueError("problem_issues_operation_invalid")
    if any(
        type(v) is not bool for v in (enabled, recurring_blocker, workflow_stranded)
    ):
        raise ValueError("problem_issues_switch_invalid")
    request = dict(
        enabled=enabled,
        recurring_blocker=recurring_blocker,
        workflow_stranded=workflow_stranded,
        expected_version=expected_version,
        reason=controls._text(reason, "reason"),
        request_key=request_key,
        expected_preview_sha256=expected_preview_sha256,
    )
    action = "problem_issues_control_applied"
    with _locked_session() as (db, control):
        if operation == "apply":
            controls._text(request_key, "request_key")
            for raw in db.exec(
                select(FactoryAudit.detail_json).where(
                    FactoryAudit.actor == actor,
                    FactoryAudit.action == action,
                )
            ).all():
                prior = json.loads(raw)
                if prior["request"]["request_key"] == request_key:
                    if prior["request"] != request:
                        raise ValueError("problem_issues_request_conflict")
                    return prior["result"]
        if control.version != expected_version:
            raise ValueError("problem_issues_version_changed")
        policy = json.loads(control.policy_json or "{}")
        if not policy.get("repo") or control.state == "stopped":
            raise ValueError("problem_issues_control_unavailable")
        before = policy.get("problem_issues", {})
        after = {
            **before,
            "enabled": enabled,
            "sources": {
                **before.get("sources", {}),
                "recurring_blocker": recurring_blocker,
                "workflow_stranded": workflow_stranded,
            },
        }
        normalized = problem_issues_policy({"problem_issues": after})
        proposal = dict(
            ok=True,
            version=control.version,
            policy_sha256=controls.digest(policy),
            before=before,
            after=after,
            generation=policy.get("generation"),
            reason=request["reason"],
            effective_sources=_effective(normalized),
        )
        proposal["preview_sha256"] = controls.digest(proposal)
        if operation == "preview":
            return proposal
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("problem_issues_preview_changed")
        policy["problem_issues"] = after
        control.policy_json = dumps(policy)
        control.version += 1
        control.actor, control.updated_at = actor, _now()
        result = dict(
            ok=True,
            version=control.version,
            generation=policy.get("generation"),
            dispatched=False,
        )
        _audit(db, actor, action, request=request, proposal=proposal, result=result)
        return result


def github_list(repo: str, suffix: str) -> list:
    """Use the conductor's bounded repository read through one test seam."""
    from factory.orchestration.factory_conductor import github_list as read

    return read(repo, suffix)


def github_write(repo: str, payload: dict) -> object:
    """Create one issue in the configured repository with bounded I/O."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    with httpx.Client(timeout=WRITE_TIMEOUT_SECONDS) as client:
        with client.stream(
            "POST",
            f"{GITHUB_API}/repos/{repo}/issues",
            headers=github_headers("monolith-factory-problem-issues"),
            json=payload,
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > RESPONSE_LIMIT_BYTES:
                    raise ValueError("GitHub response exceeds factory limit")
                content.extend(chunk)
    return json.loads(bytes(content) or b"{}")


def _source(row: FactoryAudit) -> tuple[str, dict] | None:
    try:
        detail = json.loads(row.detail_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    if row.action == "improvement_candidate":
        return "quota_improvement", detail
    if row.action in ("node_stalled", "workflow_stranded", "recurring_blocker"):
        return row.action, detail
    if (
        row.action == "merge_arm_refused"
        and detail.get("reason") == "landing_recovery_exhausted"
    ):
        return "landing_recovery_exhausted", detail
    return None


def _identity(source: str, row: FactoryAudit, detail: dict) -> dict | None:
    if source in ("node_stalled", "workflow_stranded"):
        workflow_id = detail.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id:
            return None
        return {
            "source": source,
            "task_id": row.task_id,
            "workflow_id": workflow_id,
        }
    if source == "recurring_blocker":
        key = detail.get("blocker_key")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            return None
        # The problem, not the task: a re-admitted receipt repeats the key.
        return {"source": source, "blocker_key": key}
    number = detail.get("pr_number")
    if type(number) is not int or number <= 0:
        return None
    return {
        "source": source,
        "task_id": row.task_id,
        "pr_number": number,
    }


def _fingerprint(source: str, row: FactoryAudit, detail: dict) -> str | None:
    if source == "quota_improvement":
        fingerprint = detail.get("fingerprint")
        return (
            fingerprint
            if isinstance(fingerprint, str)
            and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            else None
        )
    identity = _identity(source, row, detail)
    if identity is None:
        return None
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _marker(fingerprint: str) -> str:
    return f"<!-- {MARKER_PREFIX}:{fingerprint} -->"


def _effective(block: dict) -> dict[str, bool]:
    return {
        source: bool(block["enabled"] and block["sources"].get(source))
        for source in PROBLEM_ISSUE_SOURCES
    }


def _event_filter(source: str, after: int):
    action = _EVENT_ACTIONS[source]
    conditions = [FactoryAudit.action == action, FactoryAudit.id > after]
    if source == "landing_recovery_exhausted":
        conditions.append(
            FactoryAudit.detail_json.contains('"reason":"landing_recovery_exhausted"')
        )
    return and_(*conditions)


def _latest_source_id(db: Session, source: str) -> int:
    row = db.exec(
        select(FactoryAudit.id)
        .where(_event_filter(source, 0))
        .order_by(FactoryAudit.id.desc())
    ).first()
    return int(row or 0)


def _observe_policy(block: dict) -> tuple[bool, dict]:
    """Record switch transitions and watermark newly enabled source history."""
    current = _effective(block)
    with _locked_session() as (db, _control):
        last = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "problem_issue_policy_observed")
            .order_by(FactoryAudit.id.desc())
        ).first()
        previous_detail = json.loads(last.detail_json) if last is not None else {}
        previous = previous_detail.get("effective_sources") or {}
        if previous == current:
            return False, previous_detail
        watermarks = dict(previous_detail.get("watermarks") or {})
        for source, enabled in current.items():
            if enabled and not previous.get(source, False):
                watermarks[source] = _latest_source_id(db, source)
            else:
                watermarks.setdefault(source, 0)
        _audit(
            db,
            ACTOR,
            "problem_issue_policy_observed",
            enabled=block["enabled"],
            effective_sources=current,
            watermarks=watermarks,
        )
        return True, {
            "effective_sources": current,
            "watermarks": watermarks,
        }


def _fingerprint_clause(fingerprint: str):
    return FactoryAudit.detail_json.contains(f'"fingerprint":"{fingerprint}"')


def _task_clause(task_id: str | None):
    return (
        FactoryAudit.task_id.is_(None)
        if task_id is None
        else FactoryAudit.task_id == task_id
    )


def _last_matching(
    db: Session,
    fingerprint: str,
    actions: tuple[str, ...],
    task_id: str | None,
):
    return db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action.in_(actions),
            _task_clause(task_id),
            _fingerprint_clause(fingerprint),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()


def _terminal(db: Session, fingerprint: str, task_id: str | None) -> bool:
    return _last_matching(db, fingerprint, _TERMINAL_ACTIONS, task_id) is not None


def _cursor(db: Session, source: str, watermark: int) -> int:
    row = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action.in_(_TERMINAL_ACTIONS),
            FactoryAudit.detail_json.contains(f'"source":"{source}"'),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return watermark
    try:
        detail = json.loads(row.detail_json)
    except (TypeError, ValueError):
        return watermark
    source_id = detail.get("source_audit_id")
    return max(watermark, source_id) if type(source_id) is int else watermark


def _pending_started(db: Session):
    row = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == "problem_issue_write_started")
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    try:
        detail = json.loads(row.detail_json)
        fingerprint = detail["fingerprint"]
    except (TypeError, ValueError, KeyError):
        return None
    if not _terminal(db, fingerprint, row.task_id):
        return row, detail
    return None


def _discover(repo: str, marker: str, block: dict) -> tuple[list[int], bool]:
    found = []
    truncated = False
    for page in range(1, block["issue_pages"] + 1):
        issues = github_list(
            repo,
            "issues?state=all&sort=created&direction=desc"
            f"&per_page={block['issues_per_page']}&page={page}",
        )
        if not isinstance(issues, list):
            raise ValueError("GitHub returned a non-array")
        for issue in issues:
            if (
                not isinstance(issue, dict)
                or "pull_request" in issue
                or marker not in (issue.get("body") or "")
            ):
                continue
            number = issue.get("number")
            if type(number) is int and number > 0:
                found.append(number)
        if len(issues) < block["issues_per_page"]:
            break
        truncated = page == block["issue_pages"]
    return sorted(set(found)), truncated


def _receipt(db: Session, task_id: str | None) -> FactoryReceipt | None:
    if task_id is None:
        return None
    return db.exec(
        select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
    ).first()


def _source_receipt(row: FactoryAudit) -> tuple[str, int] | None:
    with _read_session() as db:
        receipt = _receipt(db, row.task_id)
        if receipt is None:
            return None
        return receipt.repo, receipt.issue_number


def _issue(
    source: str,
    row: FactoryAudit,
    detail: dict,
    repo: str,
    issue_number: int,
    marker: str,
):
    source_url = f"https://github.com/{repo}/issues/{issue_number}"
    titles = {
        "node_stalled": f"factory: node stalled while delivering #{issue_number}",
        "workflow_stranded": (
            f"factory: workflow stranded while delivering #{issue_number}"
        ),
        "landing_recovery_exhausted": (
            f"factory: landing recovery exhausted for #{issue_number}"
        ),
        "recurring_blocker": (
            f"factory: repeated blocker without progress on #{issue_number}"
        ),
    }
    lines = [
        marker,
        "",
        "## Factory problem signal",
        "",
        f"The factory recorded the exact `{source}` event.",
        "",
        "## Source links",
        "",
        f"- [Delivery issue #{issue_number}]({source_url})",
    ]
    pr_number = detail.get("pr_number")
    if type(pr_number) is int and pr_number > 0:
        lines.append(
            f"- [Pull request #{pr_number}](https://github.com/{repo}/pull/{pr_number})"
        )
    paths = {
        "recurring_blocker": "factory_funding.py and factory_progress_watchdog.py",
        "workflow_stranded": "factory_conductor.py and durable workflow reconciliation",
        "node_stalled": "factory_conductor.py and node timeout supervision",
        "landing_recovery_exhausted": "factory_landing.py and exact-head recovery",
    }
    lines.extend(
        [
            "",
            "## Scoped repair",
            "",
            "Inspect `projects/monolith/factory/orchestration/`: "
            + paths[source]
            + ".",
            "Reproduce the missing recovery transition with synthetic evidence and "
            "fix it in the existing owner. Check existing issues and PRs before "
            "implementation; reuse an existing repair rather than competing with it.",
            "Private receipts, audit details, invocations and quota observations "
            "stay in private factory records. Do not copy them into GitHub.",
            "",
            "## Acceptance",
            "",
            "- Regression tests prove eligible recovery and refusal on human holds, "
            "unknown starts, stale evidence and concurrent changes.",
            "- Independent exact-head review and required CI precede normal merge "
            "queue landing; verify the managed rollout through the existing owner.",
            "- Observe an eligible same-task recovery after rollout. A merged repair "
            "alone is not recovery evidence. Historical cards and explicit holds "
            "remain with their owners; report unsupported recovery rather than "
            "clearing them or fabricating success.",
            "- Do not reset retry history, bypass admission or create another "
            "recovery engine. Repair tickets must not recursively produce repairs.",
        ]
    )
    lines.extend(
        [
            "",
            "This issue is discovery input only. It does not directly admit "
            "a receipt or task.",
            "",
        ]
    )
    return {"title": titles[source], "body": "\n".join(lines)}


def _detail(row: FactoryAudit, source: str, fingerprint: str, **extra) -> dict:
    return {
        "source": source,
        "source_audit_id": row.id,
        "fingerprint": fingerprint,
        **extra,
    }


def _record(action: str, row: FactoryAudit, source: str, fingerprint: str, **extra):
    with _locked_session() as (db, _control):
        _audit(
            db,
            ACTOR,
            action,
            task_id=row.task_id,
            **_detail(row, source, fingerprint, **extra),
        )


def _exception_detail(exc: Exception) -> dict:
    return {
        "error": type(exc).__name__,
        "status": getattr(getattr(exc, "response", None), "status_code", None),
    }


def _ambiguous(exc: Exception) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return True
    status = exc.response.status_code
    rate_limited = status == 403 and (
        "retry-after" in exc.response.headers
        or exc.response.headers.get("x-ratelimit-remaining") == "0"
    )
    return status >= 500 or status in (408, 429) or rate_limited


def _due(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed.tzinfo is not None and parsed <= _now()


def _reconcile_pending(block: dict, started: FactoryAudit, detail: dict):
    fingerprint = detail["fingerprint"]
    source = detail["source"]
    source_id = detail["source_audit_id"]
    repo = detail.get("repo")
    if not isinstance(repo, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
    ):
        _record(
            "problem_issue_unresolved",
            started,
            source,
            fingerprint,
            source_audit_id=source_id,
            reason="write_repository_missing",
        )
        return
    with _read_session() as db:
        retries = len(
            db.exec(
                select(FactoryAudit.id).where(
                    FactoryAudit.action == "problem_issue_reconcile_retry",
                    _task_clause(started.task_id),
                    _fingerprint_clause(fingerprint),
                )
            ).all()
        )
        last = _last_matching(
            db,
            fingerprint,
            (
                "problem_issue_write_uncertain",
                "problem_issue_reconcile_retry",
            ),
            started.task_id,
        )
    last_detail = (
        json.loads(last.detail_json)
        if last is not None
        else {
            "next_retry_at": (
                aware(started.created_at) + timedelta(minutes=block["retry_minutes"][0])
            ).isoformat()
        }
    )
    if not _due(last_detail.get("next_retry_at")):
        return
    marker = _marker(fingerprint)
    try:
        found, truncated = _discover(repo, marker, block)
    except Exception as exc:  # noqa: BLE001 - reads fail closed and stay bounded
        found = []
        truncated = False
        discovery_error = _exception_detail(exc)
    else:
        discovery_error = None
    retry = retries + 1
    if found:
        _record(
            "problem_issue_reconciled",
            started,
            source,
            fingerprint,
            source_audit_id=source_id,
            issue_numbers=found,
            retry=retry,
            issue_scan_capped=truncated,
        )
        return
    delays = block["retry_minutes"]
    if retry >= len(delays):
        _record(
            "problem_issue_unresolved",
            started,
            source,
            fingerprint,
            source_audit_id=source_id,
            retry=retry,
            issue_scan_capped=truncated,
            **(discovery_error or {}),
        )
        return
    next_retry = _now() + timedelta(minutes=delays[retry])
    _record(
        "problem_issue_reconcile_retry",
        started,
        source,
        fingerprint,
        source_audit_id=source_id,
        retry=retry,
        next_retry_at=next_retry.isoformat(),
        issue_scan_capped=truncated,
        **(discovery_error or {}),
    )


def _next_event(block: dict, observed: dict):
    effective = observed["effective_sources"]
    watermarks = observed.get("watermarks") or {}
    with _read_session() as db:
        filters = []
        for source in PROBLEM_ISSUE_SOURCES:
            if effective.get(source):
                filters.append(
                    _event_filter(
                        source,
                        _cursor(db, source, int(watermarks.get(source, 0))),
                    )
                )
        if not filters:
            return None, False
        rows = db.exec(
            select(FactoryAudit)
            .where(or_(*filters))
            .order_by(FactoryAudit.id)
            .limit(block["source_audit_limit"] + 1)
        ).all()
    capped = len(rows) > block["source_audit_limit"]
    return (rows[0] if rows else None), capped


def problem_issues_tick(policy: dict) -> None:
    """Reconcile at most one exact source event and create at most one issue."""
    policy = _activate_recovery_loop(policy)
    if policy.get("repo") == RECOVERY_REPO:
        _observe_recoveries()
    block = problem_issues_policy(policy)
    # Old policies omit the block. They remain inert for new source events, but
    # an intent recorded before an operator removed the block still needs its
    # bounded read-only reconciliation to reach a terminal state.
    if "problem_issues" not in policy:
        _observe_policy(block)
        with _read_session() as db:
            pending = _pending_started(db)
        if pending is not None:
            _reconcile_pending(block, *pending)
        return
    changed, observed = _observe_policy(block)
    with _read_session() as db:
        pending = _pending_started(db)
    if pending is not None:
        _reconcile_pending(block, *pending)
        return
    if changed or not any(observed["effective_sources"].values()):
        return

    row, capped = _next_event(block, observed)
    if row is None:
        return
    parsed = _source(row)
    if parsed is None:
        source = next(
            name for name, action in _EVENT_ACTIONS.items() if action == row.action
        )
        _record(
            "problem_issue_source_refused",
            row,
            source,
            hashlib.sha256(f"malformed:{row.id}".encode()).hexdigest(),
            reason="malformed_source_audit",
        )
        return
    source, source_detail = parsed
    fingerprint = _fingerprint(source, row, source_detail)
    if fingerprint is None:
        _record(
            "problem_issue_source_refused",
            row,
            source,
            hashlib.sha256(f"invalid:{row.id}".encode()).hexdigest(),
            reason="missing_exact_event_identity",
        )
        return
    recovery_owner = None
    if source in RECOVERY_SOURCES and policy.get("repo") == RECOVERY_REPO:
        # A held source may still need a code fix, but this producer never
        # decides that a person's card or unknown invocation is safe to retry.
        # Refusal waits without consuming the event, so fresh quota can unlock
        # discovery without a reset, manual trigger or generation rollover.
        if not _delivery_enabled(policy) or not _spare_quota():
            return
        recovery_owner = _recovery_context(policy, row)
        if recovery_owner is None:
            from factory.orchestration.factory_models import FactoryControl

            with _read_session() as db:
                control = db.get(FactoryControl, "factory")
                if (
                    control is None
                    or control.state != "enabled"
                    or json.loads(control.policy_json or "{}") != policy
                ):
                    return
            _record(
                "problem_issue_source_refused",
                row,
                source,
                fingerprint,
                reason="source_owned_or_recursive_or_unavailable",
            )
            return
    with _read_session() as db:
        completed = _last_matching(db, fingerprint, _TERMINAL_ACTIONS, row.task_id)
        delayed = _last_matching(
            db,
            fingerprint,
            (
                "problem_issue_discovery_failed",
                "problem_issue_daily_capped",
            ),
            row.task_id,
        )
        already_capped = _last_matching(
            db, fingerprint, ("problem_issue_scan_capped",), row.task_id
        )
        issue_scan_capped = _last_matching(
            db, fingerprint, ("problem_issue_issue_scan_capped",), row.task_id
        )
    if completed is not None:
        completed_detail = json.loads(completed.detail_json)
        numbers = completed_detail.get("issue_numbers") or []
        if type(completed_detail.get("issue_number")) is int:
            numbers = [completed_detail["issue_number"]]
        _record(
            "problem_issue_reconciled",
            row,
            source,
            fingerprint,
            issue_numbers=numbers,
            retry=0,
            reconciliation="ledger_fingerprint",
        )
        return
    if delayed is not None:
        delayed_detail = json.loads(delayed.detail_json)
        if not _due(delayed_detail.get("next_retry_at")):
            return
    if capped:
        if already_capped is None:
            _record(
                "problem_issue_scan_capped",
                row,
                source,
                fingerprint,
                limit=block["source_audit_limit"],
            )
    marker = _marker(fingerprint)
    receipt = (
        (source_detail.get("repo"), 0)
        if source == "quota_improvement"
        else _source_receipt(row)
    )
    if receipt is None:
        _record(
            "problem_issue_source_refused",
            row,
            source,
            fingerprint,
            reason="source_receipt_missing",
        )
        return
    repo, issue_number = receipt
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        _record(
            "problem_issue_source_refused",
            row,
            source,
            fingerprint,
            reason="source_receipt_repository_invalid",
        )
        return
    try:
        found, truncated = _discover(repo, marker, block)
    except Exception as exc:  # noqa: BLE001 - no discovery means no write
        next_retry = _now() + timedelta(minutes=block["retry_minutes"][0])
        _record(
            "problem_issue_discovery_failed",
            row,
            source,
            fingerprint,
            next_retry_at=next_retry.isoformat(),
            **_exception_detail(exc),
        )
        return
    if truncated and issue_scan_capped is None:
        _record(
            "problem_issue_issue_scan_capped",
            row,
            source,
            fingerprint,
            pages=block["issue_pages"],
            per_page=block["issues_per_page"],
        )
    if found:
        _record(
            "problem_issue_reconciled",
            row,
            source,
            fingerprint,
            issue_numbers=found,
            retry=0,
        )
        return
    if source == "quota_improvement":
        from factory.orchestration.factory_improvements import validate_candidate

        validated_context = validate_candidate(source_detail)
        if validated_context is None:
            _record(
                "problem_issue_source_refused",
                row,
                source,
                fingerprint,
                reason="improvement_evidence_changed_or_expired",
            )
            return
        issue = source_detail["draft"]
        issue = {key: issue[key] for key in ("title", "body")}
    else:
        issue = _issue(source, row, source_detail, repo, issue_number, marker)

    with _locked_session() as (db, _control):
        if recovery_owner is not None:
            if (
                _control.state != "enabled"
                or json.loads(_control.policy_json or "{}") != policy
                or _recovery_owner(db, row) != recovery_owner
            ):
                return
        if source == "quota_improvement":
            from factory.orchestration.factory_improvement_preview import digest
            from factory.orchestration.factory_improvements import (
                _database_context,
                candidate_matches,
            )

            # Final local rule/owner/goal check under the same lock as intent.
            live = _database_context(
                db, _control, scout_receipt_id=source_detail.get("scout_receipt_id")
            )
            live_block = problem_issues_policy(live["policy"])
            if (
                _control.state != "enabled"
                or not live["repo_enabled"]
                or not live_block["enabled"]
                or not live_block["sources"][source]
                or _control.version != source_detail.get("control_version")
                or not live["intake_complete"]
                or digest(live) != source_detail.get("database_digest")
                or not candidate_matches(source_detail, {**validated_context, **live})
            ):
                _audit(
                    db,
                    ACTOR,
                    "problem_issue_source_refused",
                    **_detail(
                        row, source, fingerprint, reason="improvement_control_changed"
                    ),
                )
                return
        if _terminal(db, fingerprint, row.task_id) or _last_matching(
            db, fingerprint, ("problem_issue_write_started",), row.task_id
        ):
            return
        cutoff = _now() - timedelta(hours=24)
        started = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.action == "problem_issue_write_started",
                FactoryAudit.created_at >= cutoff,
            )
            .order_by(FactoryAudit.created_at)
        ).all()
        if len(started) >= block["max_per_24_hours"]:
            next_retry = aware(started[0].created_at) + timedelta(hours=24)
            _audit(
                db,
                ACTOR,
                "problem_issue_daily_capped",
                task_id=row.task_id,
                **_detail(
                    row,
                    source,
                    fingerprint,
                    used=len(started),
                    limit=block["max_per_24_hours"],
                    next_retry_at=next_retry.isoformat(),
                ),
            )
            return
        _audit(
            db,
            ACTOR,
            "problem_issue_write_started",
            task_id=row.task_id,
            **_detail(
                row,
                source,
                fingerprint,
                marker=marker,
                repo=repo,
            ),
        )
        if recovery_owner is not None:
            _watch_recovery(db, row, source, fingerprint)

    payload = {
        **issue,
        "labels": [] if source == "quota_improvement" else list(block["labels"]),
    }
    try:
        created = github_write(repo, payload)
        if (
            not isinstance(created, dict)
            or type(created.get("number")) is not int
            or marker not in (created.get("body") or "")
        ):
            raise ValueError("GitHub create response did not confirm the marker")
    except Exception as exc:  # noqa: BLE001 - classify without replaying the write
        if _ambiguous(exc):
            next_retry = _now() + timedelta(minutes=block["retry_minutes"][0])
            _record(
                "problem_issue_write_uncertain",
                row,
                source,
                fingerprint,
                next_retry_at=next_retry.isoformat(),
                **_exception_detail(exc),
            )
        else:
            _record(
                "problem_issue_write_refused",
                row,
                source,
                fingerprint,
                **_exception_detail(exc),
            )
        logger.warning("factory problem issue write failed", exc_info=True)
        return
    _record(
        "problem_issue_created",
        row,
        source,
        fingerprint,
        issue_number=created["number"],
    )


__all__ = ["problem_issues_tick"]


def improvement_preview(snapshot: dict, *, now: datetime, policy=None) -> dict:
    """Read-only shaping shared with the bounded audited improvement source."""
    from factory.orchestration.factory_improvement_preview import preview

    return preview(snapshot, now=now, policy=policy)
