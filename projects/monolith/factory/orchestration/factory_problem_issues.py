"""Turn selected exact factory audit events into ordinary GitHub issues.

The audit ledger is both the bounded source queue and the idempotency record.
Every GitHub write is preceded by a durable ``write_started`` row. If the
outcome is ambiguous, later ticks only reconcile the exact marker; they never
blindly repeat the create request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re

import httpx
from sqlalchemy import and_, or_
from sqlmodel import Session, select

from core.github import GITHUB_API
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
_TERMINAL_ACTIONS = (
    "problem_issue_created",
    "problem_issue_reconciled",
    "problem_issue_write_refused",
    "problem_issue_unresolved",
    "problem_issue_source_refused",
)
_EVENT_ACTIONS = {
    "node_stalled": "node_stalled",
    "workflow_stranded": "workflow_stranded",
    "landing_recovery_exhausted": "merge_arm_refused",
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def github_list(repo: str, suffix: str) -> list:
    """Use the conductor's bounded repository read through one test seam."""
    from factory.orchestration.factory_conductor import github_list as read

    return read(repo, suffix)


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "monolith-factory-problem-issues",
    }
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_write(repo: str, payload: dict) -> object:
    """Create one issue in the configured repository with bounded I/O."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    with httpx.Client(timeout=WRITE_TIMEOUT_SECONDS) as client:
        with client.stream(
            "POST",
            f"{GITHUB_API}/repos/{repo}/issues",
            headers=_headers(),
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
    if row.action in ("node_stalled", "workflow_stranded"):
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
    number = detail.get("pr_number")
    if type(number) is not int or number <= 0:
        return None
    return {
        "source": source,
        "task_id": row.task_id,
        "pr_number": number,
    }


def _fingerprint(source: str, row: FactoryAudit, detail: dict) -> str | None:
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
    lines.extend(
        [
            "",
            "## Evidence",
            "",
            f"- Factory task: `{row.task_id}`",
            f"- Source audit: `{row.id}`",
        ]
    )
    for key in (
        "workflow_id",
        "node_key",
        "idle_seconds",
        "turn_timeout_seconds",
        "workflow_version",
        "running_version",
        "pr_number",
    ):
        value = detail.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            lines.append(f"- {key.replace('_', ' ').title()}: `{str(value)[:500]}`")
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
                _aware(started.created_at)
                + timedelta(minutes=block["retry_minutes"][0])
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
    block = problem_issues_policy(policy)
    # Old policies omit the block. They remain inert for new source events, but
    # an intent recorded before an operator removed the block still needs its
    # bounded read-only reconciliation to reach a terminal state.
    if "problem_issues" not in policy:
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
        source = (
            row.action
            if row.action in ("node_stalled", "workflow_stranded")
            else "landing_recovery_exhausted"
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
    receipt = _source_receipt(row)
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
    issue = _issue(source, row, source_detail, repo, issue_number, marker)

    with _locked_session() as (db, _control):
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
            next_retry = _aware(started[0].created_at) + timedelta(hours=24)
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

    payload = {**issue, "labels": list(block["labels"])}
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
